# app.py — WWASD Relay v2.6
# - Port: hardened /blofin/latest + SSR (dict out, never raises)
# - TV: restored read endpoints /tv/latest, /snap, /snap_ssr.html
# - Ingest: unchanged (POST /tv?token=... takes WWASD_STATE & BLOFIN_POSITIONS)

import os, time, json, html
from typing import Dict, Any, Optional, List, Set
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

# -------------------- App & CORS --------------------
app = FastAPI(title="wwasd-relay")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False,
    allow_methods=["GET", "POST"], allow_headers=["*"]
)

# -------------------- Config / helpers --------------------
def _strip(s: str) -> str: return (s or "").strip()
def _upper(s: str) -> str: return _strip(s).upper()
def now_ms() -> int: return int(time.time() * 1000)

AUTH_SHARED_SECRET = _strip(os.getenv("AUTH_SHARED_SECRET", ""))

# TV freshness window (seconds) – default matches desk expectations
FRESH_CUTOFF_SECS = int(os.getenv("FRESH_CUTOFF_SECS", "5400"))  # 90m

# Port freshness (seconds)
BLOFIN_TTL_SEC = int(os.getenv("BLOFIN_TTL_SEC", "240"))

def _split_env_list(name: str) -> List[str]:
    raw = os.getenv(name, "")
    raw = raw.replace("\\n", " ").replace("\n", " ")
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    return [t for t in [x.strip() for x in raw.split(",")] if t]

def _norm_variants(sym: str) -> Set[str]:
    out: Set[str] = set()
    s = _upper(sym); out.add(s)
    core = s.split(":", 1)[1] if ":" in s else s; out.add(core)
    if "/" in core: out.add(core.replace("/", ""))  # BTCUSDT.P etc
    else:
        if core.endswith("USDT.P"):
            base = core[:-6]; out.add(f"{base}/USDT.P")
    return out

def _make_selector(name: str) -> Set[str]:
    out: Set[str] = set()
    for t in _split_env_list(name):
        out |= _norm_variants(t)
    return out

GREEN_LIST = _split_env_list("GREEN_LIST")
MACRO_LIST = _split_env_list("MACRO_LIST")
FULL_LIST  = _split_env_list("FULL_LIST")
SEL_GREEN, SEL_MACRO, SEL_FULL = (
    _make_selector("GREEN_LIST"),
    _make_selector("MACRO_LIST"),
    _make_selector("FULL_LIST"),
)

def _fresh_ms(ts_ms: Optional[int], max_age_secs: int) -> bool:
    if ts_ms is None:
        return False
    try:
        ts = int(ts_ms)
    except Exception:
        return False
    return (now_ms() - ts) <= (max_age_secs * 1000)

def _require_token(req: Request) -> None:
    token = req.query_params.get("token") or req.headers.get("X-WWASD-Token")
    if AUTH_SHARED_SECRET and token != AUTH_SHARED_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

# -------------------- In‑memory state --------------------
# TV
_tv_latest: Dict[str, Dict[str, Any]] = {}    # symbol -> last WWASD_STATE (with server_received_ms)
# Port
_blofin_latest: Optional[Dict[str, Any]] = None
_blofin_last_ms: int = 0

# -------------------- Port helpers --------------------
def _extract_positions(any_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Accept variant shapes:
      {"data":{"code":"0","data":[...]}}  (Blofin REST)
      {"data":[...]}, {"positions":[...]}, or nested {"payload":{...}}
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

def _list_selector(name: str) -> Optional[Set[str]]:
    ln = (name or "").lower()
    return SEL_GREEN if ln == "green" else SEL_MACRO if ln == "macro" else SEL_FULL if ln == "full" else None

def _in_named_list(sym: str, list_name: str) -> bool:
    sel = _list_selector(list_name)
    return True if sel is None else bool(_norm_variants(sym) & sel)

# -------------------- Root & Health --------------------
@app.get("/")
async def root():
    return PlainTextResponse(
        "wwasd-relay online\n"
        "POST /tv?token=***  (WWASD_STATE or BLOFIN_POSITIONS)\n"
        "GET  /tv/latest?list=green&fresh_only=1   (TV JSON)\n"
        "GET  /snap?lists=green,macro,full&fresh_only=1  (TV JSON multi)\n"
        "GET  /snap_ssr.html                         (TV SSR)\n"
        "GET  /blofin/latest                         (Port JSON)\n"
        "GET  /port2_ssr.html                        (Port SSR)\n"
        "GET  /health                                (ok,tv_count,port_cached)\n"
    )

@app.get("/health")
async def health():
    return {
        "ok": True,
        "time": int(time.time()),
        "tv_count": len(_tv_latest),
        "port_cached": bool(_blofin_latest),
    }

# -------------------- Ingest (unchanged contract) --------------------
@app.post("/tv")
async def ingest_tv(request: Request):
    """
    Unified webhook for:
      - WWASD_STATE (TradingView alerts)
      - BLOFIN_POSITIONS (local bridge)
    Protected by ?token=. (AUTH_SHARED_SECRET).
    """
    _require_token(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    typ = _upper(body.get("type") or "")
    now = now_ms()

    if typ == "WWASD_STATE":
        sym = _upper(body.get("symbol") or body.get("sym") or "")
        if not sym:
            raise HTTPException(status_code=400, detail="missing symbol")
        body["server_received_ms"] = now
        _tv_latest[sym] = body
        return {"ok": True, "stored": sym}

    if typ == "BLOFIN_POSITIONS":
        global _blofin_latest, _blofin_last_ms
        _blofin_latest = body
        _blofin_last_ms = now
        return {"ok": True, "stored": "blofin_positions"}

    return {"ok": True, "ignored": True}

# -------------------- TV read endpoints --------------------
@app.get("/tv/latest")
async def tv_latest(list: Optional[str] = None, lists: Optional[str] = None,
                    fresh_only: int = 0, max_age_secs: int = FRESH_CUTOFF_SECS):
    """
    Returns the latest WWASD_STATE items, optionally filtered by list=green|macro|full.
    If 'lists' contains commas, mirrors /snap for backward compatibility.
    """
    name = list or lists or ""
    if name and "," in name:   # multi-list → delegate to /snap
        return await snap(lists=name, fresh_only=fresh_only, max_age_secs=max_age_secs)

    items: List[Dict[str, Any]] = []
    for sym, item in _tv_latest.items():
        if name and not _in_named_list(sym, name):
            continue
        out = dict(item)
        ts = out.get("server_received_ms") or out.get("ts")
        out["is_fresh"] = _fresh_ms(ts, max_age_secs)
        items.append(out)
    items.sort(key=lambda x: x.get("symbol", ""))
    if fresh_only:
        items = [it for it in items if it.get("is_fresh")]
    return {"count": len(items), "items": items}

@app.get("/snap")
async def snap(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: int = FRESH_CUTOFF_SECS):
    wanted = [x.strip().lower() for x in lists.split(",") if x.strip()] or ["green"]
    resp: Dict[str, Any] = {"ts": now_ms(), "lists": {}}
    for name in wanted:
        data = await tv_latest(list=name, fresh_only=fresh_only, max_age_secs=max_age_secs)
        resp["lists"][name] = data
    return resp

@app.get("/snap_ssr.html")
async def snap_ssr(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: int = FRESH_CUTOFF_SECS):
    wanted = [x.strip().lower() for x in lists.split(",") if x.strip()] or ["green"]
    snap_json = await snap(lists=",".join(wanted), fresh_only=fresh_only, max_age_secs=max_age_secs)
    parts: List[str] = []
    for name in wanted:
        data = snap_json["lists"].get(name, {"count": 0, "items": []})
        rows = "".join(
            f"<tr><td>{html.escape(it.get('symbol',''))}</td>"
            f"<td>{'fresh' if it.get('is_fresh') else 'stale'}</td></tr>"
            for it in data.get("items", [])
        ) or "<tr><td colspan='2'>No items</td></tr>"
        parts.append(
            f"<h2 style='font:600 14px system-ui;margin:12px 0 6px'>{name.upper()} — count {data.get('count',0)}</h2>"
            f"<table style='width:100%;border-collapse:collapse'>"
            f"<thead><tr><th style='text-align:left;border-bottom:1px solid #1f2937;padding:6px 5px'>Symbol</th>"
            f"<th style='text-align:left;border-bottom:1px solid #1f2937;padding:6px 5px'>Fresh</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    html_doc = (
        "<!doctype html><meta charset='utf-8'/>"
        "<title>WWASD Snap</title>"
        "<body style='background:#0b0f14;color:#e6edf3;font:14px system-ui;padding:16px'>"
        + "".join(parts) + "</body>"
    )
    return HTMLResponse(html_doc, headers={"Cache-Control": "no-store"})

# -------------------- Port JSON --------------------
@app.get("/blofin/latest")
async def blofin_latest():
    """
    Always returns {"fresh":bool,"ts":int|None,"data":dict|None}
    """
    if not _blofin_latest:
        return {"fresh": False, "ts": None, "data": None}
    ts = _blofin_last_ms or _blofin_latest.get("client_ts") or _blofin_latest.get("ts")
    return {
        "fresh": _fresh_ms(int(ts) if ts else None, BLOFIN_TTL_SEC),
        "ts": int(ts) if ts else None,
        "data": _blofin_latest,
    }

# -------------------- Port SSR --------------------
def _fmt(v: Any) -> str:
    return html.escape(str(v)) if v is not None else ""

def _render_port_html(latest: Optional[Dict[str, Any]]) -> str:
    fresh = False
    ts_ms = None
    rows_html = ""
    if latest:
        fresh = bool(latest.get("fresh"))
        ts_ms = latest.get("ts")
        payload = latest.get("data") or {}
        positions = _extract_positions(payload)
        for p in positions:
            inst = p.get("instId") or p.get("symbol") or "?"
            side = (p.get("positionSide") or p.get("posSide") or p.get("side") or "net").upper()
            sz   = p.get("positions") or p.get("pos") or p.get("size") or p.get("qty") or "-"
            avg  = p.get("averagePrice") or p.get("avgPx") or p.get("avg") or "-"
            mark = p.get("markPrice") or p.get("markPx") or p.get("mark") or "-"
            lev  = p.get("leverage") or p.get("lever") or "-"
            rows_html += f"<tr><td>{_fmt(inst)}</td><td>{_fmt(side)}</td><td>{_fmt(sz)}</td><td>{_fmt(avg)}</td><td>{_fmt(mark)}</td><td>{_fmt(lev)}</td></tr>"

    ts_txt = "-" if not ts_ms else time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(ts_ms)/1000))
    pill = '<span style="padding:.15rem .45rem;border-radius:.5rem;font-size:.8rem;background:#2a6c2a;color:#dff0d8">fresh</span>' \
           if fresh else '<span style="padding:.15rem .45rem;border-radius:.5rem;font-size:.8rem;background:#5a5a5a;color:#eee">stale</span>'
    table = rows_html or '<tr><td colspan="6" style="opacity:.6">No open positions</td></tr>'

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
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
  <h1>WWASD Port <span class="sub">last update (server): {ts_txt} {pill}</span></h1>
  <table>
    <thead><tr><th>Instrument</th><th>Side</th><th>Sz</th><th>Avg</th><th>Mark</th><th>Lev</th></tr></thead>
    <tbody>{table}</tbody>
  </table>
</div></body></html>"""

# Aliases for the SSR view
@app.get("/port.html")
async def port_html():        return HTMLResponse(_render_port_html(await blofin_latest()))
@app.get("/port2.html")
async def port2_html():       return HTMLResponse(_render_port_html(await blofin_latest()))
@app.get("/port_ssr.html")
async def port_ssr_html():    return HTMLResponse(_render_port_html(await blofin_latest()))
@app.get("/port2_ssr.html")
async def port2_ssr_html():   return HTMLResponse(_render_port_html(await blofin_latest()))
