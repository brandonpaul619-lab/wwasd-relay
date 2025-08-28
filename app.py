# app.py — WWASD Relay v2.6
# - Port: clean /blofin/latest (positions[] + count) while retaining raw payload
# - TV: /tv/latest and /snap read endpoints present
# - SSR pages unchanged; resilient against shape drift

import os, time, json, html
from typing import Dict, Any, Optional, List, Set
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

# -------------------- App & CORS --------------------
app = FastAPI(title="wwasd-relay")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False,
    allow_methods=["GET", "POST"], allow_headers=["*"]
)

# -------------------- Config --------------------
def _strip(s: str) -> str: return (s or "").strip()
def _upper(s: str) -> str: return _strip(s).upper()

AUTH_SHARED_SECRET = _strip(os.getenv("AUTH_SHARED_SECRET", ""))

# TV freshness window (seconds) for “fresh_only=1”
TV_FRESH_SECS = int(os.getenv("FRESH_CUTOFF_SECS", "5400"))  # 90 minutes default

# Port freshness window (milliseconds)
PORT_FRESH_MS = int(os.getenv("FRESH_CUTOFF_MS", "300000"))  # 5 minutes default

# Environment lists (Render → Environment)
def _split_env_list(name: str) -> List[str]:
    raw = os.getenv(name, "")
    raw = raw.replace("\\n", " ").replace("\n", " ")
    if raw.startswith('"') and raw.endswith('"'): raw = raw[1:-1]
    return [t for t in [x.strip() for x in raw.split(",")] if t]

def _norm_variants(sym: str) -> Set[str]:
    out: Set[str] = set()
    s = _upper(sym); out.add(s)
    core = s.split(":", 1)[1] if ":" in s else s; out.add(core)
    if "/" in core: out.add(core.replace("/", ""))           # BTCUSDT.P
    else:
        if core.endswith("USDT.P") and "/" not in core:
            base = core[:-6]; out.add(f"{base}/USDT.P")      # BTC/USDT.P
    return out

def _make_selector(name: str) -> Set[str]:
    out: Set[str] = set()
    for t in _split_env_list(name): out |= _norm_variants(t)
    return out

GREEN_LIST = _split_env_list("GREEN_LIST")
MACRO_LIST = _split_env_list("MACRO_LIST")
FULL_LIST  = _split_env_list("FULL_LIST")
SEL_GREEN, SEL_MACRO, SEL_FULL = _make_selector("GREEN_LIST"), _make_selector("MACRO_LIST"), _make_selector("FULL_LIST")

# -------------------- In‑memory stores --------------------
# TV
_tv_latest: Dict[str, Dict[str, Any]] = {}    # symbol -> last WWASD_STATE (with server_received_ms)
# Port
_blofin_latest: Optional[Dict[str, Any]] = None   # last BLOFIN_POSITIONS push (raw)
_blofin_last_ms: int = 0

# -------------------- Utils --------------------
def now_ms() -> int: return int(time.time() * 1000)

def is_fresh(ts_ms: Optional[int], window_ms: int) -> bool:
    if not ts_ms: return False
    try: return (now_ms() - int(ts_ms)) <= int(window_ms)
    except Exception: return False

def _require_token(req: Request) -> None:
    token = req.query_params.get("token") or req.headers.get("X-WWASD-Token")
    if not AUTH_SHARED_SECRET or token != AUTH_SHARED_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

# -------------------- Port helpers --------------------
def _extract_positions(any_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Accept variant shapes:
      {"data":{"code":"0","data":[...]}}  (Blofin REST)
      {"data":[...]} or {"positions":[...]} or {"payload":{...}}
    """
    if not any_payload:
        return []
    data = any_payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    if isinstance(data, list):
        return data
    if isinstance(any_payload.get("positions"), list):
        return any_payload["positions"]
    inner = any_payload.get("payload")
    if isinstance(inner, dict):
        return _extract_positions(inner)
    return []

# -------------------- Routes: root & health --------------------
@app.get("/")
async def root():
    return PlainTextResponse(
        "wwasd-relay online\n"
        "POST /tv?token=***  (WWASD_STATE or BLOFIN_POSITIONS)\n"
        "GET  /tv/latest?lists=green,macro,full&fresh_only=1  (TV JSON)\n"
        "GET  /snap?lists=green,macro,full&fresh_only=1       (TV JSON alias)\n"
        "GET  /blofin/latest  (Port JSON: fresh, ts, count, positions[], data)\n"
        "GET  /port2_ssr.html (Port SSR)\n"
        "GET  /health         (ok,tv_count,port_cached)\n"
    )

@app.get("/health")
async def health():
    return {
        "ok": True,
        "time": int(time.time()),
        "tv_count": len(_tv_latest),
        "port_cached": _blofin_latest is not None,
    }

# -------------------- Ingest (unchanged contract) --------------------
@app.post("/tv")
async def ingest_tv(request: Request):
    """
    Unified webhook for:
      - WWASD_STATE (TradingView alerts)
      - BLOFIN_POSITIONS (local bridge)
    Protected by ?token=... (AUTH_SHARED_SECRET).
    """
    _require_token(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    typ = (body.get("type") or "").upper()
    now = now_ms()

    if typ == "WWASD_STATE":
        sym = _upper(body.get("symbol") or "")
        if not sym:
            raise HTTPException(status_code=400, detail="missing symbol")
        body["server_received_ms"] = now
        _tv_latest[sym] = body
        return {"ok": True, "stored": sym}

    if typ == "BLOFIN_POSITIONS":
        global _blofin_latest, _blofin_last_ms
        _blofin_latest = body
        _blofin_last_ms = body.get("client_ts") or body.get("ts") or now
        return {"ok": True, "stored": "blofin_positions"}

    return {"ok": True, "ignored": True}

# -------------------- TV read endpoints (restored) --------------------
def _list_selector(name: str) -> Optional[Set[str]]:
    ln = (name or "").lower()
    return SEL_GREEN if ln == "green" else SEL_MACRO if ln == "macro" else SEL_FULL if ln == "full" else None

def _in_named_list(sym: str, list_name: str) -> bool:
    sel = _list_selector(list_name)
    return True if sel is None else bool(_norm_variants(sym) & sel)

def _tv_collect(list_name: str, fresh_only: int, max_age_secs: int) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    max_age_ms = max(1, int(max_age_secs)) * 1000
    for sym, item in _tv_latest.items():
        if list_name and not _in_named_list(sym, list_name): 
            continue
        out = dict(item)
        out["symbol"] = sym
        ts = item.get("server_received_ms") or item.get("ts")
        out["is_fresh"] = is_fresh(ts, max_age_ms)
        if fresh_only and not out["is_fresh"]:
            continue
        items.append(out)
    items.sort(key=lambda x: x.get("symbol",""))
    return {"count": len(items), "items": items}

@app.get("/tv/latest")
async def tv_latest(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: int = TV_FRESH_SECS):
    wanted = [ _strip(x).lower() for x in lists.split(",") if _strip(x) ] or ["green"]
    resp: Dict[str, Any] = {"ts": now_ms(), "lists": {}}
    for name in wanted:
        resp["lists"][name] = _tv_collect(name, fresh_only, max_age_secs)
    return resp

@app.get("/snap")
async def snap(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: int = TV_FRESH_SECS):
    r    resp = await tv_latest(lists=lists, fresh_only=fresh_only, max_age_secs=max_age_secs)
    return resp

# -------------------- Port JSON (bot‑safe, clean) --------------------
@app.get("/blofin/latest")
async def blofin_latest():
    """
    Clean Port JSON that never raises.
    Returns:
      {
        "fresh": bool,
        "ts": int|None,
        "count": int,
        "positions": [ ... simplified per‑instrument dicts ... ],
        "data": { ...raw pusher payload... }   # for SSR/diagnostics
      }
    """
    if not _blofin_latest:
        return {"fresh": False, "ts": None, "count": 0, "positions": [], "data": None}

    ts = _blofin_last_ms or _blofin_latest.get("client_ts") or _blofin_latest.get("ts")
    ts = int(ts) if ts else None
    positions = _extract_positions(_blofin_latest)

    # Hand back both the clean list (positions) and the original blob (data)
    return {
        "fresh": is_fresh(ts, PORT_FRESH_MS),
        "ts": ts,
        "count": len(positions),
        "positions": positions,
        "data": _blofin_latest
    }

# -------------------- SSR (Port HTML) --------------------
def _fmt(v: Any) -> str:
    return html.escape(str(v)) if v is not None else ""

def _render_port_html(latest: Optional[Dict[str, Any]]) -> str:
    fresh = False
    ts_ms = None
    rows_html = ""

    if latest:
        # accept both shapes (new: positions[], old: data→positions[])
        fresh = bool(latest.get("fresh"))
        ts_ms = latest.get("ts")
        positions = latest.get("positions") or _extract_positions(latest.get("data") or {})

        for p in positions:
            inst = p.get("instId") or p.get("symbol") or "?"
            side = (p.get("positionSide") or p.get("posSide") or p.get("side") or "net").upper()
            sz   = p.get("positions") or p.get("pos") or p.get("size") or p.get("qty") or "-"
            avg  = p.get("averagePrice") or p.get("avgPx") or p.get("avg") or "-"
            mark = p.get("markPrice") or p.get("markPx") or p.get("mark") or "-"
            lev  = p.get("leverage") or p.get("lever") or "-"
            rows_html += f"<tr><td>{_fmt(inst)}</td><td>{_fmt(side)}</td><td>{_fmt(sz)}</td><td>{_fmt(avg)}</td><td>{_fmt(mark)}</td><td>{_fmt(lev)}</td></tr>"

    ts_txt = "-" if not ts_ms else time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(ts_ms)/1000))
    pill   = '<span style="padding:.15rem .45rem;border-radius:.5rem;font-size:.8rem;background:#2a6c2a;color:#dff0d8">fresh</span>' \
             if fresh else '<span style="padding:.15rem .45rem;border-radius:.5rem;font-size:.8rem;background:#5a5a5a;color:#eee">stale</span>'
    table = rows_html or '<tr><td colspan="6" style="opacity:.6">No open positions</td></tr>'

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WWASD Port</title>
<style>
 body{{background:#0f1115;color:#e6e6e6;font:14px/1.4 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Inter,Arial}}
 .wrap{{max-width:980px;margin:24px auto;padding:0 16px}}
 h1{{font-size:18px;margin:0 0 8px}}
 table{{width:100%;border-collapse:collapse;border-spacing:0;margin-top:12px}}
 th,td{{padding:8px 10px;border-bottom:1px solid #222;white-space:nowrap}}
 th{{text-align:left;color:#a9b0bc;font-weight:600}}
 .sub{{color:#8a92a6;font-size:.9rem}}
</style>
</head><body><div class="wrap">
  <h1>WWASD Port <span class="sub">last update (server): { _fmt(ts_txt) } {pill}</span></h1>
  <table>
    <thead><tr><th>Instrument</th><th>Side</th><th>Sz</th><th>Avg</th><th>Mark</th><th>Lev</th></tr></thead>
    <tbody>{table}</tbody>
  </table>
</div></body></html>"""

# SSR aliases
@app.get("/port2.html")     async def port2_html():     return HTMLResponse(_render_port_html(await blofin_latest()))
@app.get("/port2_ssr.html") async def port2_ssr_html(): return HTMLResponse(_render_port_html(await blofin_latest()))
@app.get("/port.html")      async def port_html():      return HTMLResponse(_render_port_html(await blofin_latest()))
@app.get("/port_ssr.html")  async def port_ssr_html():  return HTMLResponse(_render_port_html(await blofin_latest()))

