# app.py — WWASD Relay (tolerant /tv ingest, no behavior surprises)
# - Auth via ?token= accepts AUTH_SHARED_SECRET or AUTL_SHARED_SECRET
# - Normalizes TradingView payloads:
#     * unwraps "message":"{...}"
#     * if data is a JSON string -> json.loads
#     * accepts sym/l -> symbol/list
#     * ensures tf is a string ("5","15","60",...)
# - Keeps default freshness windows as you run them today.
#
# Endpoints:
#   POST /tv                -> ingest WWASD_STATE (and friends)
#   GET  /tv/latest         -> recent TV events (with "fresh" flag)
#   GET  /blofin/latest     -> latest Port block (default 15m freshness)
#   POST /blofin/push       -> optional authenticated port pusher
#   GET  /snap              -> grouped snapshot for WWASD HTML
#   GET  /port2_ssr.html    -> simple SSR port view

import os, json, time
from typing import Any, Dict, List, Tuple
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse

app = FastAPI(title="wwasd-relay")

# ----------------------------- config ---------------------------------
# Site-wide "fresh" window for TV items (unchanged from your current default).
FRESH_CUTOFF_SECS = int(os.getenv("FRESH_CUTOFF_SECS", "5400"))  # 90 minutes

# Accept either name for the shared secret (matches your Render env)
AUTH_SHARED_SECRET = (
    os.getenv("AUTH_SHARED_SECRET")
    or os.getenv("AUTL_SHARED_SECRET")
    or ""
)

# ----------------------------- in-memory stores -----------------------
_tv_items: List[Dict[str, Any]] = []       # raw (but normalized) TV events
_tv_ts: float = 0.0

_port_obj: Dict[str, Any] = {"positions": []}  # whatever your pusher sends
_port_ts: float = 0.0

def _now() -> float:
    return time.time()

def _is_fresh(ts: float, window: int) -> bool:
    return ts and (_now() - ts) <= window

# ----------------------------- helpers --------------------------------
def _unwrap_tradingview(payload: Any) -> Any:
    """Unwrap TV 'message' wrapper and de-stringify inner data if needed."""
    try:
        if isinstance(payload, dict) and "message" in payload:
            try:
                payload = json.loads(payload["message"])
            except Exception:
                # keep as-is if not json
                pass
        # decode inner data if it's a JSON string
        if isinstance(payload, dict) and isinstance(payload.get("data"), str):
            try:
                payload["data"] = json.loads(payload["data"])
            except Exception:
                pass
        # normalize variants
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            d = payload["data"]
            if "symbol" not in d and "sym" in d:
                d["symbol"] = d["sym"]
            if "list" not in d and "l" in d:
                d["list"] = d["l"]
            if "tf" in d:
                d["tf"] = str(d["tf"])
            payload["data"] = d
    except Exception:
        # never explode on bad inputs
        pass
    return payload

def _project_item(p: Dict[str, Any]) -> Dict[str, Any]:
    """Return a compact view for /snap consumers."""
    t = p.get("type")
    d = p.get("data") if isinstance(p.get("data"), dict) else {}
    return {
        "type": t,
        "list": d.get("list"),
        "symbol": d.get("symbol"),
        "tf": d.get("tf"),
        "ts": p.get("ts", _now()),
        "state": d.get("state"),   # if your Pine includes it
    }

# ----------------------------- endpoints ------------------------------
@app.post("/tv")
async def tv_ingest(req: Request):
    # ---- auth
    token = req.query_params.get("token", "")
    if AUTH_SHARED_SECRET and token != AUTH_SHARED_SECRET:
        raise HTTPException(status_code=403, detail="invalid token")

    # ---- parse body (JSON or raw)
    try:
        payload = await req.json()
    except Exception:
        raw = (await req.body()).decode("utf-8", "ignore")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw}

    # ---- normalize
    payload = _unwrap_tradingview(payload)

    # ---- detect/optionally capture port pushes that arrive here
    # If your pusher sends {"type":"BLOFIN_POSITIONS","data":{...port...}}
    try:
        if isinstance(payload, dict) and payload.get("type") == "BLOFIN_POSITIONS":
            global _port_obj, _port_ts
            d = payload.get("data") or {}
            _port_obj = d if isinstance(d, dict) else {"positions": []}
            _port_ts = _now()
    except Exception:
        pass

    # ---- store TV event (keep last 400)
    global _tv_items, _tv_ts
    payload["ts"] = _now()
    _tv_items.append(payload)
    _tv_items = _tv_items[-400:]
    _tv_ts = payload["ts"]

    # minimal = looks like a real WWASD emitter event
    minimal = (
        isinstance(payload, dict)
        and isinstance(payload.get("data"), dict)
        and bool(payload["data"].get("symbol"))
        and bool(payload["data"].get("tf"))
    )
    return JSONResponse({"ok": True, "stored": minimal})

@app.get("/tv/latest")
def tv_latest():
    return JSONResponse({
        "fresh": _is_fresh(_tv_ts, FRESH_CUTOFF_SECS),
        "cutoff_secs": FRESH_CUTOFF_SECS,
        "ts": _tv_ts,
        "items": _tv_items,
    })

@app.get("/blofin/latest")
def blofin_latest(max_age_secs: int = 900):
    """Port block for SSR/HTML; default 15m window (unchanged)."""
    return JSONResponse({
        "fresh": _is_fresh(_port_ts, max_age_secs),
        "ts": _port_ts,
        "port": _port_obj,
    })

@app.post("/blofin/push")
async def blofin_push(req: Request):
    """Optional authenticated pusher for positions (use token=?)."""
    token = req.query_params.get("token", "")
    if AUTH_SHARED_SECRET and token != AUTH_SHARED_SECRET:
        raise HTTPException(status_code=403, detail="invalid token")
    try:
        body = await req.json()
    except Exception:
        body = {}
    global _port_obj, _port_ts
    _port_obj = body if isinstance(body, dict) else {"positions": []}
    _port_ts = _now()
    return JSONResponse({"ok": True, "ts": _port_ts})

@app.get("/snap")
def snap(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: int = None):
    """
    Snapshot used by the WWASD HTML.
    - groups TV items by list name ("green","macro","full")
    - respects fresh_only: if 1, filters by (max_age_secs or FRESH_CUTOFF_SECS)
    """
    wanted = [s.strip() for s in lists.split(",") if s.strip()]
    age = max_age_secs if (fresh_only and max_age_secs) else (FRESH_CUTOFF_SECS if fresh_only else 10**9)
    cutoff = _now() - age if fresh_only else 0

    grouped: Dict[str, List[Dict[str, Any]]] = {k: [] for k in wanted}
    for it in _tv_items:
        ts = it.get("ts", 0)
        if fresh_only and ts < cutoff:
            continue
        d = it.get("data") or {}
        li = (d.get("list") or d.get("l"))
        if li in grouped:
            grouped[li].append(_project_item(it))

    out = {
        "ts": _now(),
        "tv_cutoff_secs": FRESH_CUTOFF_SECS,
        "fresh_only": bool(fresh_only),
        "lists": wanted,
        "port": {
            "fresh": _is_fresh(_port_ts, 900),
            "ts": _port_ts,
            "data": _port_obj,
        }
    }
    for k in wanted:
        items = grouped.get(k, [])
        out[k] = {"count": len(items), "items": items}
    return JSONResponse(out)

@app.get("/port2_ssr.html")
def port_ssr():
    """Very small SSR page for quick eyeballing of the port."""
    fresh = _is_fresh(_port_ts, 900)
    badge = "fresh" if fresh else "stale"
    rows = []
    positions = (_port_obj or {}).get("positions") or []
    if positions:
        for p in positions:
            rows.append(
                f"<tr><td>{p.get('symbol','')}</td>"
                f"<td>{p.get('type','')}</td>"
                f"<td>{p.get('side','')}</td>"
                f"<td>{p.get('qty','')}</td>"
                f"<td>{p.get('avg','')}</td>"
                f"<td>{p.get('mark','')}</td>"
                f"<td>{p.get('upnl','')}</td>"
                f"<td>{p.get('x','')}</td>"
                f"<td>{p.get('liq','')}</td></tr>"
            )
    else:
        rows.append('<tr><td colspan="9">No open positions</td></tr>')

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>WWASD Port (SSR)</title>
<style>
body{{font-family:ui-sans-serif,system-ui,Segoe UI,Arial;margin:18px}}
.badge{{display:inline-block;padding:3px 7px;border-radius:6px;color:#fff;background:{'#16a34a' if fresh else '#f59e0b'};}}
table{{border-collapse:collapse;width:100%;margin-top:10px}}
th,td{{border:1px solid #ddd;padding:6px;text-align:left;white-space:nowrap}}
</style></head><body>
<h2>WWASD Port (SSR) <span class="badge">{badge}</span></h2>
<div>ts: {(_port_ts and int(_port_ts)) or 'None'}</div>
<table>
<tr><th>Symbol</th><th>Type</th><th>Side</th><th>Qty</th><th>Avg</th><th>Mark</th><th>uPnL</th><th>x</th><th>Liq</th></tr>
{''.join(rows)}
</table>
</body></html>"""
    return HTMLResponse(html)

