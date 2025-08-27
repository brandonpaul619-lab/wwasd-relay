# WWASD Relay v2.6 — TV + Port (FastAPI)
# - Adds /tv/latest (green|full|all) with fresh_only filtering
# - Adds /tv/snap to seed list membership (no TV alert changes needed)
# - Keeps /blofin/latest, /port2_ssr.html, /health exactly as before
# - Accepts both WWASD_STATE and BLOFIN_POSITIONS on /tv?token=...

import os, json, time
from typing import Dict, Any, List, Set
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse

app = FastAPI()

# ------------------------ config ------------------------
TV_FRESH_MS = int(os.getenv("TV_FRESH_MS", str(48 * 60 * 60 * 1000)))  # 48h window for "fresh_only=1"
PORT_FRESH_MS = int(os.getenv("PORT_FRESH_MS", str(10 * 60 * 1000)))   # 10m considered "fresh" for port
AUTH_ENV = os.getenv("AUTH_SHARED_SECRET", "").strip()

def _read_secret_file() -> str:
    try:
        with open(".auth_shared_secret", "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""

AUTH_SHARED_SECRET = AUTH_ENV or _read_secret_file()

def now_ms() -> int:
    return int(time.time() * 1000)

# ------------------------ in-memory stores ------------------------
# Latest WWASD state per symbol:  { "BTCUSDT.P": {"ts": 1756..., "data": {...}} }
TV_LAST: Dict[str, Dict[str, Any]] = {}

# List membership (what belongs to 'green' vs 'full')
LISTS: Dict[str, Set[str]] = {
    "green": set(),   # 21
    "full":  set(),   # 144
}

# Port JSON cache: {"fresh": bool, "ts": int, "data": {... or None}}
PORT_JSON: Dict[str, Any] = {"fresh": False, "ts": None, "data": None}

# ------------------------ helpers ------------------------
def _require_token(req: Request):
    token = req.query_params.get("token", "")
    if not AUTH_SHARED_SECRET or token != AUTH_SHARED_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

def _is_fresh(ts_ms: int, window_ms: int) -> bool:
    if not ts_ms:
        return False
    return (now_ms() - int(ts_ms)) <= window_ms

# ------------------------ health ------------------------
@app.get("/health")
async def health():
    return JSONResponse({
        "ok": True,
        "time": int(time.time()),
        "tv_count": len(TV_LAST),
        "port_cached": bool(PORT_JSON.get("data"))
    })

# ------------------------ TV ingest ------------------------
@app.post("/tv")
async def tv_ingest(req: Request):
    _require_token(req)
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    typ = payload.get("type", "")
    ts  = int(payload.get("ts", now_ms()))

    # WWASD_STATE from TradingView
    if typ == "WWASD_STATE":
        sym = payload.get("symbol", "").strip()
        if not sym:
            raise HTTPException(status_code=400, detail="Missing symbol")
        TV_LAST[sym] = {"ts": ts, "data": payload}
        return JSONResponse({"ok": True, "msg": "state accepted", "server_received_ms": now_ms()})

    # BLOFIN_POSITIONS from the Windows bridge
    if typ == "BLOFIN_POSITIONS":
        PORT_JSON["data"] = payload
        PORT_JSON["ts"] = now_ms()
        PORT_JSON["fresh"] = True
        return JSONResponse({"ok": True, "msg": "port accepted", "server_received_ms": now_ms()})

    # unknown
    return JSONResponse({"ok": False, "msg": "ignored type"}, status_code=202)

# ------------------------ TV list snapshot (one-time seeding) ------------------------
# Body example:
# { "lists": { "green": ["BTCUSDT.P","ETHUSDT.P", ...], "full": ["... 144 ..."] } }
@app.post("/tv/snap")
async def tv_snap(req: Request):
    _require_token(req)
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    lists = body.get("lists", {})
    if not isinstance(lists, dict):
        raise HTTPException(status_code=400, detail="lists must be an object")

    # reset + load
    for name in ("green", "full"):
        arr = lists.get(name, [])
        if isinstance(arr, list):
            LISTS[name] = {str(s).strip() for s in arr if str(s).strip()}
    return JSONResponse({"ok": True, "loaded": {k: len(v) for k, v in LISTS.items()}})

# ------------------------ TV latest ------------------------
# GET /tv/latest?lists=green,full&fresh_only=1
# also supports lists=all (union of all seen symbols)
@app.get("/tv/latest")
async def tv_latest(req: Request):
    lists_raw = req.query_params.get("lists", "green").strip().lower()
    fresh_only = req.query_params.get("fresh_only", "1") in ("1", "true", "yes")

    names = [s for s in (x.strip() for x in lists_raw.split(",")) if s]
    if not names:
        names = ["green"]

    # Build response buckets
    resp_lists: Dict[str, Any] = {}

    # Convenience: lists=all => union of everything we've seen
    if names == ["all"]:
        symbols = set(TV_LAST.keys())
        items = []
        for sym in symbols:
            row = TV_LAST[sym]
            if (not fresh_only) or _is_fresh(row.get("ts"), TV_FRESH_MS):
                items.append(row["data"])
        resp_lists["all"] = {"count": len(items), "items": items}
    else:
        for name in names:
            if name not in ("green", "full"):
                resp_lists[name] = {"count": 0, "items": []}
                continue

            wanted = LISTS[name] if LISTS[name] else set()  # may be empty if not snapped yet
            # If not yet snapped, return ANY symbols we've seen, tagged under this bucket,
            # so the desk still has data while you seed lists.
            if not wanted:
                wanted = set(TV_LAST.keys())

            items: List[Dict[str, Any]] = []
            for sym in wanted:
                row = TV_LAST.get(sym)
                if not row:
                    continue
                if fresh_only and not _is_fresh(row.get("ts"), TV_FRESH_MS):
                    continue
                items.append(row["data"])
            resp_lists[name] = {"count": len(items), "items": items}

    return JSONResponse({
        "ts": now_ms(),
        "is_fresh": True,
        "lists": resp_lists
    })

# ------------------------ Port: latest JSON ------------------------
@app.get("/blofin/latest")
async def blofin_latest():
    # Always return a consistent shape for the desk
    return JSONResponse({
        "fresh": bool(PORT_JSON.get("data")) and _is_fresh(PORT_JSON.get("ts") or 0, PORT_FRESH_MS),
        "ts": PORT_JSON.get("ts"),
        "data": PORT_JSON.get("data")
    })

# ------------------------ Port: SSR HTML ------------------------
@app.get("/port2_ssr.html")
async def render_port_html():
    # Light, read-only SSR for the Port Desk (unchanged behavior)
    b = await blofin_latest()
    payload = await b.body()
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except Exception:
        parsed = {"fresh": False, "ts": None, "data": None}

    rows_html = ""
    data = parsed.get("data") or {}
    pdata = data.get("data") or {}
    pos_list = pdata.get("positions") or pdata.get("data") or []  # tolerate both shapes

    if not pos_list:
        rows_html = "<tr><td colspan='7'>No open positions</td></tr>"
    else:
        for p in pos_list:
            sym = p.get("instId") or p.get("instId".lower()) or p.get("symbol") or "?"
            side = p.get("positionSide") or p.get("posSide") or p.get("side") or "net"
            sz   = p.get("positions") or p.get("size") or p.get("positionAmt") or "-"
            avg  = p.get("averagePrice") or p.get("avgPx") or "-"
            mark = p.get("markPrice") or "-"
            lev  = p.get("leverage") or "-"
            rows_html += f"<tr><td>{sym}</td><td>{side}</td><td>{sz}</td><td>{avg}</td><td>{mark}</td><td>{lev}</td></tr>"

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>WWASD Port</title>
<style>
 body{{background:#0b0f14;color:#cfd8e3;font-family:system-ui,Segoe UI,Arial,sans-serif}}
 table{{width:100%;border-collapse:collapse;margin-top:12px}}
 th,td{{padding:8px 10px;border-bottom:1px solid #1b2533}}
 .pill{{display:inline-block;padding:2px 8px;border-radius:12px;background:#1b2533;color:#7bd389;font-size:12px}}
</style></head>
<body>
<h2>WWASD Port <span class="pill">{'fresh' if parsed.get('fresh') else 'stale'}</span></h2>
<table>
<thead><tr><th>Instrument</th><th>Side</th><th>Sz</th><th>Avg</th><th>Mark</th><th>Lev</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>
</body></html>"""
    return HTMLResponse(html)

