# app.py — WWASD Relay v2.6 (additive, TV latest restored; port untouched)

import os, time, json
from typing import Dict, Any, List
from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

def now_ms() -> int:
    return int(time.time() * 1000)

app = FastAPI(title="wwasd-relay")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

AUTH = os.environ.get("AUTH_SHARED_SECRET", "")

# ----------------------------
# In‑memory stores (stateless app)
# ----------------------------
STATE: Dict[str, Any] = {
    "tv_by_symbol": {},     # symbol -> last WWASD_STATE (or BLOFIN_POSITIONS)
    "lists": {              # optional hard lists; if empty we fallback to "everything we've seen"
        "green": set(),     # can be filled via /tv/snap (optional)
        "full":  set(),
    },
    "last_blofin_ts": None, # ms
}

# ----------------------------
# Health (unchanged contract)
# ----------------------------
@app.get("/health")
async def health():
    return {
        "ok": True,
        "time": int(time.time()),
        "tv_count": len(STATE["tv_by_symbol"]),
        "port_cached": STATE["last_blofin_ts"] is not None,
    }

# ----------------------------
# TV intake  (TradingView webhook posts here)
# Accepts BOTH:
#  - type="WWASD_STATE"  (from WWASD_State_Emitter)
#  - type="BLOFIN_POSITIONS" (from your port pusher)
# ----------------------------
@app.post("/tv")
async def tv_ingest(request: Request, token: str = ""):
    if AUTH and token != AUTH:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    # tolerate text/plain or invalid headers
    try:
        payload = await request.json()
    except Exception:
        body = (await request.body()).decode("utf-8", "ignore")
        try:
            payload = json.loads(body)
        except Exception:
            return JSONResponse({"ok": False, "error": "bad_json"}, status_code=400)

    if not isinstance(payload, dict) or "type" not in payload:
        return JSONResponse({"ok": False, "error": "bad_payload"}, status_code=400)

    ptype = str(payload.get("type", "")).upper()

    # Port updates flow through here as BLOFIN_POSITIONS
    if ptype == "BLOFIN_POSITIONS":
        # record and mark fresh
        STATE["tv_by_symbol"]["__PORT__"] = payload
        STATE["last_blofin_ts"] = now_ms()
        return {"ok": True, "stored": "BLOFIN_POSITIONS", "server_received_ms": now_ms()}

    # Normal TV state (WWASD_State_Emitter)
    if ptype == "WWASD_STATE":
        symbol = str(payload.get("symbol") or payload.get("sym") or "").strip()
        if not symbol:
            return JSONResponse({"ok": False, "error": "no_symbol"}, status_code=400)
        STATE["tv_by_symbol"][symbol] = payload
        return {"ok": True, "stored": symbol, "server_received_ms": now_ms()}

    return JSONResponse({"ok": False, "error": "unknown_type"}, status_code=400)

# ----------------------------
# TV latest/last (reader endpoints the desks rely on)
# /tv/latest?lists=green,full&fresh_only=1&max_age_s=3600
# If a list isn't defined yet, we fall back to "all symbols we've received".
# ----------------------------
def _resolve_symbols(list_name: str) -> List[str]:
    ln = list_name.lower().strip()
    if ln in ("all", "*"):
        return sorted(STATE["tv_by_symbol"].keys())
    s = STATE["lists"].get(ln)
    if isinstance(s, set) and len(s) > 0:
        return sorted(list(s))
    # fallback: return everything we've ever seen so "full" never comes back empty
    return sorted(STATE["tv_by_symbol"].keys())

@app.get("/tv/latest")
async def tv_latest(
    lists: str = "green",
    fresh_only: int = 0,
    max_age_s: int = 3600
):
    names = [x for x in [t.strip() for t in lists.split(",")] if x]
    now = now_ms()
    max_age = max(1, int(max_age_s)) * 1000
    resp_lists: Dict[str, Any] = {}
    for name in names:
        items = []
        for sym in _resolve_symbols(name):
            st = STATE["tv_by_symbol"].get(sym)
            if not isinstance(st, dict):
                continue
            try:
                ts = int(st.get("ts", 0))
            except Exception:
                ts = 0
            is_fresh = bool(ts and (now - ts) <= max_age)
            if fresh_only and not is_fresh:
                continue
            row = dict(st)
            row["is_fresh"] = is_fresh
            items.append(row)
        resp_lists[name] = {"count": len(items), "items": items}
    return {"ts": now, "lists": resp_lists}

# alias kept for older scripts
@app.get("/tv/last")
async def tv_last(lists: str = "green", fresh_only: int = 0, max_age_s: int = 3600):
    return await tv_latest(lists=lists, fresh_only=fresh_only, max_age_s=max_age_s)

# Optional admin: update watchlists (token required)
@app.post("/tv/snap")
async def tv_snap(request: Request, token: str = ""):
    if AUTH and token != AUTH:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_json"}, status_code=400)
    lists = body.get("lists") if isinstance(body, dict) else None
    if not isinstance(lists, dict):
        return JSONResponse({"ok": False, "error": "no_lists"}, status_code=400)
    for name, arr in lists.items():
        key = str(name).lower().strip()
        if key not in STATE["lists"]:
            STATE["lists"][key] = set()
        if isinstance(arr, list):
            STATE["lists"][key] = set(str(x) for x in arr)
    return {"ok": True, "counts": {k: len(v) for k, v in STATE["lists"].items()}}

@app.get("/tv/lists")
async def tv_lists():
    return {
        "lists": {k: sorted(list(v)) for k, v in STATE["lists"].items()},
        "tv_count": len(STATE["tv_by_symbol"]),
    }

# ----------------------------
# Port read endpoints (unchanged contract)
# ----------------------------
BLOFIN = {"fresh": False, "ts": None, "data": None}

@app.get("/blofin/latest")
async def blofin_latest():
    # the pusher posts BLOFIN_POSITIONS via /tv; normalize here for SSR reader
    port_payload = STATE["tv_by_symbol"].get("__PORT__")
    if port_payload:
        return {"fresh": True, "ts": STATE["last_blofin_ts"], "data": port_payload}
    return BLOFIN

# Minimal SSR page (kept stable and read‑only)
PORT2_SSR_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>WWASD Port</title>
<style>
body{background:#0f1115;color:#e6e6e6;font:14px/1.4 system-ui,Segoe UI,Roboto,Arial}
table{border-collapse:collapse;width:100%}th,td{padding:8px;border-bottom:1px solid #333}
small.badge{padding:2px 6px;border-radius:10px;background:#333;margin-left:8px}
</style></head>
<body>
<h3>WWASD Port <small id="fresh" class="badge">unknown</small></h3>
<table><thead><tr><th>Instrument</th><th>Side</th><th>Sz</th><th>Avg</th><th>Mark</th><th>Lev</th></tr></thead>
<tbody id="rows"><tr><td colspan="6">Loading…</td></tr></tbody></table>
<script>
async function load(){
  try {
    const r = await fetch('/blofin/latest'); const j = await r.json();
    document.getElementById('fresh').textContent = j.fresh ? 'fresh' : 'stale';
    const tb = document.getElementById('rows'); tb.innerHTML='';
    const arr = (j && j.data && j.data.data) ? j.data.data : [];
    if(!arr.length){ tb.innerHTML='<tr><td colspan="6">No open positions</td></tr>'; return; }
    for (const p of arr){
      const tr = document.createElement('tr');
      const c=(k)=>{const td=document.createElement('td'); td.textContent=(p[k]??''); tr.appendChild(td)};
      c('instId'); c('positionSide'); c('positions'); c('averagePrice'); c('markPrice'); c('leverage');
      tb.appendChild(tr);
    }
  } catch(e){ console.error(e); }
}
load(); setInterval(load,15000);
</script></body></html>"""

@app.get("/port2_ssr.html")
async def port2_ssr():
    return HTMLResponse(PORT2_SSR_HTML)

