"""
FastAPI server to broadcast access to Binance futures data files and provide a summary endpoint.
Token authentication required for all endpoints.
"""
import os
import sys
import json
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict
import pandas as pd
from pathlib import Path
from datetime import datetime
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

app = FastAPI(title="Binance Futures Data Server")

# Allow CORS for local dev (adjust as needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def check_token(request: Request):
    token = get_request_token(request)
    if token != TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token.")
    return token


def get_request_token(request: Request) -> str:
    """Accept token from header or query parameter."""
    token = request.headers.get("x-access-token")
    if not token:
        token = request.query_params.get("token")
    return token or ""


def with_token(path: str, token: str) -> str:
    """Append token as query param to a relative URL path."""
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}token={quote_plus(token)}"


def coin_links(coin: str, token: str) -> str:
    """Coin name linking to its filtered summary on this server, plus a TradingView chart link."""
    asset_url = with_token(f"/summary?showmeasset={coin}", token)
    tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{coin}USDT.P"
    return (
        f'<a href="{asset_url}">{coin}</a> '
        f'<a href="{tv_url}" target="_blank" rel="noopener" title="View {coin} on TradingView">&#128200;</a>'
    )

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

# --- Helper: Get file time span ---
def get_time_span(fpath: Path):
    # Try to find a suitable time column
    for col in ["timestamp", "date", "time"]:
        try:
            df = pd.read_feather(fpath, columns=[col])
            if df.empty or col not in df:
                continue
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
            return start, end
        except Exception:
            continue
    return None, None

# --- Endpoint: Summary ---
from fastapi.responses import HTMLResponse


# --- Endpoint: Landing page / navigation menu ---
@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    token = check_token(request)
    feather_files = [f for f in DATA_DIR.iterdir() if f.suffix == ".feather"]
    n_files = len(feather_files)
    coins = sorted({p[0] for p in (parse_filename(f.name) for f in feather_files) if p})
    coin_options = "".join(f'<option value="{c}">{c}</option>' for c in coins)
    nav = [
        ("Data Summary", "Full file table: coin, timeframe, type, date range, age. Click a filename to download.", with_token("/summary", token)),
        ("Full Binance Ranking", "Every Binance futures perpetual ranked, filtered by volume &amp; spread, with a volatility index. Green = tradeable.", with_token("/binance-ranking", token)),
        ("Raw JSON Summary", "Machine-readable summary of every data file.", with_token("/summary.json", token)),
    ]
    cards = "".join(
        f'<a class="card" href="{url}"><h3>{title}</h3><p>{desc}</p></a>'
        for title, desc, url in nav
    )
    html = f"""<html><head><title>Binance Futures Data Server</title>
<style>
body{{font-family:sans-serif;max-width:820px;margin:40px auto;padding:0 20px;color:#222;}}
h1{{margin-bottom:4px;}} .sub{{color:#777;margin-top:0;}}
.card{{display:block;border:1px solid #ddd;border-radius:8px;padding:16px 18px;margin:12px 0;text-decoration:none;color:#222;transition:.15s;}}
.card:hover{{border-color:#888;background:#fafafa;}}
.card h3{{margin:0 0 6px;}} .card p{{margin:0;color:#666;font-size:14px;}}
.jump{{margin:20px 0;padding:16px 18px;border:1px solid #ddd;border-radius:8px;background:#f7f7f7;}}
.jump select{{padding:6px 10px;font-size:14px;border:1px solid #bbb;border-radius:4px;min-width:160px;}}
.jump button{{padding:6px 14px;font-size:14px;border:0;border-radius:4px;background:#3367d6;color:#fff;cursor:pointer;}}
</style></head><body>
<h1>Binance Futures Data Server</h1>
<p class="sub">{n_files} data files &middot; OHLCV, mark &amp; funding-rate (feather)</p>
{cards}
<div class="jump">
  <strong>Jump to a single coin</strong><br>
  <p style="color:#666;font-size:14px;margin:6px 0 10px;">Pick an asset to view its files ({len(coins)} coins on file).</p>
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
  location.href = '/summary?showmeasset=' + encodeURIComponent(c) + '&token=' + encodeURIComponent(TOKEN);
}}
</script>
</body></html>"""
    return html


@app.get("/summary", response_class=HTMLResponse)
async def summary(request: Request):
    token = check_token(request)
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
        start, end = get_time_span(f)
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
            "days": days
        })
    # Build HTML table
    html = [
        "<html><head><title>Binance Futures Data Summary</title>",
        # DataTables CSS
        '<link rel="stylesheet" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css">',
        '<style>body{font-family:sans-serif;}table{margin-top:20px;}button, a.json-btn{margin:10px 0;padding:6px 12px;background:#eee;border-radius:4px;text-decoration:none;color:#333;display:inline-block;}</style>',
        "</head><body>",
        '<a href="' + with_token('/', token) + '" class="json-btn">&#8962; Home</a>',
        '<a href="' + with_token('/summary.json' + (f'?showmeasset={asset}' if asset else ''), token) + '" class="json-btn">View Raw JSON Summary</a>',
        ('<a href="' + with_token('/summary', token) + '" class="json-btn">Show All Coins</a>') if asset else '',
        f"<h2>Binance Futures Data Summary{(' &mdash; ' + asset) if asset else ''}</h2>",
        '<table id="summary" class="display">',
        "<thead><tr><th>Coin</th><th>Timeframe</th><th>Type</th><th>Year</th><th>Months</th><th>Days</th><th>Date Range</th><th>Filename</th><th>File Age</th></tr></thead><tbody>"
    ]
    now = datetime.utcnow().timestamp()
    for coin in sorted(summary.keys()):
        for tf in sorted(summary[coin].keys()):
            for entry in sorted(summary[coin][tf], key=lambda x: (x["type"], x["file"])):
                file_url = with_token(f"/file/{entry['file']}", token)
                csv_url = with_token(f"/csv/{entry['file']}", token)
                # Get file age in days
                fpath = DATA_DIR / entry['file']
                try:
                    mtime = fpath.stat().st_mtime
                    age_days = int((now - mtime) // 86400)
                except Exception:
                    age_days = ''
                # Format date range
                def fmt(ts):
                    if ts is None:
                        return ''
                    try:
                        return datetime.utcfromtimestamp(ts/1000).strftime('%Y-%b-%d')
                    except Exception:
                        return str(ts)
                date_range = f"{fmt(entry['start'])} → {fmt(entry['end'])}" if entry['start'] and entry['end'] else ''
                html.append(
                    f"<tr>"
                    f"<td>{coin_links(coin, token)}</td>"
                    f"<td>{tf}</td>"
                    f"<td>{entry['type']}</td>"
                    f"<td>{entry['year'] if entry['year'] is not None else ''}</td>"
                    f"<td>{entry['months'] if entry['months'] is not None else ''}</td>"
                    f"<td>{entry['days'] if entry['days'] is not None else ''}</td>"
                    f"<td>{date_range}</td>"
                    f"<td><a href='{file_url}'>{entry['file']}</a> &nbsp;|&nbsp; <a href='{csv_url}'>CSV</a></td>"
                    f"<td>{age_days} days old</td>"
                    f"</tr>"
                )
    html.append("</tbody></table>")
    # DataTables JS (only for summary table)
    html.append('''
<script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
<script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
<script>
$(document).ready(function() {
    $('#summary').DataTable({"pageLength":50});
});
</script>
''')
    html.append("</body></html>")
    return "\n".join(html)
@app.get("/summary.json")
async def summary_json(request: Request):
    check_token(request)
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
        start, end = get_time_span(f)
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
            "days": days
        })
    # Sort coins, then timeframes
    sorted_summary = {k: dict(sorted(v.items())) for k, v in sorted(summary.items())}
    return JSONResponse(content=sorted_summary)


# --- Endpoint: Download file ---
BINANCE_RANKING_FILE = Path(__file__).resolve().parent / "binance_ranking.json"


# --- Endpoint: Full Binance futures ranking (whole universe) ---
@app.get("/binance-ranking", response_class=HTMLResponse)
async def binance_ranking(request: Request):
    token = check_token(request)
    home = with_token("/", token)
    if not BINANCE_RANKING_FILE.exists():
        return (f'<html><body style="font-family:sans-serif;max-width:820px;margin:40px auto;">'
                f'<a href="{home}">&#8962; Home</a><h2>No Binance ranking yet</h2>'
                f'<p>Run <code>python3 build_binance_ranking.py</code> to generate it.</p></body></html>')
    data = json.loads(BINANCE_RANKING_FILE.read_text())
    rows_html = []
    for r in data.get("rows", []):
        good = r["good"]
        bg = "#e7f6e7" if good else "#fdeaea"
        flag = ('<span style="color:#1a7f37;font-weight:bold">GOOD</span>' if good
                else '<span style="color:#c0392b;font-weight:bold">no</span>')
        vol = r.get("volatility_pct")
        vol_cell = f"<td data-order='{vol if vol is not None else -1}'>{vol:.2f}</td>" if vol is not None else "<td data-order='-1'>-</td>"
        qv = r["quote_volume"]
        rows_html.append(
            f"<tr style='background:{bg}'>"
            f"<td>{r['rank']}</td>"
            f"<td>{coin_links(r['coin'], token)}</td>"
            f"<td>{r['spread_pct']:.4f}</td>"
            f"<td>{r['fee_roundtrip_pct']:.2f}</td>"
            f"<td>{r['total_cost_pct']:.4f}</td>"
            f"{vol_cell}"
            f"<td data-order='{qv:.0f}'>{qv/1e6:,.2f}M</td>"
            f"<td>{'&#10003;' if r['in_pairlist'] else ''}</td>"
            f"<td data-order='{1 if good else 0}'>{flag}</td></tr>"
        )
    html = f"""<html><head><title>Full Binance Futures Ranking</title>
<link rel="stylesheet" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css">
<style>body{{font-family:sans-serif;}}table{{margin-top:20px;}}
a.btn{{margin:10px 8px 10px 0;padding:6px 12px;background:#eee;border-radius:4px;text-decoration:none;color:#333;display:inline-block;}}
a.dl{{background:#1a7f37;color:#fff;}} .meta{{color:#666;font-size:14px;}}</style></head><body>
<a href="{home}" class="btn">&#8962; Home</a>
<a href="{with_token('/summary', token)}" class="btn">Data Summary</a>
<a href="{with_token('/binance-good-pairs.json', token)}" class="btn dl">Download GOOD pairs (JSON)</a>
<h2>Full Binance Futures Ranking</h2>
<p class="meta">All {data.get('total_symbols')} live USDⓈ-M PERPETUAL symbols, ranked by round-trip cost
(spread % + {data.get('fees',{}).get('roundtrip_taker_pct')}% fee).
<b style="color:#1a7f37">GOOD</b> = 24h volume &ge; {data.get('min_volume'):,.0f} USDT
AND spread &le; {data.get('max_spread_pct')}%
AND volatility &ge; {data.get('min_volatility_pct')}%.
<b>{data.get('count_good')}</b> of {data.get('total_symbols')} qualify.
Volatility = 24h (high-low)/avg %.<br>Generated {data.get('generated_utc')} UTC.</p>
<table id="rank" class="display">
<thead><tr><th>Rank</th><th>Coin</th><th>Spread %</th><th>Fee RT %</th><th>Total Cost %</th>
<th>Volatility %</th><th>24h Vol</th><th>In List</th><th>Good</th></tr></thead>
<tbody>
{''.join(rows_html)}
</tbody></table>
<script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
<script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
<script>$(document).ready(function(){{$('#rank').DataTable({{"pageLength":50,"order":[[0,"asc"]]}});}});</script>
</body></html>"""
    return html


# --- Endpoint: the "good" coins as a downloadable freqtrade pairlist ---
@app.get("/binance-good-pairs.json")
async def binance_good_pairs(request: Request):
    check_token(request)
    if not BINANCE_RANKING_FILE.exists():
        raise HTTPException(status_code=404, detail="Ranking not generated yet.")
    data = json.loads(BINANCE_RANKING_FILE.read_text())
    pairs = [f"{r['coin']}/USDT:USDT" for r in data.get("rows", []) if r.get("good")]
    return JSONResponse({"pairs": pairs, "count": len(pairs), "generated_utc": data.get("generated_utc")})


@app.get("/file/{filename}")
async def get_file(filename: str, request: Request):
    check_token(request)
    fpath = DATA_DIR / filename
    if not fpath.exists() or not fpath.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(str(fpath), filename=filename)


# --- Endpoint: Download a data file converted to CSV on the fly ---
@app.get("/csv/{filename}")
async def get_csv(filename: str, request: Request):
    check_token(request)
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
