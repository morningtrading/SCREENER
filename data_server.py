"""
FastAPI server to broadcast access to Binance futures data files and provide a summary endpoint.

Authentication (any one of these grants access):
  * Session cookie  — obtained via the /login page (username + password).
  * HTTP Basic auth — same username + password, for programmatic/browser use.
  * Access token    — `x-access-token` header or `?token=` query param (for scripts/API).
"""
import os
import sys
import json
import base64
import hashlib
import secrets
import binascii
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, RedirectResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from typing import List, Dict, Optional, Tuple
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote_plus
from dotenv import load_dotenv

# --- Configuration: locations come from env vars / a local .env (no hardcoded host paths). ---
HERE = Path(__file__).resolve().parent
# A local .env in this folder can set SCREENER_* locations and/or the token.
load_dotenv(HERE / ".env")
PROJECT_ROOT = Path(os.environ.get("SCREENER_PROJECT_ROOT", str(HERE)))
DATA_DIR = Path(os.environ.get("SCREENER_DATA_DIR", str(PROJECT_ROOT / "data" / "futures")))
ENV_FILE = Path(os.environ.get("SCREENER_ENV_FILE", str(HERE / ".env")))
# Token may come from the local .env above or from a configured project .env.
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

# Rough CSV-size estimate factor (CSV text is ~this many times the on-disk .feather size).
CSV_SIZE_FACTOR = float(os.getenv("SCREENER_CSV_SIZE_FACTOR", "2.8"))

# Secret used to sign the session cookie. Stable across restarts (derived from the token)
# unless SCREENER_SECRET_KEY is set explicitly.
SECRET_KEY = os.getenv("SCREENER_SECRET_KEY") or hashlib.sha256(
    ("screener-session::" + TOKEN).encode()
).hexdigest()
# Session lifetime in seconds (default 7 days).
SESSION_MAX_AGE = int(os.getenv("SCREENER_SESSION_MAX_AGE", str(7 * 24 * 3600)))

app = FastAPI(title="Binance Futures Data Server")

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
    """Coin name linking to its filtered summary on this server."""
    asset_url = with_token(f"/summary?showmeasset={coin}", token)
    return f'<a href="{asset_url}">{coin}</a>'


def tradingview_link(coin: str) -> str:
    """Standalone TradingView chart link (chart emoji) for a coin."""
    tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{coin}USDT.P"
    return (f'<a href="{tv_url}" target="_blank" rel="noopener" class="tv" '
            f'title="View {coin} on TradingView">&#128200;</a>')


def coin_links(coin: str, token: str) -> str:
    """Coin name link plus an inline TradingView chart link (used where there's no Chart column)."""
    return f'{coin_link(coin, token)} {tradingview_link(coin)}'


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

# --- Helper: Parse filename ---
def parse_filename(fname: str):
    # Example: BTC_USDT_USDT-15m-futures.feather
    parts = fname.split("-")
    if len(parts) < 2:
        return None
    coin = parts[0].split("_")[0]
    timeframe = parts[0].split("_")[-1].replace("USDT", "").replace("_", "")
    tf = parts[1]
    dtype = parts[2].replace(".feather", "") if len(parts) > 2 else "unknown"
    return coin, tf, dtype

# --- Helper: Get file time span + bar (row) count ---
def get_time_span(fpath: Path):
    # Try to find a suitable time column. Returns (start, end, nbars).
    for col in ["timestamp", "date", "time"]:
        try:
            df = pd.read_feather(fpath, columns=[col])
            if df.empty or col not in df:
                continue
            nbars = len(df)  # rows in this column == number of bars/records
            # Try to convert to int if not already
            try:
                start = int(df[col].iloc[0])
                end = int(df[col].iloc[-1])
            except Exception:
                # Try to parse as datetime string
                try:
                    start = int(pd.to_datetime(df[col].iloc[0]).timestamp() * 1000)
                    end = int(pd.to_datetime(df[col].iloc[-1]).timestamp() * 1000)
                except Exception:
                    continue
            return start, end, nbars
        except Exception:
            continue
    return None, None, None

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
    feather_files = [f for f in DATA_DIR.iterdir() if f.suffix == ".feather"]
    n_files = len(feather_files)
    coins = sorted({p[0] for p in (parse_filename(f.name) for f in feather_files) if p})
    coin_options = "".join(f'<option value="{c}">{c}</option>' for c in coins)
    nav = [
        ("Data Summary", "Full file table: coin, timeframe, type, date range, age. Click a filename to download.", with_token("/summary", token)),
        ("Full Binance Ranking", "Every Binance futures perpetual ranked, filtered by volume &amp; spread, with a volatility index. Green = tradeable.", with_token("/binance-ranking", token)),
        ("MEXC Ranking", "Every MEXC futures perpetual ranked the same way (public API, no key). Green = tradeable.", with_token("/mexc-ranking", token)),
        ("Combined (MEXC + Binance)", "Trade-on-MEXC selection view with Binance backtest-data links side by side.", with_token("/combined", token)),
        ("Raw JSON Summary", "Machine-readable summary of every data file.", with_token("/summary.json", token)),
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
{neon_logo(f"{n_files} data files &middot; OHLCV, mark &amp; funding-rate")}
{nav_bar(request, token)}
{auth_status_html(request)}
{cards}
<div class="jump">
  <strong>Jump to a single coin</strong><br>
  <p style="color:#9aa3b6;font-size:14px;margin:6px 0 10px;">Pick an asset to view its files ({len(coins)} coins on file).</p>
  <select id="coin" onchange="go()">
    <option value="" disabled selected>Select a coin…</option>
    {coin_options}
  </select>
  <button onclick="go()">Show coin</button>
</div>
<script>
const TOKEN = {token!r};
function go(){{
  const c = document.getElementById('coin').value.trim();
  if(!c) return;
  let url = '/summary?showmeasset=' + encodeURIComponent(c);
  if (TOKEN) url += '&token=' + encodeURIComponent(TOKEN);
  location.href = url;
}}
</script>
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
.navbtn{display:inline-flex;align-items:center;gap:7px;padding:7px 14px;border-radius:9px;text-decoration:none;
  font-size:13px;color:#cfefff;border:1px solid rgba(0,255,255,.22);background:rgba(255,255,255,.03);transition:.16s;}
.navbtn:hover{border-color:var(--neon);box-shadow:0 0 14px rgba(0,255,255,.3);color:#fff;}
.navbtn.dl{border-color:rgba(63,224,138,.4);color:#9bf3c2;}
.navbtn.dl:hover{border-color:#3fe08a;box-shadow:0 0 14px rgba(63,224,138,.35);}
.navbtn .num{font-weight:800;color:var(--neon);letter-spacing:.5px;font-variant-numeric:tabular-nums;
  text-shadow:0 0 8px rgba(0,255,255,.5);}
.navbtn.dl .num{color:#3fe08a;text-shadow:0 0 8px rgba(63,224,138,.5);}
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
/* BTC market-regime banner (info only) */
.btcbar{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin:0 0 16px;padding:9px 15px;border-radius:12px;
  background:linear-gradient(90deg,rgba(243,186,47,.07),rgba(0,255,255,.04));border:1px solid rgba(243,186,47,.28);}
.btctitle{font-weight:800;letter-spacing:1.5px;color:#f3ba2f;text-shadow:0 0 9px rgba(243,186,47,.45);}
.btcdot{display:inline-flex;flex-direction:column;align-items:center;gap:3px;min-width:30px;}
.btclab{font-size:9px;color:#8a91a3;letter-spacing:.3px;}
.btcsep{width:1px;height:24px;background:rgba(255,255,255,.14);margin:0 3px;}
.btcregime{font-weight:700;font-size:12px;padding:2px 11px;border-radius:999px;border:1px solid;}
.btcregime.up{color:#3fe08a;border-color:rgba(63,224,138,.55);background:rgba(63,224,138,.1);}
.btcregime.down{color:#ff7a93;border-color:rgba(255,90,110,.5);background:rgba(255,90,110,.07);}
.btcregime.mixed{color:#bdfdff;border-color:rgba(0,255,255,.4);}
.btcnote{font-size:10.5px;color:#7d8499;margin-left:auto;}
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

# Colour + one-word gloss per freqtrade candle type.
TYPE_INFO = {
    "futures": ("var(--neon)", "#0ff"),
    "mark": ("var(--neon2)", "#f0f"),
    "funding_rate": ("var(--rate)", "#ffb300"),
    "rate": ("var(--rate)", "#ffb300"),
}


def type_badge(dtype: str) -> str:
    _, hexc = TYPE_INFO.get(dtype, ("#8a93a8", "#8a93a8"))
    return (f'<span class="badge" style="color:{hexc};border-color:{hexc};'
            f'box-shadow:0 0 8px {hexc}55">{dtype}</span>')


def neon_logo(subtitle: str) -> str:
    return (f'<h1 class="logo"><span class="bolt">&#9889;</span> SCREENER</h1>'
            f'<p class="subt">{subtitle}</p>')


def nav_bar(request: Request, token: str) -> str:
    """The numbered top navigation, identical on every page. GOOD-pairs is hidden for read-only users."""
    items = [
        ("Home", with_token("/", token), ""),
        ("Data Summary", with_token("/summary", token), ""),
        ("Binance Ranking", with_token("/binance-ranking", token), ""),
        ("MEXC Ranking", with_token("/mexc-ranking", token), ""),
        ("Combined", with_token("/combined", token), ""),
        ("Momentum", with_token("/momentum", token), ""),
        ("Raw JSON", with_token("/summary.json", token), ""),
    ]
    if not is_readonly(request):
        items.append(("GOOD pairs", with_token("/binance-good-pairs.json", token), " dl"))
    links = "".join(
        f'<a class="navbtn{cls}" href="{url}"><span class="num">-{i}-</span>{label}</a>'
        for i, (label, url, cls) in enumerate(items, 1)
    )
    return f'<nav class="topnav">{links}</nav>'


def human_size(n: Optional[int]) -> str:
    """Human-readable byte size, e.g. 2.4 MB."""
    if n is None:
        return ''
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# Legend explaining the three data types served for each coin.
DATA_TYPE_LEGEND = (
    '<div class="legend"><h3>Data types — what each row means</h3><ul>'
    f'<li>{type_badge("futures")} &nbsp;<b>Futures (last price) OHLCV.</b> '
    'Open/high/low/close &amp; volume built from the perpetual contract’s actual '
    '<i>traded</i> price. This is the standard price series for charting and backtesting.</li>'
    f'<li>{type_badge("mark")} &nbsp;<b>Mark-price OHLCV.</b> '
    'The exchange’s <i>fair</i> price (an index-based, smoothed value) used to compute '
    'liquidations and unrealised PnL — not the last trade. It avoids wicks from thin '
    'order books, so it can differ slightly from the futures price.</li>'
    f'<li>{type_badge("rate")} &nbsp;<b>Funding rate.</b> '
    'The periodic payment (typically every 8h) exchanged between longs and shorts that '
    'tethers the perpetual to spot. <b>Positive</b> = longs pay shorts (market leans long); '
    '<b>negative</b> = shorts pay longs. Stored as a rate series, not OHLCV.</li>'
    '</ul></div>'
)


@app.get("/summary", response_class=HTMLResponse)
async def summary(request: Request):
    if not is_authenticated(request):
        return login_redirect(request)
    token = link_token(request)
    # Optional filter: ?showmeasset=bnb -> only show that coin (case-insensitive)
    asset = request.query_params.get("showmeasset", "").strip().upper()
    files = sorted([f for f in DATA_DIR.iterdir() if f.suffix == ".feather"])
    summary: Dict[str, Dict[str, List[Dict]]] = {}
    for f in files:
        fname = f.name
        parsed = parse_filename(fname)
        if not parsed:
            continue
        coin, tf, dtype = parsed
        if asset and coin != asset:
            continue
        start, end, nbars = get_time_span(f)
        # Calculate year, months, days
        if start and end:
            dt_start = datetime.utcfromtimestamp(start/1000)
            dt_end = datetime.utcfromtimestamp(end/1000)
            year = dt_start.year
            months = (dt_end.year - dt_start.year) * 12 + (dt_end.month - dt_start.month) + 1
            days = (dt_end - dt_start).days + 1
        else:
            year = months = days = None
        if coin not in summary:
            summary[coin] = {}
        if tf not in summary[coin]:
            summary[coin][tf] = []
        summary[coin][tf].append({
            "file": fname,
            "type": dtype,
            "start": start,
            "end": end,
            "year": year,
            "months": months,
            "days": days,
            "bars": nbars
        })
    # Build HTML table (neon themed — matches the /login logo)
    subtitle = ("Binance Futures Data &middot; " + asset) if asset else "Binance Futures Data"
    html = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>SCREENER &middot; Data Summary</title>",
        '<link rel="stylesheet" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css">',
        DATA_PAGE_CSS,
        "</head><body><div class='wrap'>",
        neon_logo(subtitle),
        nav_bar(request, token),
        ('<a href="' + with_token('/summary', token) + '" class="btn">&#8617; Show All Coins</a>') if asset else '',
        auth_status_html(request),
        f"<h2>Data Summary{(' &mdash; ' + asset) if asset else ''}</h2>",
        DATA_TYPE_LEGEND,
        ('<div class="searchbar">'
         '<span class="sicon">&#128269;</span>'
         '<input id="tsearch" type="search" placeholder="Search coin, timeframe, type, file…" autocomplete="off">'
         '<button id="tsearchbtn" class="sbtn" type="button">Search</button>'
         '<button id="tclearbtn" class="sbtn clear" type="button">Clear</button>'
         '</div>'),
        '<table id="summary" class="display" style="width:100%">',
        ("<thead><tr><th>Coin</th><th>TF</th><th>Type</th>"
         "<th title='Number of rows/candles in the file'>Bars</th><th>Date Range</th><th>Filename</th>"
         "<th title='On-disk .feather size'>Size</th>"
         f"<th title='Rough estimate (~{CSV_SIZE_FACTOR:g}x the .feather size); exact size is computed only on download'>CSV ~</th>"
         "<th>Age</th></tr></thead><tbody>")
    ]
    ro = is_readonly(request)
    now = datetime.utcnow().timestamp()
    for coin in sorted(summary.keys()):
        for tf in sorted(summary[coin].keys()):
            for entry in sorted(summary[coin][tf], key=lambda x: (x["type"], x["file"])):
                file_url = with_token(f"/file/{entry['file']}", token)
                csv_url = with_token(f"/csv/{entry['file']}", token)
                # Get file age (days) and on-disk size from a single stat() call
                fpath = DATA_DIR / entry['file']
                try:
                    st = fpath.stat()
                    age_days = int((now - st.st_mtime) // 86400)
                    size_bytes = st.st_size
                except Exception:
                    age_days = ''
                    size_bytes = None
                # Format date range
                def fmt(ts):
                    if ts is None:
                        return ''
                    try:
                        return datetime.utcfromtimestamp(ts/1000).strftime('%Y-%b-%d')
                    except Exception:
                        return str(ts)
                date_range = f"{fmt(entry['start'])} → {fmt(entry['end'])}" if entry['start'] and entry['end'] else ''
                bars = entry['bars']
                bars_cell = (f"<td data-order='{bars}'>{bars:,}</td>"
                             if bars is not None else "<td data-order='-1'></td>")
                # Read-only accounts see the filename but no download links.
                if ro:
                    file_cell = (f"<td>{entry['file']} "
                                 f"<span class='muted' title='Read-only account — downloads disabled'>&#128274;</span></td>")
                else:
                    file_cell = (f"<td><a href='{file_url}'>{entry['file']}</a> "
                                 f"&nbsp;|&nbsp; <a href='{csv_url}'>CSV</a></td>")
                # Estimated CSV download size (rough; exact size only known on conversion).
                csv_est = int(size_bytes * CSV_SIZE_FACTOR) if size_bytes is not None else None
                csv_cell = (f"<td data-order='{csv_est}'>~{human_size(csv_est)}</td>"
                            if csv_est is not None else "<td data-order='-1'></td>")
                html.append(
                    f"<tr>"
                    f"<td>{coin_links(coin, token)}</td>"
                    f"<td>{tf}</td>"
                    f"<td>{type_badge(entry['type'])}</td>"
                    f"{bars_cell}"
                    f"<td>{date_range}</td>"
                    f"{file_cell}"
                    f"<td data-order='{size_bytes if size_bytes is not None else -1}'>{human_size(size_bytes)}</td>"
                    f"{csv_cell}"
                    f"<td>{age_days} days old</td>"
                    f"</tr>"
                )
    html.append("</tbody></table>")
    # DataTables JS + custom neon search (matches the ranking page)
    html.append('''
<script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
<script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
<script>
$(document).ready(function() {
    var dt=$('#summary').DataTable({"pageLength":50,"dom":"lrtip"});
    var box=document.getElementById('tsearch');
    function doSearch(){ dt.search(box.value).draw(); }
    box.addEventListener('input', doSearch);
    box.addEventListener('keydown', function(e){ if(e.key==='Enter'){ e.preventDefault(); doSearch(); } });
    document.getElementById('tsearchbtn').addEventListener('click', doSearch);
    document.getElementById('tclearbtn').addEventListener('click', function(){ box.value=''; doSearch(); box.focus(); });
});
</script>
''')
    html.append("</div></body></html>")
    return "\n".join(html)
@app.get("/summary.json")
async def summary_json(request: Request):
    require_api_auth(request)
    # Optional filter: ?showmeasset=bnb -> only show that coin (case-insensitive)
    asset = request.query_params.get("showmeasset", "").strip().upper()
    files = sorted([f for f in DATA_DIR.iterdir() if f.suffix == ".feather"])
    summary: Dict[str, Dict[str, List[Dict]]] = {}
    for f in files:
        fname = f.name
        parsed = parse_filename(fname)
        if not parsed:
            continue
        coin, tf, dtype = parsed
        if asset and coin != asset:
            continue
        start, end, nbars = get_time_span(f)
        # Calculate year, months, days
        if start and end:
            dt_start = datetime.utcfromtimestamp(start/1000)
            dt_end = datetime.utcfromtimestamp(end/1000)
            year = dt_start.year
            months = (dt_end.year - dt_start.year) * 12 + (dt_end.month - dt_start.month) + 1
            days = (dt_end - dt_start).days + 1
        else:
            year = months = days = None
        if coin not in summary:
            summary[coin] = {}
        if tf not in summary[coin]:
            summary[coin][tf] = []
        summary[coin][tf].append({
            "file": fname,
            "type": dtype,
            "start": start,
            "end": end,
            "year": year,
            "months": months,
            "days": days,
            "bars": nbars
        })
    # Sort coins, then timeframes
    sorted_summary = {k: dict(sorted(v.items())) for k, v in sorted(summary.items())}
    return JSONResponse(content=sorted_summary)


# --- Endpoint: Download file ---
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
<p class="meta">All {data.get('total_symbols')} live USDⓈ-M PERPETUAL symbols, ranked by round-trip cost
(spread % + {data.get('fees',{}).get('roundtrip_taker_pct')}% fee).
<span class="chip good">FILTER PASS</span> = 24h volume &ge; {data.get('min_volume'):,.0f} USDT
AND spread &le; {data.get('max_spread_pct')}%
AND volatility &ge; {data.get('min_volatility_pct')}%.
<b>{data.get('count_good')}</b> of {data.get('total_symbols')} qualify.
Volatility = 24h (high-low)/avg %.<br>Generated {data.get('generated_utc')} UTC.</p>
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
<script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
<script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
<script>$(document).ready(function(){{
  var dt=$('#rank').DataTable({{"pageLength":50,"order":[[0,"asc"]],"dom":"lrtip",
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
<p class="meta">All {data.get('total_symbols')} live USDT-margined perpetuals (MEXC public API, no key),
ranked by round-trip cost (spread % + {data.get('fees',{}).get('roundtrip_taker_pct')}% fee).
<span class="chip good">FILTER PASS</span> = 24h volume &ge; {data.get('min_volume'):,.0f} USDT
AND spread &le; {data.get('max_spread_pct')}%
AND volatility &ge; {data.get('min_volatility_pct')}%.
<b>{data.get('count_good')}</b> of {data.get('total_symbols')} qualify.
Coin links go to the Binance data summary (backtest CSVs).<br>Generated {data.get('generated_utc')} UTC.</p>
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
<script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
<script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
<script>$(document).ready(function(){{
  var dt=$('#rank').DataTable({{"pageLength":50,"order":[[0,"asc"]],"dom":"lrtip",
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


# --- Endpoint: Momentum screener (CMC trending × Binance 1h/2h/4h) ---
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


def _btc_banner(btc) -> str:
    """A dot strip for BTC across the same windows (5/15/30/45m + 1h/2h/4h) — market-regime context."""
    if not btc:
        return ""
    intraday = ["5m", "15m", "30m", "45m"]
    tfs = ["1h", "2h", "4h"]

    def cell(k):
        return f'<span class="btcdot">{_btc_dot(btc.get(k))}<span class="btclab">{k}</span></span>'

    longs = [btc.get(t) for t in tfs if btc.get(t) is not None]
    pos = sum(1 for x in longs if x > 0)
    neg = sum(1 for x in longs if x < 0)
    if longs and pos == len(longs):
        rcls, rtxt = "up", "Risk-on &#9650;"
    elif longs and neg == len(longs):
        rcls, rtxt = "down", "Risk-off &#9660;"
    else:
        rcls, rtxt = "mixed", "Mixed / chop"
    return (f'<div class="btcbar"><span class="btctitle">BTC</span>'
            f'<span class="btcregime {rcls}">{rtxt}</span>'
            f'{"".join(cell(k) for k in intraday)}<span class="btcsep"></span>'
            f'{"".join(cell(k) for k in tfs)}'
            f'<span class="btcnote">market regime &middot; for context only</span></div>')


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
                f"<body><div class='wrap'>{neon_logo('Momentum — CMC Trending × Binance')}"
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
    for r in data.get("rows", []):
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
            f"<td>{r.get('cmc_rank','')}</td>"
            f"<td>{coin_link(r['coin'], token)}</td>"
            f"<td>{chart}</td>"
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
{neon_logo("Momentum — CMC Trending × Binance 1h/2h/4h")}
{nav_bar(request, token)}
{auth_status_html(request)}
<h2>Momentum — trending coins in a real uptrend</h2>
<p class="meta">CoinMarketCap's {data.get('total')} trending coins, scored on Binance
<b>1h/2h/4h</b> candles (weights {w.get('1h')}/{w.get('2h')}/{w.get('4h')}, strong on 1h)
plus a small recent bucket ({w.get('recent')}) and a 5–15m acceleration term.
<span class="chip good">UPTREND</span> = composite score &ge; {cfg.get('min_score')}, 1h rising,
NOT overextended (1h &le; {cfg.get('max_extension_pct')}% above its EMA{cfg.get('ema_slow')}),
no single-bar spike (&le; {cfg.get('max_single_bar_pct')}%), 4h trend confirms, and
no recent 15m dump (&gt; {cfg.get('max_recent_drop_pct')}%) — i.e.
a genuine climb, <b>not a post-pump</b> top. <b>{data.get('count_momentum')}</b> qualify now.
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
Source {data.get('source')} · generated {gen} UTC · <b id="dataage">{age_txt}</b> min old
<span class="muted">(auto-refreshes every 5 min)</span>.</p>
{_btc_banner(data.get("btc"))}
<div class="searchbar">
  <span class="sicon">&#128269;</span>
  <input id="momentumsearch" type="search" placeholder="Search coin, rank or value…" autocomplete="off" autofocus>
  <button id="searchbtn" class="sbtn" type="button">Search</button>
  <button id="clearbtn" class="sbtn clear" type="button">Clear</button>
</div>
<table id="rank" class="display" style="width:100%">
<thead><tr><th>CMC#</th><th>Coin</th><th>Chart</th><th>Score</th><th>Recent<br>5·15·30·45m</th><th>Buy%</th><th>RVOL</th><th>Early</th><th>1h&nbsp;%</th><th>2h&nbsp;%</th><th>4h&nbsp;%</th>
<th>Ext&nbsp;%</th><th>4h&nbsp;Trend</th><th>Mkt</th><th>Exchanges</th><th>Momentum</th></tr></thead>
<tbody>
{''.join(rows_html)}
</tbody></table>
<script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
<script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
<script>$(document).ready(function(){{
  var dt=$('#rank').DataTable({{"pageLength":50,"order":[[4,"desc"]],"dom":"lrtip",
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
    # Coins that have Binance backtest data on disk (feather files).
    bdata_coins = set()
    try:
        for f in DATA_DIR.iterdir():
            if f.suffix == ".feather":
                p = parse_filename(f.name)
                if p:
                    bdata_coins.add(p[0])
    except FileNotFoundError:
        pass
    rows_html = []
    for r in mexc.get("rows", []):          # already sorted by MEXC cost
        coin = r["coin"]
        good = r["good"]
        bg = "rgba(63,224,138,.08)" if good else "rgba(255,90,110,.05)"
        flag = ('<span class="chip good">FILTER PASS</span>' if good else '<span class="chip bad">FILTER FAIL</span>')
        vol = r.get("volatility_pct")
        vol_cell = f"<td data-order='{vol if vol is not None else -1}'>{vol:.2f}</td>" if vol is not None else "<td data-order='-1'>-</td>"
        qv = r["quote_volume"]
        b = b_by_coin.get(coin)
        b_cost = f"{b['total_cost_pct']:.4f}" if b else "—"
        b_cost_order = b["total_cost_pct"] if b else 9999
        if coin in bdata_coins:
            bt = f'<a class="btn dl" href="{with_token(f"/summary?showmeasset={coin}", token)}">CSV &#8595;</a>'
            bt_order = 1
        else:
            bt = '<span class="no">&mdash;</span>'
            bt_order = 0
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
            f"<td data-order='{1 if good else 0}'>{flag}</td>"
            f"<td data-order='{bt_order}'>{bt}</td></tr>"
        )
    mexc_fee = mexc.get("fees", {}).get("roundtrip_taker_pct")
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SCREENER &middot; Combined</title>
<link rel="stylesheet" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css">
{DATA_PAGE_CSS}
</head><body><div class="wrap">
{neon_logo("Combined — trade on MEXC, backtest on Binance")}
{nav_bar(request, token)}
{auth_status_html(request)}
<h2>Combined ranking — MEXC selection &middot; Binance backtest data</h2>
<p class="meta">Ranked by <b>MEXC</b> round-trip cost (spread % + {mexc_fee}% fee) — the exchange you trade on.
<span class="chip good">FILTER PASS</span> reflects MEXC volume/spread/volatility thresholds.
The <b>Binance Cost %</b> column and the <b>Backtest</b> CSV link are for historical data only
(downloads come from the bundled Binance feather files).<br>
{mexc.get('total_symbols')} MEXC symbols &middot; {mexc.get('count_good')} good &middot; generated {mexc.get('generated_utc')} UTC.</p>
<div class="searchbar">
  <span class="sicon">&#128269;</span>
  <input id="ranksearch" type="search" placeholder="Search coin, rank or value…" autocomplete="off" autofocus>
  <button id="searchbtn" class="sbtn" type="button">Search</button>
  <button id="clearbtn" class="sbtn clear" type="button">Clear</button>
</div>
<table id="rank" class="display" style="width:100%">
<thead><tr><th>Rank</th><th>Coin</th><th>Chart</th><th>MEXC Spread %</th><th>MEXC Vol %</th>
<th>MEXC 24h Vol</th><th>MEXC Cost %</th><th>Binance Cost %</th><th>MEXC Filter</th><th>Backtest</th></tr></thead>
<tbody>
{''.join(rows_html)}
</tbody></table>
<script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
<script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
<script>$(document).ready(function(){{
  var dt=$('#rank').DataTable({{"pageLength":50,"order":[[0,"asc"]],"dom":"lrtip",
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


@app.get("/file/{filename}")
async def get_file(filename: str, request: Request):
    require_full_access(request)
    fpath = DATA_DIR / filename
    if not fpath.exists() or not fpath.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(str(fpath), filename=filename)


# --- Endpoint: Download a data file converted to CSV on the fly ---
@app.get("/csv/{filename}")
async def get_csv(filename: str, request: Request):
    require_full_access(request)
    fpath = DATA_DIR / filename
    if not fpath.exists() or not fpath.is_file() or fpath.suffix != ".feather":
        raise HTTPException(status_code=404, detail="Feather file not found.")
    try:
        df = pd.read_feather(fpath)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read feather file: {e}")
    csv_data = df.to_csv(index=False)
    out_name = fpath.stem + ".csv"
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("data_server:app", host="0.0.0.0", port=8000, reload=True)
