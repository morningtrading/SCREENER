"""
SCREENER — FastAPI server for momentum, shorts, and exchange rankings.

Authentication (any one of these grants access):
  * Session cookie  — obtained via the /login page (username + password).
  * HTTP Basic auth — same username + password, for programmatic/browser use.
  * Access token    — `x-access-token` header or `?token=` query param (for scripts/API).
"""
import os
import sys
import json
import html
import base64
import hashlib
import secrets
import binascii
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote_plus
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
PROJECT_ROOT = Path(os.environ.get("SCREENER_PROJECT_ROOT", str(HERE)))
ENV_FILE = Path(os.environ.get("SCREENER_ENV_FILE", str(HERE / ".env")))
load_dotenv(ENV_FILE)

# Access token is required; no insecure default. Set DATA_SERVER_TOKEN in .env.
TOKEN = os.getenv("DATA_SERVER_TOKEN")
if not TOKEN:
    sys.exit("FATAL: DATA_SERVER_TOKEN is not set. Add it to .env before starting the data server.")

# --- Login credentials (username + password) for the /login page and HTTP Basic auth. ---
# The primary user's name defaults to "admin"; its password falls back to the access token
# so the login works out of the box. Override via .env (SCREENER_USER / SCREENER_PASSWORD).
AUTH_USER = os.getenv("SCREENER_USER", "admin")
AUTH_PASSWORD = os.getenv("SCREENER_PASSWORD") or TOKEN

# Full user table: the primary admin plus any extra accounts from SCREENER_USERS, a JSON
# object of {"username": "password"} pairs in .env. All users share the same access level.
USERS: Dict[str, str] = {AUTH_USER: AUTH_PASSWORD}
_extra_users = os.getenv("SCREENER_USERS")
if _extra_users:
    try:
        _parsed = json.loads(_extra_users)
        if isinstance(_parsed, dict):
            USERS.update({str(u): str(p) for u, p in _parsed.items()})
        else:
            print("WARNING: SCREENER_USERS must be a JSON object; ignoring.", file=sys.stderr)
    except (json.JSONDecodeError, ValueError):
        print("WARNING: SCREENER_USERS is not valid JSON; ignoring.", file=sys.stderr)

# Read-only accounts: a JSON list of usernames that may browse but not download files.
# (Token/Basic-with-admin always have full access.)
READONLY_USERS = set()
_ro = os.getenv("SCREENER_READONLY_USERS")
if _ro:
    try:
        _ro_parsed = json.loads(_ro)
        if isinstance(_ro_parsed, list):
            READONLY_USERS = {str(u) for u in _ro_parsed}
        else:
            print("WARNING: SCREENER_READONLY_USERS must be a JSON list; ignoring.", file=sys.stderr)
    except (json.JSONDecodeError, ValueError):
        print("WARNING: SCREENER_READONLY_USERS is not valid JSON; ignoring.", file=sys.stderr)

# Secret used to sign the session cookie. Stable across restarts (derived from the token)
# unless SCREENER_SECRET_KEY is set explicitly.
SECRET_KEY = os.getenv("SCREENER_SECRET_KEY") or hashlib.sha256(
    ("screener-session::" + TOKEN).encode()
).hexdigest()
# Session lifetime in seconds (default 7 days).
SESSION_MAX_AGE = int(os.getenv("SCREENER_SESSION_MAX_AGE", str(7 * 24 * 3600)))

app = FastAPI(title="SCREENER")

# Signed, tamper-proof session cookie (used by the /login flow).
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=SESSION_MAX_AGE,
    same_site="lax",
    https_only=False,  # set true behind HTTPS / a TLS-terminating proxy
)

# Allow CORS for local dev (adjust as needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Authentication helpers -------------------------------------------------
def check_credentials(username: str, password: str) -> bool:
    """Constant-time check of a username/password pair against the user table."""
    expected = USERS.get(username or "")
    if expected is None:
        # Compare against itself so unknown users take a similar code path (limits timing leaks).
        secrets.compare_digest(password or "", password or "")
        return False
    return secrets.compare_digest(password or "", expected)


def get_basic_credentials(request: Request) -> Optional[Tuple[str, str]]:
    """Parse an `Authorization: Basic ...` header into (username, password), if present."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    username, password = decoded.split(":", 1)
    return username, password


def _now_epoch() -> float:
    return datetime.utcnow().timestamp()


def session_seconds_left(request: Request) -> Optional[float]:
    """Seconds until the current login must be re-validated, or None if no session."""
    if request.session.get("auth") is not True:
        return None
    login_at = request.session.get("login_at")
    if not login_at:
        return 0.0  # legacy session with no timestamp -> treat as expired
    return (login_at + SESSION_MAX_AGE) - _now_epoch()


def session_valid(request: Request) -> bool:
    """A session is honoured only within SESSION_MAX_AGE of login (absolute expiry)."""
    left = session_seconds_left(request)
    return left is not None and left > 0


def is_authenticated(request: Request) -> bool:
    """True if the request carries a valid session, Basic-auth header, or access token."""
    # 1. Signed session cookie (set by /login) — only until it expires.
    if request.session.get("auth") is True:
        if session_valid(request):
            return True
        request.session.clear()  # expired: force a fresh login
    # 2. Access token via header or query param (for scripts / API clients).
    token = get_request_token(request)
    if token and secrets.compare_digest(token, TOKEN):
        return True
    # 3. HTTP Basic auth (username + password).
    creds = get_basic_credentials(request)
    if creds and check_credentials(*creds):
        return True
    return False


def current_username(request: Request) -> Optional[str]:
    """The authenticated user's name, or None for token auth (which has no username)."""
    if request.session.get("auth") is True and session_valid(request):
        return request.session.get("user")
    creds = get_basic_credentials(request)
    if creds and check_credentials(*creds):
        return creds[0]
    return None  # token auth (or unauthenticated)


def is_readonly(request: Request) -> bool:
    """True if the requester is a read-only account (browse but no downloads)."""
    user = current_username(request)
    return user is not None and user in READONLY_USERS


def require_full_access(request: Request) -> None:
    """Guard for download endpoints: must be authenticated AND not a read-only account."""
    require_api_auth(request)
    if is_readonly(request):
        raise HTTPException(status_code=403, detail="This account is read-only; downloads are disabled.")


# Client-side ticking countdown for the session pill (TZ-proof: counts down from a
# server-provided remaining-seconds value, so it never depends on the client clock's offset).
COUNTDOWN_JS = """
<script>
(function(){
 if(window.__scrCountdown) return; window.__scrCountdown=true;
 var t0=Date.now();
 function fmt(ms){
   if(ms<0) ms=0;
   var s=Math.floor(ms/1000), d=Math.floor(s/86400); s-=d*86400;
   var h=Math.floor(s/3600); s-=h*3600; var m=Math.floor(s/60); s-=m*60;
   return d>0 ? (d+'d '+h+'h '+m+'m') : (h+'h '+m+'m '+s+'s');
 }
 function tick(){
   var elapsed=(Date.now()-t0)/1000;
   document.querySelectorAll('.countdown').forEach(function(el){
     var rem=parseFloat(el.getAttribute('data-remaining'))-elapsed;
     el.textContent=fmt(rem*1000);
   });
 }
 tick(); setInterval(tick,1000);
})();
</script>
"""


def auth_status_html(request: Request) -> str:
    """A neon pill: who's logged in, their access level, and a live session countdown."""
    if request.session.get("auth") is True and session_valid(request):
        left = session_seconds_left(request) or 0.0
        exp = datetime.utcfromtimestamp(request.session.get("login_at", 0) + SESSION_MAX_AGE)
        exp_str = exp.strftime("%Y-%b-%d %H:%M UTC")
        user = request.session.get("user", "")
        ro = is_readonly(request)
        role = ('<span class="role ro" title="Browse only — downloads disabled">read-only</span>'
                if ro else '<span class="role">full access</span>')
        return (
            '<div class="authpill">&#128275; Logged in'
            + (f' as <b>{user}</b> ' if user else ' ')
            + role
            + f' &middot; session ends in <b><span class="countdown" data-remaining="{int(left)}">…</span></b>'
            + f' <span class="muted">(by {exp_str})</span>'
            + ' &middot; <a href="/logout">Log out</a>'
            + COUNTDOWN_JS
            + '</div>'
        )
    return ('<div class="authpill api">&#128273; Authenticated via API token / Basic auth '
            '&middot; full access &middot; no session expiry</div>')


def require_api_auth(request: Request) -> None:
    """Guard for machine-readable endpoints: 401 (not a redirect) when unauthenticated."""
    if not is_authenticated(request):
        raise HTTPException(
            status_code=401,
            detail="Authentication required.",
            headers={"WWW-Authenticate": 'Basic realm="SCREENER"'},
        )


def login_redirect(request: Request) -> RedirectResponse:
    """Redirect an unauthenticated browser to the login page, remembering where it wanted to go."""
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    return RedirectResponse(url=with_token("/login", "") + f"?next={quote_plus(target)}", status_code=303)


def get_request_token(request: Request) -> str:
    """Accept token from header or query parameter."""
    token = request.headers.get("x-access-token")
    if not token:
        token = request.query_params.get("token")
    return token or ""


def link_token(request: Request) -> str:
    """Token to embed in generated links.

    When the user is logged in via session/Basic auth, links stay clean (the cookie or
    Basic header carries auth). Token-based callers keep their token in links so bookmarks
    and API navigation keep working.
    """
    if request.session.get("auth") is True or get_basic_credentials(request):
        return ""
    return get_request_token(request) or ""


def with_token(path: str, token: str) -> str:
    """Append token as query param to a relative URL path (omitted when token is empty)."""
    if not token:
        return path
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}token={quote_plus(token)}"


def coin_link(coin: str, token: str) -> str:
    tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{coin}USDT.P"
    return f'<a href="{tv_url}" target="_blank" rel="noopener">{coin}</a>'


def tradingview_link(coin: str) -> str:
    """Standalone TradingView chart link (chart emoji) for a coin."""
    tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{coin}USDT.P"
    return (f'<a href="{tv_url}" target="_blank" rel="noopener" class="tv" '
            f'title="View {coin} on TradingView">&#128200;</a>')


def asset_icon(asset_type: Optional[str]) -> str:
    """A small badge flagging non-crypto instruments (stock / index / commodity)."""
    icons = {
        "stock": ("\U0001F3E2", "Stock — not a crypto"),            # 🏢
        "index": ("\U0001F4CA", "Index — not a crypto"),            # 📊
        "commodity": ("\U0001F6E2️", "Commodity — not a crypto"),  # 🛢️
    }
    ent = icons.get(asset_type or "")
    if not ent:
        return ""
    icon, title = ent
    return f' <span class="aflag" title="{title}">{icon}</span>'

# --- Login page (neon themed) ----------------------------------------------
def render_login_page(next_url: str = "/", error: str = "") -> str:
    """A dark, neon-glow login screen. Pure CSS — no external assets."""
    error_html = (
        f'<p class="error">{error}</p>' if error else ""
    )
    safe_next = next_url or "/"
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SCREENER · Login</title>
<style>
  :root {{ --neon:#0ff; --neon2:#f0f; --bg:#05060a; }}
  * {{ box-sizing:border-box; }}
  html,body {{ height:100%; margin:0; }}
  body {{
    font-family:'Segoe UI',system-ui,sans-serif;
    background:radial-gradient(1200px 600px at 50% -10%, #10131f 0%, var(--bg) 60%);
    color:#e8eaf0; display:flex; align-items:center; justify-content:center;
    min-height:100vh; overflow:hidden;
  }}
  /* moving grid backdrop */
  body::before {{
    content:""; position:fixed; inset:-50%;
    background-image:linear-gradient(rgba(0,255,255,.06) 1px,transparent 1px),
                     linear-gradient(90deg,rgba(255,0,255,.06) 1px,transparent 1px);
    background-size:42px 42px; transform:perspective(400px) rotateX(60deg);
    animation:scroll 12s linear infinite; z-index:0;
  }}
  @keyframes scroll {{ from{{background-position:0 0}} to{{background-position:0 42px}} }}
  .card {{
    position:relative; z-index:1; width:340px; max-width:90vw; padding:38px 32px;
    background:rgba(13,16,26,.72); border:1px solid rgba(0,255,255,.25);
    border-radius:16px; backdrop-filter:blur(10px);
    box-shadow:0 0 40px rgba(0,255,255,.15), inset 0 0 22px rgba(255,0,255,.06);
  }}
  .logo {{
    text-align:center; font-weight:800; font-size:30px; letter-spacing:5px;
    margin:0 0 4px; color:#fff;
    text-shadow:0 0 6px var(--neon),0 0 14px var(--neon),0 0 28px var(--neon2),0 0 48px var(--neon2);
    animation:flicker 3.5s infinite alternate;
  }}
  .logo .bolt {{ color:var(--neon); text-shadow:0 0 10px var(--neon),0 0 22px var(--neon); }}
  @keyframes flicker {{
    0%,18%,22%,25%,53%,57%,100% {{ opacity:1; }}
    20%,24%,55% {{ opacity:.55; }}
  }}
  .tag {{ text-align:center; color:#7d8499; font-size:12px; letter-spacing:2px;
          margin:0 0 26px; text-transform:uppercase; }}
  label {{ display:block; font-size:11px; letter-spacing:1.5px; text-transform:uppercase;
           color:#8a93a8; margin:14px 0 6px; }}
  input {{
    width:100%; padding:12px 14px; background:rgba(255,255,255,.04);
    border:1px solid rgba(0,255,255,.2); border-radius:9px; color:#fff; font-size:15px;
    outline:none; transition:.2s;
  }}
  input:focus {{ border-color:var(--neon); box-shadow:0 0 0 2px rgba(0,255,255,.25),0 0 18px rgba(0,255,255,.25); }}
  button {{
    width:100%; margin-top:24px; padding:13px; border:0; border-radius:9px; cursor:pointer;
    font-size:15px; font-weight:700; letter-spacing:1px; color:#03121a;
    background:linear-gradient(90deg,var(--neon),var(--neon2));
    box-shadow:0 0 20px rgba(0,255,255,.4); transition:.2s; text-transform:uppercase;
  }}
  button:hover {{ filter:brightness(1.12); box-shadow:0 0 30px rgba(255,0,255,.5); }}
  .pw-wrap {{ position:relative; }}
  .pw-wrap input {{ padding-right:46px; }}
  .toggle {{ position:absolute; right:6px; top:50%; transform:translateY(-50%);
    width:auto; margin:0; padding:4px 9px; background:transparent; border:0; box-shadow:none;
    color:#8a93a8; font-size:16px; cursor:pointer; text-transform:none; letter-spacing:0; }}
  .toggle:hover {{ filter:none; box-shadow:none; color:var(--neon); }}
  .hint {{ text-align:center; color:#5f6677; font-size:11px; margin:16px 0 0; letter-spacing:.5px; }}
  .error {{ background:rgba(255,40,80,.12); border:1px solid rgba(255,40,80,.5);
            color:#ff7a93; padding:9px 12px; border-radius:8px; font-size:13px;
            text-align:center; margin:6px 0 0; }}
</style></head>
<body>
  <form class="card" method="post" action="/login">
    <h1 class="logo"><span class="bolt">&#9889;</span> SCREENER</h1>
    <p class="tag">Binance Futures Data Server</p>
    {error_html}
    <input type="hidden" name="next" value="{safe_next}">
    <label for="u">Username</label>
    <input id="u" name="username" autocomplete="username" autofocus required>
    <label for="p">Password</label>
    <div class="pw-wrap">
      <input id="p" name="password" type="password" autocomplete="current-password" required>
      <button type="button" class="toggle" id="pwtoggle" title="Show / hide password" aria-label="Show password">&#128065;</button>
    </div>
    <button type="submit">Enter</button>
    <p class="hint">Stays signed in for {SESSION_MAX_AGE // 86400} days</p>
  </form>
  <script>
    (function(){{
      var b=document.getElementById('pwtoggle'), p=document.getElementById('p');
      if(b&&p) b.addEventListener('click',function(){{
        var show=p.type==='password';
        p.type=show?'text':'password';
        b.style.color=show?'#0ff':'';
        p.focus();
      }});
    }})();
  </script>
</body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # Already authenticated with a still-valid session? Skip the form.
    if session_valid(request):
        return RedirectResponse(url="/", status_code=303)
    next_url = request.query_params.get("next", "/")
    return render_login_page(next_url=next_url)


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    next_url = form.get("next") or "/"
    # Only allow local redirects (avoid open-redirect via ?next=).
    if not next_url.startswith("/"):
        next_url = "/"
    if check_credentials(username, password):
        request.session["auth"] = True
        request.session["user"] = username
        request.session["login_at"] = int(_now_epoch())  # stamps the absolute expiry window
        return RedirectResponse(url=next_url, status_code=303)
    return HTMLResponse(
        render_login_page(next_url=next_url, error="Invalid username or password."),
        status_code=401,
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url=with_token("/login", ""), status_code=303)


# --- Endpoint: Landing page / navigation menu ---
@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    if not is_authenticated(request):
        return login_redirect(request)
    token = link_token(request)
    nav = [
        ("Full Binance Ranking", "Every Binance futures perpetual ranked, filtered by volume &amp; spread, with a volatility index. Green = tradeable.", with_token("/binance-ranking", token)),
        ("MEXC Ranking", "Every MEXC futures perpetual ranked the same way (public API, no key). Green = tradeable.", with_token("/mexc-ranking", token)),
        ("Combined (MEXC + Binance)", "Trade-on-MEXC selection view with Binance cost comparison side by side.", with_token("/combined", token)),
        ("Momentum", "Top momentum coins scored across MEXC+HL universe.", with_token("/momentum", token)),
        ("Shorts", "Short-side opportunities and funding rates.", with_token("/shorts", token)),
    ]
    cards = "".join(
        f'<a class="card" href="{url}"><h3>{title}</h3><p>{desc}</p></a>'
        for title, desc, url in nav
    )
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SCREENER &middot; Home</title>
{DATA_PAGE_CSS}
</head><body><div class="wrap">
{neon_logo("SCREENER &middot; Momentum &amp; Rankings")}
{nav_bar(request, token)}
{auth_status_html(request)}
{cards}
</div></body></html>"""
    return html


# --- Shared neon look-and-feel for the data pages (matches the /login logo) ----
DATA_PAGE_CSS = """
<style>
:root{--neon:#0ff;--neon2:#f0f;--rate:#ffb300;--bg:#05060a;--card:rgba(13,16,26,.72);}
*{box-sizing:border-box;}
body{font-family:'Segoe UI',system-ui,sans-serif;color:#e8eaf0;margin:0;padding:0 22px 60px;
  min-height:100vh;position:relative;background:radial-gradient(1200px 600px at 50% -10%,#10131f 0%,var(--bg) 60%);}
body::before{content:"";position:fixed;inset:-50%;z-index:0;pointer-events:none;
  background-image:linear-gradient(rgba(0,255,255,.05) 1px,transparent 1px),linear-gradient(90deg,rgba(255,0,255,.05) 1px,transparent 1px);
  background-size:42px 42px;transform:perspective(400px) rotateX(60deg);animation:scroll 14s linear infinite;}
@keyframes scroll{from{background-position:0 0}to{background-position:0 42px}}
.wrap{position:relative;z-index:1;max-width:1100px;margin:0 auto;}
.logo{text-align:center;font-weight:800;font-size:28px;letter-spacing:5px;margin:30px 0 2px;color:#fff;
  text-shadow:0 0 6px var(--neon),0 0 14px var(--neon),0 0 28px var(--neon2),0 0 48px var(--neon2);}
.logo .bolt{color:var(--neon);}
.subt{text-align:center;color:#8a93a8;font-size:12px;letter-spacing:2px;text-transform:uppercase;margin:0 0 20px;}
a.btn{margin:6px 8px 6px 0;padding:8px 14px;background:rgba(255,255,255,.04);border:1px solid rgba(0,255,255,.3);
  border-radius:9px;text-decoration:none;color:#cfefff;display:inline-block;font-size:13px;transition:.18s;}
a.btn:hover{border-color:var(--neon);box-shadow:0 0 16px rgba(0,255,255,.35);color:#fff;}
a.btn.alt{border-color:rgba(255,0,255,.4);color:#ffd6ff;}
a.btn.alt:hover{box-shadow:0 0 16px rgba(255,0,255,.4);}
a.btn.dl{border-color:rgba(63,224,138,.5);color:#9bf3c2;}
a.btn.dl:hover{box-shadow:0 0 16px rgba(63,224,138,.4);}
/* numbered top navigation (repeated on every page) */
.topnav{display:flex;flex-wrap:wrap;gap:8px;margin:4px 0 16px;padding:8px 10px;border:1px solid rgba(0,255,255,.18);
  border-radius:12px;background:var(--card);backdrop-filter:blur(8px);box-shadow:0 0 22px rgba(0,255,255,.06);}
.navbtn{position:relative;display:inline-flex;align-items:center;gap:7px;padding:7px 14px;border-radius:9px;text-decoration:none;
  font-size:13px;color:#cfefff;border:1px solid rgba(0,255,255,.22);background:rgba(255,255,255,.03);transition:.16s;}
.navbtn:hover{border-color:var(--neon);box-shadow:0 0 14px rgba(0,255,255,.3);color:#fff;}
.navbtn.dl{border-color:rgba(63,224,138,.4);color:#9bf3c2;}
.navbtn.dl:hover{border-color:#3fe08a;box-shadow:0 0 14px rgba(63,224,138,.35);}
.navbtn .num{font-weight:800;color:var(--neon);letter-spacing:.5px;font-variant-numeric:tabular-nums;
  text-shadow:0 0 8px rgba(0,255,255,.5);}
.navbtn.dl .num{color:#3fe08a;text-shadow:0 0 8px rgba(63,224,138,.5);}
/* current page: button stays lit */
.navbtn.active{color:#fff;border-color:var(--neon);background:rgba(0,255,255,.16);
  box-shadow:0 0 18px rgba(0,255,255,.5),inset 0 0 14px rgba(0,255,255,.14);font-weight:600;}
.navbtn.active .num{color:#fff;text-shadow:0 0 11px rgba(0,255,255,.95);}
.navbtn.dl.active{border-color:#3fe08a;background:rgba(63,224,138,.16);
  box-shadow:0 0 18px rgba(63,224,138,.5),inset 0 0 14px rgba(63,224,138,.14);}
.navbtn.dl.active .num{color:#eafff4;text-shadow:0 0 11px rgba(63,224,138,.95);}
/* hover fly-by: 3-line description per button */
.navbtn .tip{position:absolute;top:calc(100% + 9px);left:0;z-index:60;width:max-content;max-width:250px;
  padding:9px 12px;border-radius:9px;font-size:11.5px;line-height:1.55;font-weight:400;letter-spacing:0;
  color:#bfe9ff;text-align:left;white-space:normal;background:rgba(8,14,24,.97);border:1px solid var(--neon);
  box-shadow:0 0 20px rgba(0,255,255,.4);opacity:0;visibility:hidden;transform:translateY(-5px);
  transition:opacity .15s,transform .15s;pointer-events:none;}
.navbtn .tip b{display:block;margin-bottom:2px;color:#fff;font-weight:700;font-size:12px;text-shadow:0 0 8px rgba(0,255,255,.5);}
.navbtn .tip::before{content:"";position:absolute;bottom:100%;left:16px;border:6px solid transparent;border-bottom-color:var(--neon);}
.navbtn:hover .tip,.navbtn:focus-visible .tip{opacity:1;visibility:visible;transform:translateY(0);}
.navbtn.dl .tip{border-color:#3fe08a;box-shadow:0 0 20px rgba(63,224,138,.4);}
.navbtn.dl .tip::before{border-bottom-color:#3fe08a;}
h2{margin:18px 0 6px;text-shadow:0 0 10px rgba(0,255,255,.25);}
.meta{color:#9aa3b6;font-size:13px;line-height:1.55;margin:6px 0 4px;}
.meta b{color:#cfefff;}
.good{color:#3fe08a;font-weight:700;text-shadow:0 0 8px rgba(63,224,138,.5);}
.bad{color:#ff7a93;font-weight:700;}
.legend{background:var(--card);border:1px solid rgba(0,255,255,.22);border-radius:14px;padding:16px 20px;
  margin:16px 0 22px;backdrop-filter:blur(8px);box-shadow:0 0 30px rgba(0,255,255,.08);}
.legend h3{margin:0 0 12px;font-size:14px;letter-spacing:1px;color:#cfefff;text-transform:uppercase;}
.legend ul{margin:0;padding:0;list-style:none;display:grid;gap:11px;}
.legend li{font-size:13.5px;line-height:1.55;color:#c4cad8;}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;border:1px solid;font-size:12px;font-weight:700;
  letter-spacing:.5px;background:rgba(0,0,0,.25);text-transform:lowercase;vertical-align:baseline;}
/* DataTables dark overrides */
.dataTables_wrapper{color:#c4cad8;margin-top:10px;}
table.dataTable{border-collapse:collapse!important;width:100%;background:var(--card);border-radius:12px;overflow:hidden;
  box-shadow:0 0 30px rgba(0,255,255,.06);}
table.dataTable{font-size:13px;}
table.dataTable thead th{background:rgba(0,255,255,.07);color:#cfefff;border-bottom:1px solid rgba(0,255,255,.25);text-align:left;padding:5px 9px;font-size:12px;}
table.dataTable tbody td{border-top:1px solid rgba(255,255,255,.05);color:#dde2ec;padding:4px 9px;}
table.dataTable tbody tr{background:transparent;}
table.dataTable tbody tr:hover{background:rgba(0,255,255,.05);}
table.dataTable a{color:var(--neon);text-decoration:none;}
table.dataTable a:hover{text-shadow:0 0 8px var(--neon);}
.dataTables_filter input,.dataTables_length select{background:rgba(255,255,255,.05);border:1px solid rgba(0,255,255,.25);
  border-radius:7px;color:#fff;padding:5px 8px;}
.dataTables_paginate .paginate_button{color:#c4cad8!important;}
.dataTables_paginate .paginate_button.current{color:#fff!important;background:rgba(0,255,255,.15)!important;
  border:1px solid rgba(0,255,255,.4)!important;border-radius:6px;}
.dataTables_paginate .paginate_button:hover{color:#fff!important;background:rgba(255,0,255,.15)!important;border:0;}
/* custom neon search bar (ranking page) */
.searchbar{display:flex;align-items:center;gap:8px;margin:14px 0 12px;padding:6px 8px;max-width:560px;
  background:var(--card);border:1px solid rgba(0,255,255,.25);border-radius:12px;backdrop-filter:blur(8px);
  box-shadow:0 0 22px rgba(0,255,255,.07);transition:.18s;}
.searchbar:focus-within{border-color:var(--neon);box-shadow:0 0 28px rgba(0,255,255,.28);}
.searchbar .sicon{font-size:15px;opacity:.65;padding:0 2px 0 6px;}
.searchbar input{flex:1;min-width:0;background:transparent;border:0;outline:none;color:#fff;font-size:14px;padding:7px 4px;}
.searchbar input::placeholder{color:#6b7488;}
.searchbar input::-webkit-search-cancel-button{filter:invert(.6);cursor:pointer;}
.sbtn{border:0;cursor:pointer;font-size:13px;font-weight:700;letter-spacing:.5px;padding:8px 18px;border-radius:9px;
  color:#03121a;background:linear-gradient(90deg,var(--neon),var(--neon2));box-shadow:0 0 16px rgba(0,255,255,.35);
  transition:.18s;text-transform:uppercase;white-space:nowrap;}
.sbtn:hover{filter:brightness(1.1);box-shadow:0 0 24px rgba(255,0,255,.45);}
.sbtn.clear{background:transparent;border:1px solid rgba(255,255,255,.2);color:#9aa3b6;box-shadow:none;}
.sbtn.clear:hover{border-color:var(--neon2);color:#fff;box-shadow:0 0 14px rgba(255,0,255,.3);}
/* ranking table polish (scoped to #rank) */
#rank{font-variant-numeric:tabular-nums;border:1px solid rgba(0,255,255,.12);}
#rank thead th{position:sticky;top:0;z-index:2;background:#0a0f1a;color:#bdfdff;
  text-transform:uppercase;font-size:11px;letter-spacing:.6px;padding:8px 12px;
  border-bottom:1px solid rgba(0,255,255,.35);box-shadow:0 2px 8px rgba(0,0,0,.4);}
#rank tbody td{padding:5px 12px;border-top:1px solid rgba(255,255,255,.04);}
#rank tbody tr{transition:background .12s;}
#rank tbody tr.odd{background:rgba(255,255,255,.015);}
#rank tbody tr.even{background:rgba(0,255,255,.055);}
#rank tbody tr:hover{background:rgba(0,255,255,.12)!important;}
.aflag{font-size:.95em;margin-left:5px;cursor:help;opacity:.9;}
#rank td:nth-child(1),#rank th:nth-child(1){text-align:right;color:#7f8aa3;width:50px;}
#rank td:nth-child(2){font-weight:600;}
#rank td:nth-child(3),#rank th:nth-child(3){text-align:center;width:54px;}
#rank td:nth-child(4),#rank th:nth-child(4),#rank td:nth-child(5),#rank th:nth-child(5),
#rank td:nth-child(6),#rank th:nth-child(6),#rank td:nth-child(7),#rank th:nth-child(7),
#rank td:nth-child(8),#rank th:nth-child(8){text-align:right;}
#rank td:nth-child(9),#rank th:nth-child(9),#rank td:nth-child(10),#rank th:nth-child(10){text-align:center;}
.tv{text-decoration:none;font-size:15px;opacity:.8;transition:.15s;}
.tv:hover{opacity:1;filter:drop-shadow(0 0 6px var(--neon));}
.chip{display:inline-block;padding:2px 11px;border-radius:999px;font-size:11px;font-weight:700;letter-spacing:.5px;border:1px solid;}
.chip.good{color:#3fe08a;border-color:rgba(63,224,138,.55);background:rgba(63,224,138,.13);text-shadow:0 0 8px rgba(63,224,138,.4);}
.chip.bad{color:#ff7a93;border-color:rgba(255,90,110,.4);background:rgba(255,90,110,.08);}
.yes{color:#3fe08a;font-weight:700;text-shadow:0 0 8px rgba(63,224,138,.45);}
.no{color:#39414f;}
/* momentum cells */
td.up{color:#3fe08a;font-weight:600;}
td.down{color:#ff7a93;}
#rank td .muted{color:#7d8499;font-weight:600;}
.exch{display:inline-block;padding:1px 6px;margin:0 1px;border-radius:5px;font-size:10px;font-weight:700;letter-spacing:.3px;border:1px solid;}
.exch.b{color:#f3ba2f;border-color:rgba(243,186,47,.55);background:rgba(243,186,47,.09);}
.exch.m{color:#5ab0ff;border-color:rgba(90,176,255,.55);background:rgba(90,176,255,.09);}
.exch.hl{color:#b98cff;border-color:rgba(185,140,255,.55);background:rgba(185,140,255,.09);}
/* recent-variation dots: colour = direction, size = magnitude, white ≈ flat */
.dots{white-space:nowrap;}
.dot{display:inline-block;border-radius:50%;margin:0 2px;vertical-align:middle;}
.dot.s{width:7px;height:7px;}
.dot.m{width:11px;height:11px;}
.dot.l{width:16px;height:16px;}
.dot.g{background:#3fe08a;box-shadow:0 0 7px rgba(63,224,138,.75);}
.dot.r{background:#ff5a6e;box-shadow:0 0 7px rgba(255,90,110,.75);}
.dot.w{background:#e8edf5;box-shadow:0 0 5px rgba(232,237,245,.6);}
/* early-signal badges */
.sig{display:inline-block;padding:1px 5px;margin:1px;border-radius:4px;font-size:9.5px;font-weight:700;
  letter-spacing:.3px;color:#9fe7ff;border:1px solid rgba(0,255,255,.42);background:rgba(0,255,255,.08);}
/* market-regime banner (info only) — one panel, a header + a line per reference coin */
.regimebox{margin:0 0 16px;padding:10px 15px 11px;border-radius:12px;
  background:linear-gradient(90deg,rgba(243,186,47,.06),rgba(0,255,255,.04));border:1px solid rgba(243,186,47,.26);}
.regimecap{font-size:10px;letter-spacing:.7px;text-transform:uppercase;color:#7d8499;margin:0 0 7px 2px;}
.btcbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0;padding:3px 0;background:none;border:none;}
.btchead{padding-top:0;padding-bottom:5px;}
.btctitle{min-width:56px;display:inline-block;font-weight:800;letter-spacing:1px;}
.btcdot{display:inline-flex;flex-direction:column;align-items:center;gap:3px;min-width:30px;}
.btclab{font-size:9px;color:#8a91a3;letter-spacing:.3px;}
.btcsep{width:1px;height:18px;background:rgba(255,255,255,.14);margin:0 3px;}
.btcregime{font-weight:700;font-size:11px;padding:1px 10px;border-radius:999px;border:1px solid;margin-left:6px;}
.btcregime.up{color:#3fe08a;border-color:rgba(63,224,138,.55);background:rgba(63,224,138,.1);}
.btcregime.down{color:#ff7a93;border-color:rgba(255,90,110,.5);background:rgba(255,90,110,.07);}
.btcregime.mixed{color:#bdfdff;border-color:rgba(0,255,255,.4);}
/* shorts-specific */
.chip.short{color:#ff5a6e;border-color:rgba(255,90,110,.6);background:rgba(255,90,110,.14);text-shadow:0 0 8px rgba(255,90,110,.4);}
.warn{font-size:15px;cursor:help;filter:drop-shadow(0 0 5px rgba(255,179,0,.6));}
.warn.high{opacity:1;}
.warn.low{opacity:.55;font-size:13px;}
.risktoggle{display:inline-flex;align-items:center;gap:8px;margin:0 0 14px;padding:7px 13px;border-radius:999px;
  font-size:12.5px;color:#ffe2a6;background:rgba(255,179,0,.07);border:1px solid rgba(255,179,0,.32);cursor:pointer;user-select:none;}
.risktoggle input{accent-color:#ffb300;width:15px;height:15px;cursor:pointer;}
/* results page: two scorecards side by side */
.resgrid{display:grid;grid-template-columns:1fr 1fr;gap:28px;align-items:start;margin-top:6px;}
@media(max-width:1100px){.resgrid{grid-template-columns:1fr;}}
.resgrid h3{margin:0 0 4px;color:#cfefff;font-size:16px;letter-spacing:.5px;}
.spark{vertical-align:middle;}
.equity{display:block;width:100%;height:auto;background:rgba(0,255,255,.03);border:1px solid rgba(255,255,255,.07);border-radius:8px;}
.eqcap{font-size:11px;color:#7d8499;margin:2px 0 0;}
.legend{font-size:11.5px;line-height:1.7;color:#9aa3b8;margin:2px 0 14px;padding:9px 13px;
  background:rgba(0,255,255,.035);border:1px solid rgba(255,255,255,.08);border-radius:8px;}
.legend b{color:#cfefff;font-weight:600;}
.legend .sig,.legend .warn{vertical-align:middle;}
/* auth-status pill */
.authpill{display:inline-block;margin:8px 0 16px;padding:7px 14px;border-radius:999px;font-size:12.5px;
  background:rgba(0,255,255,.06);border:1px solid rgba(0,255,255,.28);color:#cfefff;}
.authpill.api{background:rgba(255,179,0,.06);border-color:rgba(255,179,0,.35);color:#ffe2a6;}
.authpill a{color:var(--neon2);text-decoration:none;font-weight:600;}
.authpill b{color:#fff;}
.authpill .muted{color:#7d8499;}
.role{font-size:11px;padding:1px 8px;border-radius:999px;border:1px solid rgba(0,255,255,.4);color:#bdfdff;}
.role.ro{border-color:rgba(255,179,0,.55);color:#ffe2a6;background:rgba(255,179,0,.07);}
/* nav cards + coin jump (landing page) */
.card{display:block;border:1px solid rgba(0,255,255,.22);border-radius:12px;padding:16px 18px;margin:12px 0;
  text-decoration:none;color:#e8eaf0;background:var(--card);backdrop-filter:blur(8px);transition:.18s;}
.card:hover{border-color:var(--neon);box-shadow:0 0 22px rgba(0,255,255,.18);}
.card h3{margin:0 0 6px;color:#cfefff;text-shadow:0 0 8px rgba(0,255,255,.25);}
.card p{margin:0;color:#9aa3b6;font-size:14px;}
.jump{margin:20px 0;padding:16px 18px;border:1px solid rgba(255,0,255,.25);border-radius:12px;background:var(--card);backdrop-filter:blur(8px);}
.jump select{padding:7px 10px;font-size:14px;border:1px solid rgba(0,255,255,.3);border-radius:7px;min-width:170px;
  background:rgba(255,255,255,.05);color:#fff;}
.jump button{padding:7px 16px;font-size:14px;border:0;border-radius:7px;cursor:pointer;color:#03121a;font-weight:700;
  background:linear-gradient(90deg,var(--neon),var(--neon2));box-shadow:0 0 16px rgba(0,255,255,.35);}
.jump strong{color:#cfefff;}
</style>
"""

def neon_logo(subtitle: str) -> str:
    return (f'<h1 class="logo"><span class="bolt">&#9889;</span> SCREENER</h1>'
            f'<p class="subt">{subtitle}</p>')


# Navigation structure (fixed): (url path, default label, default 3-line tooltip).
# The TEXT (label + tooltip lines) is editable without code via nav_tips.txt — see
# load_nav_tips(). These defaults are the fallback when that file is missing/incomplete.
NAV_ITEMS = [
    ("/", "Home", ("Dashboard home", "Rankings & momentum overview", "Jump to any section")),
    ("/combined", "Combined", ("Select on MEXC, compare Binance cost", "Cross-exchange shortlist", "Spread & fee aware")),
    ("/momentum", "Long", ("MEXC+HL universe × Binance 1h/2h/4h", "Coins in a real uptrend, not post-pump", "Early-detection leading signals")),
    ("/shorts", "Shorts", ("Weak, liquid perps to short", "Downtrend score + reversal-risk flags", "Funding / OI aware")),
    ("/results", "Results", ("Track record — were the calls right?", "Entry vs price · open + settled", "Equity curves & per-pick P&L")),
]
NAV_TIPS_FILE = Path(os.environ.get("SCREENER_NAV_TIPS", str(Path(__file__).resolve().parent / "nav_tips.txt")))


def load_nav_tips(path: Path) -> Dict[str, List[str]]:
    """Parse the editable nav text file → {url_path: [label, line1, line2, line3]}.

    Format: a "[/path]" header, then 4 text lines (label + 3 tooltip lines). Blank lines
    and lines starting with '#' are ignored. Returns {} on any read error (defaults apply).
    """
    out: Dict[str, List[str]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    key = None
    for raw in lines:
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[") and s.endswith("]"):
            key = s[1:-1].strip()
            out[key] = []
        elif key is not None:
            out[key].append(s)
    return out


def nav_bar(request: Request, token: str) -> str:
    """The numbered top navigation, identical on every page.

    Each button's label and its 3-line hover fly-by come from nav_tips.txt (live-reloaded
    on every request), falling back to the NAV_ITEMS defaults. All text is HTML-escaped, so
    the file can contain plain '&', '<', '×', etc.
    """
    overrides = load_nav_tips(NAV_TIPS_FILE)
    try:
        cur = request.url.path           # light up the button for the page we're on
    except Exception:
        cur = ""
    parts = []
    for i, (path, dlabel, dtips) in enumerate(NAV_ITEMS, 1):
        ov = overrides.get(path, [])
        label = html.escape(ov[0]) if len(ov) >= 1 and ov[0] else html.escape(dlabel)
        tips = [html.escape(ov[j + 1]) if len(ov) >= j + 2 and ov[j + 1] else html.escape(dtips[j])
                for j in range(3)]
        url = with_token(path, token)
        active = " active" if url.split("?", 1)[0] == cur else ""
        aria = ' aria-current="page"' if active else ""
        tip_html = f'<b>{tips[0]}</b><br>{tips[1]}<br>{tips[2]}'
        parts.append(
            f'<a class="navbtn{active}" href="{url}"{aria}>'
            f'<span class="num">-{i}-</span>{label}'
            f'<span class="tip" role="tooltip">{tip_html}</span></a>'
        )
    return f'<nav class="topnav">{"".join(parts)}</nav>'


BINANCE_RANKING_FILE = Path(__file__).resolve().parent / "binance_ranking.json"


# --- Endpoint: Full Binance futures ranking (whole universe) ---
@app.get("/binance-ranking", response_class=HTMLResponse)
async def binance_ranking(request: Request):
    if not is_authenticated(request):
        return login_redirect(request)
    token = link_token(request)
    home = with_token("/", token)
    if not BINANCE_RANKING_FILE.exists():
        return (f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
                f"<title>SCREENER &middot; Ranking</title>{DATA_PAGE_CSS}</head>"
                f"<body><div class='wrap'>{neon_logo('Full Binance Futures Ranking')}"
                f'<a href="{home}" class="btn">&#8962; Home</a>'
                f'<h2>No Binance ranking yet</h2>'
                f'<p class="meta">Run <code>python3 build_binance_ranking.py</code> to generate it.</p>'
                f"</div></body></html>")
    data = json.loads(BINANCE_RANKING_FILE.read_text())
    rows_html = []
    for r in data.get("rows", []):
        good = r["good"]
        bg = "rgba(63,224,138,.08)" if good else "rgba(255,90,110,.05)"
        flag = ('<span class="chip good">FILTER PASS</span>' if good else '<span class="chip bad">FILTER FAIL</span>')
        in_list = ('<span class="yes">&#10003;</span>' if r['in_pairlist'] else '<span class="no">&middot;</span>')
        vol = r.get("volatility_pct")
        vol_cell = f"<td data-order='{vol if vol is not None else -1}'>{vol:.2f}</td>" if vol is not None else "<td data-order='-1'>-</td>"
        qv = r["quote_volume"]
        rows_html.append(
            f"<tr>"
            f"<td>{r['rank']}</td>"
            f"<td>{coin_link(r['coin'], token)}{asset_icon(r.get('asset_type'))}</td>"
            f"<td>{tradingview_link(r['coin'])}</td>"
            f"<td>{r['spread_pct']:.4f}</td>"
            f"<td>{r['fee_roundtrip_pct']:.2f}</td>"
            f"<td>{r['total_cost_pct']:.4f}</td>"
            f"{vol_cell}"
            f"<td data-order='{qv:.0f}'>{qv/1e6:,.2f}M</td>"
            f"<td data-order='{1 if r['in_pairlist'] else 0}'>{in_list}</td>"
            f"<td data-order='{1 if good else 0}'>{flag}</td></tr>"
        )
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SCREENER &middot; Binance Ranking</title>
<link rel="stylesheet" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css">
{DATA_PAGE_CSS}
</head><body><div class="wrap">
{neon_logo("Full Binance Futures Ranking")}
{nav_bar(request, token)}
{auth_status_html(request)}
<h2>Full Binance Futures Ranking</h2>
<div class="searchbar">
  <span class="sicon">&#128269;</span>
  <input id="ranksearch" type="search" placeholder="Search coin, rank or value…" autocomplete="off" autofocus>
  <button id="searchbtn" class="sbtn" type="button">Search</button>
  <button id="clearbtn" class="sbtn clear" type="button">Clear</button>
</div>
<table id="rank" class="display" style="width:100%">
<thead><tr><th>Rank</th><th>Coin</th><th>Chart</th><th>Spread %</th><th>Fee RT %</th><th>Total Cost %</th>
<th>Volatility %</th><th>24h Vol</th><th>In List</th><th>Filter</th></tr></thead>
<tbody>
{''.join(rows_html)}
</tbody></table>
<p class="meta">All {data.get('total_symbols')} live USDⓈ-M PERPETUAL symbols, ranked by round-trip cost
(spread % + {data.get('fees',{}).get('roundtrip_taker_pct')}% fee).
<span class="chip good">FILTER PASS</span> = 24h volume &ge; {data.get('min_volume'):,.0f} USDT
AND spread &le; {data.get('max_spread_pct')}%
AND volatility &ge; {data.get('min_volatility_pct')}%.
<b>{data.get('count_good')}</b> of {data.get('total_symbols')} qualify.
Volatility = 24h (high-low)/avg %.<br>Generated {data.get('generated_utc')} UTC.</p>
<script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
<script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
<script>$(document).ready(function(){{
  var dt=$('#rank').DataTable({{"pageLength":100,"order":[[0,"asc"]],"dom":"lrtip",
    "columnDefs":[{{"orderable":false,"searchable":false,"targets":[2]}}]}});
  var box=document.getElementById('ranksearch');
  function doSearch(){{ dt.search(box.value).draw(); }}
  box.addEventListener('input', doSearch);
  box.addEventListener('keydown', function(e){{ if(e.key==='Enter'){{ e.preventDefault(); doSearch(); }} }});
  document.getElementById('searchbtn').addEventListener('click', doSearch);
  document.getElementById('clearbtn').addEventListener('click', function(){{ box.value=''; doSearch(); box.focus(); }});
}});</script>
</div></body></html>"""
    return html


# --- Endpoint: the "good" coins as a downloadable freqtrade pairlist ---
@app.get("/binance-good-pairs.json")
async def binance_good_pairs(request: Request):
    require_full_access(request)
    if not BINANCE_RANKING_FILE.exists():
        raise HTTPException(status_code=404, detail="Ranking not generated yet.")
    data = json.loads(BINANCE_RANKING_FILE.read_text())
    pairs = [f"{r['coin']}/USDT:USDT" for r in data.get("rows", []) if r.get("good")]
    return JSONResponse({"pairs": pairs, "count": len(pairs), "generated_utc": data.get("generated_utc")})


MEXC_RANKING_FILE = Path(__file__).resolve().parent / "mexc_ranking.json"


def _ranking_missing_page(token: str, title: str, script: str) -> str:
    home = with_token("/", token)
    return (f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            f"<title>SCREENER &middot; Ranking</title>{DATA_PAGE_CSS}</head>"
            f"<body><div class='wrap'>{neon_logo(title)}"
            f'<a href="{home}" class="btn">&#8962; Home</a>'
            f'<h2>No ranking yet</h2>'
            f'<p class="meta">Run <code>python3 {script}</code> to generate it.</p>'
            f"</div></body></html>")


# --- Endpoint: Full MEXC futures ranking (whole universe, public API, no key) ---
@app.get("/mexc-ranking", response_class=HTMLResponse)
async def mexc_ranking(request: Request):
    if not is_authenticated(request):
        return login_redirect(request)
    token = link_token(request)
    if not MEXC_RANKING_FILE.exists():
        return _ranking_missing_page(token, "Full MEXC Futures Ranking", "build_mexc_ranking.py")
    data = json.loads(MEXC_RANKING_FILE.read_text())
    rows_html = []
    for r in data.get("rows", []):
        good = r["good"]
        bg = "rgba(63,224,138,.08)" if good else "rgba(255,90,110,.05)"
        flag = ('<span class="chip good">FILTER PASS</span>' if good else '<span class="chip bad">FILTER FAIL</span>')
        vol = r.get("volatility_pct")
        vol_cell = f"<td data-order='{vol if vol is not None else -1}'>{vol:.2f}</td>" if vol is not None else "<td data-order='-1'>-</td>"
        qv = r["quote_volume"]
        rows_html.append(
            f"<tr>"
            f"<td>{r['rank']}</td>"
            f"<td>{coin_link(r['coin'], token)}{asset_icon(r.get('asset_type'))}</td>"
            f"<td>{tradingview_link(r['coin'])}</td>"
            f"<td>{r['spread_pct']:.4f}</td>"
            f"<td>{r['fee_roundtrip_pct']:.2f}</td>"
            f"<td>{r['total_cost_pct']:.4f}</td>"
            f"{vol_cell}"
            f"<td data-order='{qv:.0f}'>{qv/1e6:,.2f}M</td>"
            f"<td data-order='{1 if good else 0}'>{flag}</td></tr>"
        )
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SCREENER &middot; MEXC Ranking</title>
<link rel="stylesheet" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css">
{DATA_PAGE_CSS}
</head><body><div class="wrap">
{neon_logo("Full MEXC Futures Ranking")}
{nav_bar(request, token)}
{auth_status_html(request)}
<h2>Full MEXC Futures Ranking</h2>
<div class="searchbar">
  <span class="sicon">&#128269;</span>
  <input id="ranksearch" type="search" placeholder="Search coin, rank or value…" autocomplete="off" autofocus>
  <button id="searchbtn" class="sbtn" type="button">Search</button>
  <button id="clearbtn" class="sbtn clear" type="button">Clear</button>
</div>
<table id="rank" class="display" style="width:100%">
<thead><tr><th>Rank</th><th>Coin</th><th>Chart</th><th>Spread %</th><th>Fee RT %</th><th>Total Cost %</th>
<th>Volatility %</th><th>24h Vol</th><th>Filter</th></tr></thead>
<tbody>
{''.join(rows_html)}
</tbody></table>
<p class="meta">All {data.get('total_symbols')} live USDT-margined perpetuals (MEXC public API, no key),
ranked by round-trip cost (spread % + {data.get('fees',{}).get('roundtrip_taker_pct')}% fee).
<span class="chip good">FILTER PASS</span> = 24h volume &ge; {data.get('min_volume'):,.0f} USDT
AND spread &le; {data.get('max_spread_pct')}%
AND volatility &ge; {data.get('min_volatility_pct')}%.
<b>{data.get('count_good')}</b> of {data.get('total_symbols')} qualify.
Coin links go to the Binance data summary (backtest CSVs).<br>Generated {data.get('generated_utc')} UTC.</p>
<script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
<script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
<script>$(document).ready(function(){{
  var dt=$('#rank').DataTable({{"pageLength":100,"order":[[0,"asc"]],"dom":"lrtip",
    "columnDefs":[{{"orderable":false,"searchable":false,"targets":[2]}}]}});
  var box=document.getElementById('ranksearch');
  function doSearch(){{ dt.search(box.value).draw(); }}
  box.addEventListener('input', doSearch);
  box.addEventListener('keydown', function(e){{ if(e.key==='Enter'){{ e.preventDefault(); doSearch(); }} }});
  document.getElementById('searchbtn').addEventListener('click', doSearch);
  document.getElementById('clearbtn').addEventListener('click', function(){{ box.value=''; doSearch(); box.focus(); }});
}});</script>
</div></body></html>"""
    return html


@app.get("/mexc-good-pairs.json")
async def mexc_good_pairs(request: Request):
    require_full_access(request)
    if not MEXC_RANKING_FILE.exists():
        raise HTTPException(status_code=404, detail="MEXC ranking not generated yet.")
    data = json.loads(MEXC_RANKING_FILE.read_text())
    pairs = [f"{r['coin']}/USDT:USDT" for r in data.get("rows", []) if r.get("good")]
    return JSONResponse({"pairs": pairs, "count": len(pairs), "generated_utc": data.get("generated_utc")})


# --- Endpoint: Momentum screener (MEXC+HL universe × Binance 1h/2h/4h) ---
MOMENTUM_FILE = Path(__file__).resolve().parent / "momentum_ranking.json"


def _trend_pill(up) -> str:
    if up is True:
        return '<span class="yes">&#9650;</span>'
    if up is False:
        return '<span class="no">&#9660;</span>'
    return '<span class="no">&middot;</span>'


def _exch_badges(exchanges) -> str:
    """B / M / HL badges for the exchanges a coin is listed on (Binance, MEXC, Hyperliquid)."""
    labels = {"binance": ("b", "B", "Binance"), "mexc": ("m", "M", "MEXC"), "hl": ("hl", "HL", "Hyperliquid")}
    have = exchanges or []
    parts = [f'<span class="exch {c}" title="{name}">{txt}</span>'
             for key, (c, txt, name) in labels.items() if key in have]
    return "".join(parts) if parts else '<span class="no">&mdash;</span>'


def _recent_dots(recent) -> str:
    """A strip of dots for the recent windows (5/15/30/45m): green up, red down, white ~flat;
    dot size grows with the size of the move."""
    if not recent:
        return '<span class="no">&mdash;</span>'
    order = sorted(recent.keys(), key=lambda k: int(k.rstrip("m")))
    dots = []
    for k in order:
        v = recent.get(k)
        if v is None:
            dots.append(f'<span class="dot w s" title="{k}: n/a"></span>')
            continue
        a = abs(v)
        color = "w" if a < 0.1 else ("g" if v > 0 else "r")
        size = "s" if (color == "w" or a < 0.3) else ("m" if a < 1.0 else "l")
        dots.append(f'<span class="dot {color} {size}" title="{k}: {v:+.2f}%"></span>')
    return f'<span class="dots">{"".join(dots)}</span>'


def _buy_cell(br) -> str:
    """Taker-buy share as a %: green when buyers dominate, red when sellers do."""
    if br is None:
        return "<td data-order='-1'>-</td>"
    cls = "up" if br >= 0.55 else ("down" if br <= 0.45 else "")
    return f"<td data-order='{br}' class='{cls}'>{br * 100:.0f}%</td>"


def _rvol_cell(rv) -> str:
    """Relative volume (×baseline): green on a surge."""
    if rv is None:
        return "<td data-order='-1'>-</td>"
    cls = "up" if rv >= 1.8 else ""
    return f"<td data-order='{rv}' class='{cls}'>{rv:.1f}&times;</td>"


def _early_cell(r) -> str:
    """EARLY chip + a badge per leading signal that fired (hover for the value)."""
    sigs = r.get("early_signals") or []
    if not sigs:
        return "<td data-order='0'><span class='no'>&mdash;</span></td>"
    labels = {
        "buy": ("BUY", f"taker-buy {(r.get('buy_ratio') or 0) * 100:.0f}%"),
        "vol": ("VOL", f"rvol {r.get('rvol')}x"),
        "accel": ("ACC", f"1h accelerating {r.get('accel_1h')}pp"),
        "brk": ("BRK", "new breakout high"),
        "oi": ("OI&#9650;", f"open interest {r.get('oi_change')}%"),
        "fund": ("F", f"funding {r.get('funding')}"),
    }
    badges = "".join(
        f'<span class="sig" title="{labels.get(k, (k, k))[1]}">{labels.get(k, (k, k))[0]}</span>'
        for k in sigs
    )
    chip = '<span class="chip good">EARLY</span> ' if r.get("early") else ''
    return f"<td data-order='{len(sigs)}'>{chip}{badges}</td>"


def _btc_dot(v) -> str:
    if v is None:
        return '<span class="dot w s"></span>'
    a = abs(v)
    color = "w" if a < 0.1 else ("g" if v > 0 else "r")
    size = "s" if (color == "w" or a < 0.3) else ("m" if a < 1.0 else "l")
    return f'<span class="dot {color} {size}" title="{v:+.2f}%"></span>'


_REGIME_COLS = ["5m", "15m", "30m", "45m", "|", "1h", "2h", "4h"]
_REGIME_COLORS = {"BTC": "#f3ba2f", "ETH": "#7b9cff", "HYPE": "#4be0c0", "ZEC": "#e0b44a"}


def _regime_label(rg) -> tuple:
    longs = [rg.get(t) for t in ("1h", "2h", "4h") if rg.get(t) is not None]
    if not longs:
        return "mixed", "n/a"
    pos = sum(1 for x in longs if x > 0)
    neg = sum(1 for x in longs if x < 0)
    if pos == len(longs):
        return "up", "Risk-on &#9650;"
    if neg == len(longs):
        return "down", "Risk-off &#9660;"
    return "mixed", "Mixed"


def _regime_banner(regime) -> str:
    """A stacked dot strip — one line per reference coin (BTC/ETH/HYPE/ZEC) across the same
    windows (5/15/30/45m + 1h/2h/4h). Market-regime context only, not a filter."""
    if not regime:
        return ""
    header = ('<div class="btcbar btchead"><span class="btctitle"></span>'
              + "".join('<span class="btcsep"></span>' if k == "|"
                        else f'<span class="btcdot"><span class="btclab">{k}</span></span>'
                        for k in _REGIME_COLS) + '</div>')
    lines = []
    for coin, rg in regime.items():
        rg = rg or {}
        color = _REGIME_COLORS.get(coin.upper(), "#bdfdff")
        dots = "".join('<span class="btcsep"></span>' if k == "|"
                       else f'<span class="btcdot">{_btc_dot(rg.get(k))}</span>'
                       for k in _REGIME_COLS)
        rcls, rtxt = _regime_label(rg)
        lines.append(f'<div class="btcbar"><span class="btctitle" style="color:{color}">{coin}</span>'
                     f'{dots}<span class="btcregime {rcls}">{rtxt}</span></div>')
    return (f'<div class="regimebox"><div class="regimecap">Market regime &middot; context only</div>'
            f'{header}{"".join(lines)}</div>')


def _regime_gate_banner(data: dict) -> str:
    """Show regime gate status: current BTC 2h ROC, floor/ceiling thresholds, and whether blocked."""
    cfg = data.get("config", {})
    if not cfg.get("regime_gate"):
        return ""
    regime_blocked = data.get("regime_blocked", False)
    thresh = cfg.get("regime_gate_threshold_pct", 0.75)
    floor = cfg.get("regime_gate_floor_pct", -1.5)
    btc_rg = data.get("regime", {}).get("BTC", {})
    btc_2h = btc_rg.get("2h")

    if btc_2h is None:
        roc_html = '<span style="color:#7d8499">BTC 2h ROC: n/a</span>'
        status_cls, status_txt = "mixed", "GATE: no data"
    elif regime_blocked:
        roc_html = f'<span style="color:#ff7a93;font-weight:700">BTC 2h ROC: {btc_2h:+.2f}%</span>'
        reason = f"above ceiling ({thresh:+.2f}%)" if btc_2h > thresh else f"below floor ({floor:+.2f}%)"
        status_cls, status_txt = "down", f"&#128683; GATE ACTIVE — {reason} — picks suppressed"
    else:
        color = "#3fe08a" if btc_2h >= 0 else "#bdfdff"
        roc_html = f'<span style="color:{color};font-weight:700">BTC 2h ROC: {btc_2h:+.2f}%</span>'
        pct_to_ceil = thresh - btc_2h
        pct_to_floor = btc_2h - floor
        margin = min(pct_to_ceil, pct_to_floor)
        status_cls, status_txt = "up", f"&#9989; Gate open &middot; {margin:.2f}% margin to nearest threshold"

    return (f'<div class="regimebox" style="margin-bottom:8px">'
            f'<div class="regimecap">Short regime gate &middot; floor {floor:+.2f}% / ceiling {thresh:+.2f}%</div>'
            f'<div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">'
            f'{roc_html}'
            f'<span class="btcregime {status_cls}" style="font-size:12px;padding:2px 12px">{status_txt}</span>'
            f'</div></div>')


def _gen_epoch(iso) -> Optional[float]:
    """Parse a 'YYYY-MM-DDThh:mm:ssZ' UTC string to an epoch (seconds); None if unparseable."""
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _roc_cell(val) -> str:
    if val is None:
        return "<td data-order='-1e9'>-</td>"
    cls = "up" if val > 0 else ("down" if val < 0 else "")
    return f"<td data-order='{val}' class='{cls}'>{val:+.1f}</td>"


@app.get("/momentum", response_class=HTMLResponse)
async def momentum_page(request: Request):
    if not is_authenticated(request):
        return login_redirect(request)
    token = link_token(request)
    home = with_token("/", token)
    if not MOMENTUM_FILE.exists():
        return (f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
                f"<title>SCREENER &middot; Momentum</title>{DATA_PAGE_CSS}</head>"
                f"<body><div class='wrap'>{neon_logo('Momentum — MEXC+HL Universe × Binance')}"
                f'<a href="{home}" class="btn">&#8962; Home</a>'
                f'<h2>No momentum data yet</h2>'
                f'<p class="meta">Run <code>python3 build_momentum.py</code> to generate it.</p>'
                f"</div></body></html>")
    data = json.loads(MOMENTUM_FILE.read_text())
    cfg = data.get("config", {})
    w = cfg.get("weights", {})
    gen = data.get("generated_utc")
    gen_ts = _gen_epoch(gen)
    age_min = int(max(0, (datetime.now(timezone.utc).timestamp() - gen_ts) / 60)) if gen_ts else None
    age_txt = str(age_min) if age_min is not None else "?"
    rows_html = []
    # Show only confirmed UPTREND longs — rejected trending coins (incl. post-pump faders
    # like a coin dropping now, score < min) stay in momentum_ranking.json but off the board.
    long_rows = [r for r in data.get("rows", []) if r.get("momentum")]
    for r in long_rows:
        mom = r.get("momentum")
        if r.get("market") == "none" or r.get("score") is None:
            bg = "rgba(125,132,153,.05)"
        else:
            bg = "rgba(63,224,138,.10)" if mom else "rgba(255,90,110,.04)"
        score = r.get("score")
        score_cell = (f"<td data-order='{score}'><b>{score:.2f}</b></td>"
                      if score is not None else "<td data-order='-1e9'>-</td>")
        roc = r.get("roc", {})
        ext = r.get("extension_1h")
        ext_cell = (f"<td data-order='{ext}'>{ext:+.1f}</td>" if ext is not None
                    else "<td data-order='-1e9'>-</td>")
        if mom:
            flag = '<span class="chip good">UPTREND</span>'
        elif r.get("market") == "none" or score is None:
            flag = f'<span class="chip bad" title="{r.get("reason","")}">n/a</span>'
        else:
            flag = f'<span class="chip bad" title="{r.get("reason","")}">{r.get("reason","no")}</span>'
        mkt = r.get("market", "none")
        mkt_cell = ('<span class="yes">fut</span>' if mkt == "futures"
                    else ('<span class="muted">spot</span>' if mkt == "spot"
                          else '<span class="no">&mdash;</span>'))
        chart = tradingview_link(r["coin"]) if mkt != "none" else "&middot;"
        rows_html.append(
            f"<tr style='background:{bg}'>"
            f"<td>{coin_link(r['coin'], token)}</td>"
            f"<td>{chart}</td>"
            f"{_roc_cell(r.get('change24'))}"
            f"{score_cell}"
            f"<td data-order='{sum(v for v in (r.get('recent') or {}).values() if v is not None):.3f}'>{_recent_dots(r.get('recent'))}</td>"
            f"{_buy_cell(r.get('buy_ratio'))}"
            f"{_rvol_cell(r.get('rvol'))}"
            f"{_early_cell(r)}"
            f"{_roc_cell(roc.get('1h'))}"
            f"{_roc_cell(roc.get('2h'))}"
            f"{_roc_cell(roc.get('4h'))}"
            f"{ext_cell}"
            f"<td data-order='{1 if r.get('trend',{}).get('4h') else 0}'>{_trend_pill(r.get('trend',{}).get('4h'))}</td>"
            f"<td>{mkt_cell}</td>"
            f"<td data-order='{len(r.get('exchanges') or [])}'>{_exch_badges(r.get('exchanges'))}</td>"
            f"<td data-order='{1 if mom else 0}'>{flag}</td></tr>"
        )
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SCREENER &middot; Momentum</title>
<link rel="stylesheet" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css">
{DATA_PAGE_CSS}
</head><body><div class="wrap">
{neon_logo("Momentum — MEXC+HL Universe × Binance 1h/2h/4h")}
{nav_bar(request, token)}
{auth_status_html(request)}
<h2>Momentum — strongest coins in a real uptrend</h2>
{_regime_banner(data.get("regime"))}
<div class="searchbar">
  <span class="sicon">&#128269;</span>
  <input id="momentumsearch" type="search" placeholder="Search coin, rank or value…" autocomplete="off" autofocus>
  <button id="searchbtn" class="sbtn" type="button">Search</button>
  <button id="clearbtn" class="sbtn clear" type="button">Clear</button>
</div>
<table id="rank" class="display" style="width:100%">
<thead><tr><th>Coin</th><th>Chart</th><th>24h&nbsp;%</th><th>Score</th><th>Recent<br>5·15·30·45m</th><th>Buy%</th><th>RVOL</th><th>Early</th><th>1h&nbsp;%</th><th>2h&nbsp;%</th><th>4h&nbsp;%</th>
<th>Ext&nbsp;%</th><th>4h&nbsp;Trend</th><th>Mkt</th><th>Exchanges</th><th>Momentum</th></tr></thead>
<tbody>
{''.join(rows_html)}
</tbody></table>
<p class="meta">Top {data.get('total')} perps from the MEXC+HL universe, scored on Binance
<b>1h/2h/4h</b> candles (weights {w.get('1h')}/{w.get('2h')}/{w.get('4h')}, strong on 1h)
plus a small recent bucket ({w.get('recent')}) and a 5–15m acceleration term.
<span class="chip good">UPTREND</span> = composite score &ge; {cfg.get('min_score')}, 1h rising,
NOT overextended (1h &le; {cfg.get('max_extension_pct')}% above its EMA{cfg.get('ema_slow')}),
no single-bar spike (&le; {cfg.get('max_single_bar_pct')}%), 4h trend confirms, and
no recent 15m dump (&gt; {cfg.get('max_recent_drop_pct')}%) — i.e.
a genuine climb, <b>not a post-pump</b> top. <b>{data.get('count_momentum')}</b> qualify now &mdash;
only these confirmed UPTRENDs are listed (others are scored but hidden).
Ext% = how far 1h price sits above its mean (high = stretched).
Exchanges = listed on <span class="exch b">B</span>inance / <span class="exch m">M</span>EXC /
<span class="exch hl">HL</span> Hyperliquid.
Recent dots (5·15·30·45 min): <span class="dot g m"></span>&nbsp;up
<span class="dot r m"></span>&nbsp;down <span class="dot w s"></span>&nbsp;~flat — bigger dot = bigger move (hover for %).<br>
<b>Early-detection</b> (leading signals): <b>Buy%</b> = taker-buy share (aggressive demand),
<b>RVOL</b> = volume vs baseline (a surge often precedes the move), and the <b>Early</b> column
flags confluence — <span class="sig">BUY</span><span class="sig">VOL</span> demand/volume,
<span class="sig">ACC</span> accelerating, <span class="sig">BRK</span> new-high breakout,
<span class="sig">OI&#9650;</span> open-interest rising, <span class="sig">F</span> funding not crowded;
<span class="chip good">EARLY</span> = {cfg.get('early_min_signals')}+ fired (hover each for the value).<br>
Generated {gen} UTC · <b id="dataage">{age_txt}</b> min old
<span class="muted">(auto-refreshes every 5 min)</span>.</p>
<script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
<script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
<script>$(document).ready(function(){{
  var dt=$('#rank').DataTable({{"pageLength":100,"order":[[4,"desc"]],"dom":"lrtip",
    "columnDefs":[{{"orderable":false,"searchable":false,"targets":[2]}}]}});
  var gts={int(gen_ts) if gen_ts else 0}, ageEl=document.getElementById('dataage');
  if(gts && ageEl){{ function upd(){{ ageEl.textContent=Math.max(0,Math.floor((Date.now()/1000-gts)/60)); }} upd(); setInterval(upd,30000); }}
  var box=document.getElementById('momentumsearch');
  function doSearch(){{ dt.search(box.value).draw(); }}
  box.addEventListener('input', doSearch);
  box.addEventListener('keydown', function(e){{ if(e.key==='Enter'){{ e.preventDefault(); doSearch(); }} }});
  document.getElementById('searchbtn').addEventListener('click', doSearch);
  document.getElementById('clearbtn').addEventListener('click', function(){{ box.value=''; doSearch(); box.focus(); }});
}});</script>
</div></body></html>"""
    return html


@app.get("/momentum.json")
async def momentum_json(request: Request):
    require_api_auth(request)
    if not MOMENTUM_FILE.exists():
        raise HTTPException(status_code=404, detail="Momentum data not generated yet.")
    return JSONResponse(json.loads(MOMENTUM_FILE.read_text()))


# --- Endpoint: Shorts screener (weakest perps on MEXC / HL) ---
SHORTS_FILE = Path(__file__).resolve().parent / "shorts_ranking.json"


def _sell_cell(br) -> str:
    """Aggressive-sell share (= 1 - taker-buy): green when sellers dominate (good for a short)."""
    if br is None:
        return "<td data-order='-1'>-</td>"
    cls = "up" if br <= 0.45 else ("down" if br >= 0.55 else "")
    return f"<td data-order='{1 - br}' class='{cls}'>{(1 - br) * 100:.0f}%</td>"


def _rsi_cell(v) -> str:
    if v is None:
        return "<td data-order='-1'>-</td>"
    cls = "down" if v < 30 else ""    # oversold = bounce caution
    return f"<td data-order='{v}' class='{cls}'>{v:.0f}</td>"


def _funding_cell(f) -> str:
    if f is None:
        return "<td data-order='-1e9'>-</td>"
    cls = "down" if f < 0 else "up"   # negative funding = crowded short = caution
    return f"<td data-order='{f}' class='{cls}'>{f * 100:+.3f}%</td>"


def _spread_cell(v) -> str:
    """MEXC bid/ask spread % at the scan (None for HL-only coins, which skip the gate)."""
    if v is None:
        return "<td data-order='999' class='muted' title='no MEXC book — spread gate skipped'>&mdash;</td>"
    return f"<td data-order='{v}'>{v:.3f}</td>"


def _short_breakdown_cell(r) -> str:
    sigs = r.get("breakdown_signals") or []
    if not sigs:
        return "<td data-order='0'><span class='no'>&mdash;</span></td>"
    labels = {
        "sell": ("SELL", f"taker-sell {round((1 - (r.get('buy_ratio') or 0)) * 100)}%"),
        "vol": ("VOL", f"rvol {r.get('rvol')}x"),
        "accel": ("ACC&#9660;", f"1h accelerating down {r.get('accel_1h')}pp"),
        "brk": ("BRK&#9660;", "new-low breakdown"),
        "oi": ("OI&#9650;", f"OI {r.get('oi_change')}%"),
        "fund": ("F", f"funding {r.get('funding')}"),
    }
    badges = "".join(f'<span class="sig" title="{labels.get(k, (k, k))[1]}">{labels.get(k, (k, k))[0]}</span>'
                     for k in sigs)
    return f"<td data-order='{len(sigs)}'>{badges}</td>"


def _risk_icon(r) -> str:
    lvl = r.get("reversal_risk", "none")
    reasons = r.get("risk_reasons") or []
    if lvl == "none" or not reasons:
        return "<td data-order='0'><span class='no'>&mdash;</span></td>"
    order = 2 if lvl == "high" else 1
    return f"<td data-order='{order}'><span class=\"warn {lvl}\" title=\"{'; '.join(reasons)}\">&#9888;</span></td>"


@app.get("/shorts", response_class=HTMLResponse)
async def shorts_page(request: Request):
    if not is_authenticated(request):
        return login_redirect(request)
    token = link_token(request)
    home = with_token("/", token)
    if not SHORTS_FILE.exists():
        return (f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
                f"<title>SCREENER &middot; Shorts</title>{DATA_PAGE_CSS}</head>"
                f"<body><div class='wrap'>{neon_logo('Shorts — weakest perps')}"
                f'<a href="{home}" class="btn">&#8962; Home</a>'
                f'<h2>No shorts data yet</h2>'
                f'<p class="meta">Run <code>python3 build_shorts.py</code> to generate it.</p>'
                f"</div></body></html>")
    data = json.loads(SHORTS_FILE.read_text())
    cfg = data.get("config", {})
    gen = data.get("generated_utc")
    gen_ts = _gen_epoch(gen)
    age_min = int(max(0, (datetime.now(timezone.utc).timestamp() - gen_ts) / 60)) if gen_ts else None
    age_txt = str(age_min) if age_min is not None else "?"
    rows_html = []
    # Show only confirmed SHORTs — rejected near-misses (incl. coins that are actually
    # rising, like a bounce that scored < min) are kept in the JSON but not on the board.
    short_rows = [r for r in data.get("rows", []) if r.get("short")]
    for r in short_rows:
        is_short = r.get("short")
        risk = r.get("reversal_risk", "none")
        bg = "rgba(255,90,110,.10)" if is_short else "rgba(125,132,153,.04)"
        sc = r.get("short_score")
        score_cell = (f"<td data-order='{sc}'><b>{sc:.2f}</b></td>"
                      if sc is not None else "<td data-order='-1e9'>-</td>")
        roc = r.get("roc", {})
        if is_short:
            flag = '<span class="chip short">SHORT</span>'
        elif sc is None:
            flag = f'<span class="chip bad" title="{r.get("reason", "")}">n/a</span>'
        else:
            flag = f'<span class="chip bad" title="{r.get("reason", "")}">{r.get("reason", "no")}</span>'
        chart = tradingview_link(r["coin"]) if r.get("data_src", "none") != "none" else "&middot;"
        rec_order = sum(v for v in (r.get('recent') or {}).values() if v is not None)
        rows_html.append(
            f"<tr data-risk='{risk}' style='background:{bg}'>"
            f"<td>{r.get('rank', '')}</td>"
            f"<td>{coin_link(r['coin'], token)}</td>"
            f"<td>{chart}</td>"
            f"{score_cell}"
            f"<td data-order='{rec_order:.3f}'>{_recent_dots(r.get('recent'))}</td>"
            f"{_roc_cell(r.get('change24'))}"
            f"{_sell_cell(r.get('buy_ratio'))}"
            f"{_rvol_cell(r.get('rvol'))}"
            f"{_short_breakdown_cell(r)}"
            f"{_roc_cell(roc.get('1h'))}{_roc_cell(roc.get('2h'))}{_roc_cell(roc.get('4h'))}"
            f"{_rsi_cell(r.get('rsi'))}"
            f"{_funding_cell(r.get('funding'))}"
            f"{_spread_cell(r.get('spread_pct'))}"
            f"<td data-order='{len(r.get('exchanges') or [])}'>{_exch_badges(r.get('exchanges'))}</td>"
            f"{_risk_icon(r)}"
            f"<td data-order='{1 if is_short else 0}'>{flag}</td></tr>"
        )
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SCREENER &middot; Shorts</title>
<link rel="stylesheet" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css">
{DATA_PAGE_CSS}
</head><body><div class="wrap">
{neon_logo("Shorts — weakest perps to short (MEXC / HL)")}
{nav_bar(request, token)}
{auth_status_html(request)}
<h2>Top perps to short — weak, liquid, low reversal risk</h2>
{_regime_banner(data.get("regime"))}
{_regime_gate_banner(data)}
<label class="risktoggle"><input type="checkbox" id="hiderisk"> &#9888; Hide high reversal-risk (show only cleaner shorts)</label>
<div class="searchbar">
  <span class="sicon">&#128269;</span>
  <input id="shortsearch" type="search" placeholder="Search coin, value…" autocomplete="off" autofocus>
  <button id="searchbtn" class="sbtn" type="button">Search</button>
  <button id="clearbtn" class="sbtn clear" type="button">Clear</button>
</div>
<table id="rank" class="display" style="width:100%">
<thead><tr><th>#</th><th>Coin</th><th>Chart</th><th>Short</th><th>Recent<br>5·15·30·45m</th><th>24h%</th>
<th>Sell%</th><th>RVOL</th><th>Breakdown</th><th>1h&nbsp;%</th><th>2h&nbsp;%</th><th>4h&nbsp;%</th>
<th>RSI</th><th>Funding</th><th>Spread&nbsp;%</th><th>Exch</th><th>&#9888;</th><th>SHORT</th></tr></thead>
<tbody>
{''.join(rows_html)}
</tbody></table>
<p class="meta">Scanned {data.get('scanned')} MEXC + HL perps; those most <b>pulled back from their 24h high</b>
(with &ge; {cfg.get('min_volume_usdt', 0) / 1e6:.0f}M 24h volume <b>and MEXC spread &le; {data.get('max_spread_pct')}%</b>)
are deep-scored on 1h/2h/4h (Binance when listed, else MEXC) — catches coins dropping <i>now</i> even if
still green on the day. <span class="chip short">SHORT</span> = strong weakness,
1h falling, 4h downtrend confirmed. Breakdown badges:
<span class="sig">SELL</span><span class="sig">VOL</span><span class="sig">ACC&#9660;</span><span class="sig">BRK&#9660;</span><span class="sig">OI&#9650;</span><span class="sig">F</span>.
<b>{data.get('count_short')}</b> flagged &mdash; only these confirmed shorts are listed
(rejected near-misses, incl. coins that have since bounced, are scored but hidden).
The <span class="warn high">&#9888;</span> marks
<b>reversal risk</b> (oversold / crowded-short / bounce / capitulation) — info only; use the
toggle above the table to filter it out.<br>
generated {gen} UTC &middot; <b id="dataage">{age_txt}</b> min old
<span class="muted">(auto-refreshes every 5 min)</span>.</p>
<script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
<script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
<script>$(document).ready(function(){{
  var dt=$('#rank').DataTable({{"pageLength":100,"order":[[17,"desc"],[3,"desc"]],"dom":"lrtip",
    "columnDefs":[{{"orderable":false,"searchable":false,"targets":[2]}}]}});
  var hideRisk=false;
  $.fn.dataTable.ext.search.push(function(settings,data,dataIndex){{
    if(settings.nTable.id!=='rank') return true;
    if(!hideRisk) return true;
    return settings.aoData[dataIndex].nTr.getAttribute('data-risk')!=='high';
  }});
  var tg=document.getElementById('hiderisk');
  tg.addEventListener('change',function(){{ hideRisk=tg.checked; dt.draw(); }});
  var gts={int(gen_ts) if gen_ts else 0}, ageEl=document.getElementById('dataage');
  if(gts && ageEl){{ function upd(){{ ageEl.textContent=Math.max(0,Math.floor((Date.now()/1000-gts)/60)); }} upd(); setInterval(upd,30000); }}
  var box=document.getElementById('shortsearch');
  function doSearch(){{ dt.search(box.value).draw(); }}
  box.addEventListener('input', doSearch);
  box.addEventListener('keydown', function(e){{ if(e.key==='Enter'){{ e.preventDefault(); doSearch(); }} }});
  document.getElementById('searchbtn').addEventListener('click', doSearch);
  document.getElementById('clearbtn').addEventListener('click', function(){{ box.value=''; doSearch(); box.focus(); }});
}});</script>
</div></body></html>"""
    return html


@app.get("/shorts.json")
async def shorts_json(request: Request):
    require_api_auth(request)
    if not SHORTS_FILE.exists():
        raise HTTPException(status_code=404, detail="Shorts data not generated yet.")
    return JSONResponse(json.loads(SHORTS_FILE.read_text()))


# --- Endpoint: Results — were the momentum/short calls right? (entry vs live price) ---
EVAL_FILE = Path(__file__).resolve().parent / "eval_results.json"


def _sparkline(series) -> str:
    """Inline mini SVG of a P&L-since-call path; green if currently winning, red if not."""
    if not series or len(series) < 2:
        return ""
    W, H, pad = 96, 24, 2
    lo, hi = min(series), max(series)
    rng = (hi - lo) or 1.0
    n = len(series)
    pts = " ".join(
        f"{pad + i * (W - 2 * pad) / (n - 1):.1f},{pad + (1 - (v - lo) / rng) * (H - 2 * pad):.1f}"
        for i, v in enumerate(series))
    color = "#3fe08a" if series[-1] >= 0 else "#ff5a6e"
    zero = ""
    if lo < 0 < hi:
        zy = pad + (1 - (0 - lo) / rng) * (H - 2 * pad)
        zero = (f'<line x1="{pad}" y1="{zy:.1f}" x2="{W - pad}" y2="{zy:.1f}" '
                f'stroke="#39414f" stroke-width="0.5" stroke-dasharray="2 2"/>')
    return (f'<svg class="spark" width="{W}" height="{H}" viewBox="0 0 {W} {H}">{zero}'
            f'<polyline fill="none" stroke="{color}" stroke-width="1.3" points="{pts}"/></svg>')


def _equity_svg(points, color) -> str:
    """A larger equity curve (average open-position P&L over time)."""
    if not points or len(points) < 2:
        return '<p class="meta">Not enough history yet — the curve fills in as picks age.</p>'
    W, H, pl, pr, pt, pb = 560, 150, 46, 14, 12, 18
    eqs = [p["eq"] for p in points]
    lo, hi = min(eqs + [0.0]), max(eqs + [0.0])
    rng = (hi - lo) or 1.0
    n = len(points)

    def X(i):
        return pl + i * (W - pl - pr) / (n - 1)

    def Y(v):
        return pt + (1 - (v - lo) / rng) * (H - pt - pb)

    line_pts = " ".join(f"{X(i):.1f},{Y(p['eq']):.1f}" for i, p in enumerate(points))
    last = eqs[-1]
    zy = Y(0)
    base = (f'<line x1="{pl}" y1="{zy:.1f}" x2="{W - pr}" y2="{zy:.1f}" '
            f'stroke="#39414f" stroke-width="0.7" stroke-dasharray="3 3"/>')
    labels = (f'<text x="3" y="{Y(hi) + 3:.1f}" fill="#7d8499" font-size="9">{hi:+.1f}%</text>'
              f'<text x="3" y="{zy + 3:.1f}" fill="#7d8499" font-size="9">0%</text>'
              f'<text x="3" y="{Y(lo) + 3:.1f}" fill="#7d8499" font-size="9">{lo:+.1f}%</text>')
    lcol = "#3fe08a" if last >= 0 else "#ff5a6e"
    end = (f'<circle cx="{X(n - 1):.1f}" cy="{Y(last):.1f}" r="2.5" fill="{lcol}"/>'
           f'<text x="{W - pr - 2:.1f}" y="{Y(last) - 5:.1f}" fill="{lcol}" font-size="11" '
           f'text-anchor="end">{last:+.2f}%</text>')
    return (f'<svg class="equity" viewBox="0 0 {W} {H}">{base}{labels}'
            f'<polyline fill="none" stroke="{color}" stroke-width="1.7" points="{line_pts}"/>{end}</svg>')


def _eval_table(d, side, token, settled=False) -> str:
    kind = "Longs &mdash; momentum picks" if side == "long" else "Shorts picks"
    label = f"{kind} &middot; {'settled' if settled else 'open'}"
    moved = "rose" if side == "long" else "fell"
    n, w, avg = d.get("count", 0), d.get("wins", 0), d.get("avg", 0)
    if n == 0:
        none_txt = ("No settled picks yet — they move here once they hit the horizon or momentum flips off."
                    if settled else
                    f"No open {side} picks right now — they show here while the screener is still flagging them.")
        return f'<h3>{label}</h3><p class="meta">{none_txt}</p>'
    pct = w / n * 100
    chip = "good" if pct >= 50 else "bad"
    summary = (f'<span class="chip {chip}">{w}/{n} right &middot; {pct:.0f}%</span> '
               f'&nbsp;avg P&amp;L {avg:+.2f}% <span class="muted">(right = price {moved} since the call)</span>')
    time_hdr = "Held&nbsp;h" if settled else "Age&nbsp;h"
    price_hdr = "Exit" if settled else "Now"
    trs = []
    for r in d.get("rows", []):
        pnl = r["pnl"]
        cls = "up" if pnl > 0 else ("down" if pnl < 0 else "")
        x = r.get("extra", "")
        tags = []
        if side == "short" and x and x != "none":
            tags.append(f'<span class="warn {x}" title="{x} reversal risk at call">&#9888;</span>')
        elif side == "long" and x == "early":
            tags.append('<span class="sig">EARLY</span>')
        if settled:
            reason = r.get("close_reason")
            # How the position closed: take-profit / stop / horizon-cap / momentum-flip.
            label = {
                "tp": ('<span class="up" title="closed: take-profit hit">&#127919; TP</span>'),
                "stop": ('<span class="down" title="closed: hard stop-loss hit">&#9210; stop</span>'),
                "horizon": ('<span class="muted" title="closed: reached the horizon">&#9201; horizon</span>'),
            }.get(reason, '<span class="muted" title="closed: momentum flipped off">&#10007; off</span>')
            tags.append(label)
        tval = r.get("held_hours" if settled else "age_hours", 0.0)
        trs.append(
            f"<tr><td>{coin_link(r['coin'], token)}</td>"
            f"<td data-order='{tval}'>{tval:.1f}</td>"
            f"<td>{r['entry']:.6g}</td>"
            f"<td>{r['now']:.6g}</td>"
            f"<td data-order='{pnl}' class='{cls}'><b>{pnl:+.2f}%</b></td>"
            f"<td>{_sparkline(r.get('spark') or [])}</td>"
            f"<td>{' '.join(tags)}</td></tr>")
    tid = f"res{side}{'settled' if settled else ''}"
    return (f'<h3>{label}</h3><p class="meta">{summary}</p>'
            f'<table id="{tid}" class="display" style="width:100%">'
            f'<thead><tr><th>Coin</th><th>{time_hdr}</th><th>Entry</th><th>{price_hdr}</th><th>P&amp;L&nbsp;%</th>'
            f'<th>Since call</th><th></th></tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table>')


@app.get("/results", response_class=HTMLResponse)
async def results_page(request: Request):
    if not is_authenticated(request):
        return login_redirect(request)
    token = link_token(request)
    home = with_token("/", token)
    if not EVAL_FILE.exists():
        return (f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
                f"<title>SCREENER &middot; Results</title>{DATA_PAGE_CSS}</head>"
                f"<body><div class='wrap'>{neon_logo('Results — track record')}"
                f'<a href="{home}" class="btn">&#8962; Home</a>'
                f'<h2>No results yet</h2>'
                f'<p class="meta">Run <code>python3 build_eval.py</code> to generate it.</p>'
                f"</div></body></html>")
    data = json.loads(EVAL_FILE.read_text())
    gen = data.get("generated_utc")
    gen_ts = _gen_epoch(gen)
    age_min = int(max(0, (datetime.now(timezone.utc).timestamp() - gen_ts) / 60)) if gen_ts else None
    age_txt = str(age_min) if age_min is not None else "?"
    horizon_txt = f"{data.get('horizon_hours', 4):g}"
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SCREENER &middot; Results</title>
<link rel="stylesheet" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css">
{DATA_PAGE_CSS}
</head><body><div class="wrap">
{neon_logo("Results — were our calls right?")}
{nav_bar(request, token)}
{auth_status_html(request)}
<h2>Track record — entry price vs price since the call</h2>
<div class="resgrid">
  <div><h3>Long equity</h3>{_equity_svg(data.get("longs", {}).get("equity"), "#3fe08a")}
    <p class="eqcap">Average P&amp;L across long picks while open at each 15m step (a pick drops out once it settles).</p></div>
  <div><h3>Short equity</h3>{_equity_svg(data.get("shorts", {}).get("equity"), "#ff7a93")}
    <p class="eqcap">Average P&amp;L across short picks while open at each 15m step (a pick drops out once it settles).</p></div>
</div>
<div class="resgrid">
  <div>{_eval_table(data.get("longs", {}), "long", token)}</div>
  <div>{_eval_table(data.get("shorts", {}), "short", token)}</div>
</div>
<h2 style="margin-top:26px">Settled — closed at the {horizon_txt}h horizon or when momentum flipped off</h2>
<div class="resgrid">
  <div>{_eval_table(data.get("longs", {}).get("settled", {}), "long", token, settled=True)}</div>
  <div>{_eval_table(data.get("shorts", {}).get("settled", {}), "short", token, settled=True)}</div>
</div>
<p class="meta">Each coin's <b>first</b> flag is the entry. A pick stays <b>open</b> while the screener
keeps flagging it and tracks the live price; it <b>settles</b> — P&amp;L frozen at the close price —
once it reaches the <b>{horizon_txt}h</b> horizon or momentum flips off, whichever comes first.
Open picks are the active board above; settled picks are the realized record.
Evaluated {gen} UTC &middot; <b id="dataage">{age_txt}</b> min old
<span class="muted">(auto-refreshes every 5 min)</span>.</p>
<div class="legend">
<b>Legend</b> &mdash;
<b>Open</b>: still being flagged (tracks the live price). &nbsp;
<b>Settled</b>: closed, P&amp;L frozen at the exit. &nbsp;
<b>Age</b> = hours since the call; <b>Held</b> = hours the position stayed open.
<br>Exit reason (settled rows):
<span class="up">&#127919; TP</span> = take-profit hit; &nbsp;
<span class="down">&#9210; stop</span> = hard stop-loss hit; &nbsp;
<span class="muted">&#9201; horizon</span> = reached the <b>{horizon_txt}h</b> max hold (capped for scoring); &nbsp;
<span class="muted">&#10007; off</span> = momentum flipped off &mdash; the screener stopped flagging it (no re-flag within the grace window).
<br>Per-call tag:
<span class="sig">EARLY</span> = the long fired the <b>early-detection</b> confluence at the call (the early leading-signal threshold); &nbsp;
<span class="warn high">&#9888;</span> = the short had <b>reversal risk</b> at the call (oversold / crowded-short / bounce; a fainter icon = lower risk).
</div>
<script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
<script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
<script>$(document).ready(function(){{
  ['reslong','resshort','reslongsettled','resshortsettled'].forEach(function(id){{
    var el=document.getElementById(id);
    if(el) $('#'+id).DataTable({{"paging":false,"dom":"t","order":[[4,"desc"]],
      "columnDefs":[{{"orderable":false,"searchable":false,"targets":[5]}}]}});
  }});
  var gts={int(gen_ts) if gen_ts else 0}, ageEl=document.getElementById('dataage');
  if(gts && ageEl){{ function upd(){{ ageEl.textContent=Math.max(0,Math.floor((Date.now()/1000-gts)/60)); }} upd(); setInterval(upd,30000); }}
}});</script>
</div></body></html>"""
    return html


@app.get("/results.json")
async def results_json(request: Request):
    require_api_auth(request)
    if not EVAL_FILE.exists():
        raise HTTPException(status_code=404, detail="Results not generated yet.")
    return JSONResponse(json.loads(EVAL_FILE.read_text()))


# --- Endpoint: Combined view — select on MEXC, backtest on Binance ---
@app.get("/combined", response_class=HTMLResponse)
async def combined(request: Request):
    if not is_authenticated(request):
        return login_redirect(request)
    token = link_token(request)
    if not MEXC_RANKING_FILE.exists():
        return _ranking_missing_page(token, "Combined MEXC + Binance", "build_mexc_ranking.py")
    mexc = json.loads(MEXC_RANKING_FILE.read_text())
    binance = json.loads(BINANCE_RANKING_FILE.read_text()) if BINANCE_RANKING_FILE.exists() else {"rows": []}
    b_by_coin = {r["coin"]: r for r in binance.get("rows", [])}
    rows_html = []
    for r in mexc.get("rows", []):          # already sorted by MEXC cost
        coin = r["coin"]
        good = r["good"]
        flag = ('<span class="chip good">FILTER PASS</span>' if good else '<span class="chip bad">FILTER FAIL</span>')
        vol = r.get("volatility_pct")
        vol_cell = f"<td data-order='{vol if vol is not None else -1}'>{vol:.2f}</td>" if vol is not None else "<td data-order='-1'>-</td>"
        qv = r["quote_volume"]
        b = b_by_coin.get(coin)
        b_cost = f"{b['total_cost_pct']:.4f}" if b else "—"
        b_cost_order = b["total_cost_pct"] if b else 9999
        rows_html.append(
            f"<tr>"
            f"<td>{r['rank']}</td>"
            f"<td>{coin_link(coin, token)}{asset_icon(r.get('asset_type'))}</td>"
            f"<td>{tradingview_link(coin)}</td>"
            f"<td>{r['spread_pct']:.4f}</td>"
            f"{vol_cell}"
            f"<td data-order='{qv:.0f}'>{qv/1e6:,.2f}M</td>"
            f"<td>{r['total_cost_pct']:.4f}</td>"
            f"<td data-order='{b_cost_order}'>{b_cost}</td>"
            f"<td data-order='{1 if good else 0}'>{flag}</td></tr>"
        )
    mexc_fee = mexc.get("fees", {}).get("roundtrip_taker_pct")
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SCREENER &middot; Combined</title>
<link rel="stylesheet" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css">
{DATA_PAGE_CSS}
</head><body><div class="wrap">
{neon_logo("Combined — MEXC &amp; Binance cost comparison")}
{nav_bar(request, token)}
{auth_status_html(request)}
<h2>Combined ranking — MEXC &amp; Binance cost comparison</h2>
<div class="searchbar">
  <span class="sicon">&#128269;</span>
  <input id="ranksearch" type="search" placeholder="Search coin, rank or value…" autocomplete="off" autofocus>
  <button id="searchbtn" class="sbtn" type="button">Search</button>
  <button id="clearbtn" class="sbtn clear" type="button">Clear</button>
</div>
<table id="rank" class="display" style="width:100%">
<thead><tr><th>Rank</th><th>Coin</th><th>Chart</th><th>MEXC Spread %</th><th>MEXC Vol %</th>
<th>MEXC 24h Vol</th><th>MEXC Cost %</th><th>Binance Cost %</th><th>MEXC Filter</th></tr></thead>
<tbody>
{''.join(rows_html)}
</tbody></table>
<p class="meta">Ranked by <b>MEXC</b> round-trip cost (spread % + {mexc_fee}% fee) — the exchange you trade on.
<span class="chip good">FILTER PASS</span> reflects MEXC volume/spread/volatility thresholds.
The <b>Binance Cost %</b> column is for cross-exchange cost comparison only.<br>
{mexc.get('total_symbols')} MEXC symbols &middot; {mexc.get('count_good')} good &middot; generated {mexc.get('generated_utc')} UTC.</p>
<script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
<script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
<script>$(document).ready(function(){{
  var dt=$('#rank').DataTable({{"pageLength":100,"order":[[0,"asc"]],"dom":"lrtip",
    "columnDefs":[{{"orderable":false,"searchable":false,"targets":[2]}}]}});
  var box=document.getElementById('ranksearch');
  function doSearch(){{ dt.search(box.value).draw(); }}
  box.addEventListener('input', doSearch);
  box.addEventListener('keydown', function(e){{ if(e.key==='Enter'){{ e.preventDefault(); doSearch(); }} }});
  document.getElementById('searchbtn').addEventListener('click', doSearch);
  document.getElementById('clearbtn').addEventListener('click', function(){{ box.value=''; doSearch(); box.focus(); }});
}});</script>
</div></body></html>"""
    return html


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("data_server:app", host="0.0.0.0", port=8000, reload=True)
