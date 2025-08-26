# app.py — WWASD Relay (hardened SSR + unified webhook)
# FastAPI app that ingests TradingView + Blofin pushes at /tv?token=...
# and serves read‑only pages + JSON for WWASD and the Port.

import os, time, json, html
from typing import Dict, Any, Optional, List
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="wwasd-relay")

# --- CORS (harmless on, OK for embeds) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False,
    allow_methods=["GET", "POST"], allow_headers=["*"]
)

# --- Secrets / config ---
AUTH_SHARED_SECRET = os.getenv("AUTH_SHARED_SECRET", "").strip()
FRESH_WINDOW_MS = int(os.getenv("FRESH_CUTOFF_MS", "300000"))  # 5 minutes default

# --- In‑memory stores (ephemeral by design) ---
_tv_latest: Dict[str, Dict[str, Any]] = {}    # symbol -> last WWASD_STATE
_tv_last_ms: int = 0

_blofin_latest: Optional[Dict[str, Any]] = None  # last BLOFIN_POSITIONS payload (dict)
_blofin_last_ms: int = 0

def now_ms() -> int:
    return int(time.time() * 1000)

def is_fresh(ts_ms: Optional[int], window_ms: int = FRESH_WINDOW_MS) -> bool:
    if not ts_ms:
        return False
    return (now_ms() - int(ts_ms)) <= window_ms

def _require_token(req: Request) -> None:
    # token can be query param (?token=...) or header X-WWASD-Token
    token = req.query_params.get("token") or req.headers.get("X-WWASD-Token")
    if not AUTH_SHARED_SECRET or token != AUTH_SHARED_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

def _extract_positions(any_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Accepts whatever the pusher sent and returns a list of per‑instrument dicts.
    Accepts shapes like: {"data":{"code":"0","data":[{...},{...}]}}  OR already a list.
    """
    if not any_payload:
        return []
    data = any_payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data.get("data")  # Blofin REST 'positions' list
    if isinstance(data, list):
        return data
    if isinstance(any_payload.get("positions"), list):
        return any_payload["positions"]
    # some pushers may wrap as {"payload":{...}}
    inner = any_payload.get("payload")
    if isinstance(inner, dict):
        return _extract_positions(inner)
    return []

# ---------- Ingest ----------
@app.post("/tv")
async def ingest_tv(request: Request):
    """
    Unified webhook for TradingView (WWASD_STATE) AND Blofin positions (BLOFIN_POSITIONS).
    Protected by ?token=... (AUTH_SHARED_SECRET).
    """
    _require_token(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    typ = (body.get("type") or "").upper()
    ts_ms = int(body.get("client_ts") or body.get("ts") or now_ms())

    if typ == "WWASD_STATE":
        # Expect symbol + compact state; we just store by symbol
        symbol = body.get("symbol") or body.get("sym") or "UNKNOWN"
        _tv_latest[symbol] = {
            "ts": ts_ms,
            "symbol": symbol,
            "tf_active": body.get("tf_active"),
            "cmp": body.get("cmp"),
            "mtf": body.get("mtf"),
        }
        global _tv_last_ms
        _tv_last_ms = ts_ms
        return JSONResponse({"ok": True, "stored": "WWASD_STATE", "symbol": symbol})

    if typ == "BLOFIN_POSITIONS":
        # Keep the entire payload (so SSR/JSON can render it flexibly)
        body["server_received_ms"] = now_ms()
        global _blofin_latest, _blofin_last_ms
        _blofin_latest = body
        _blofin_last_ms = ts_ms
        return JSONResponse({"ok": True, "stored": "BLOFIN_POSITIONS", "count": len(_extract_positions(body))})

    # Unknown types are ignored but 200 (so TV doesn't retry forever)
    return JSONResponse({"ok": True, "stored": "UNKNOWN"}, status_code=200)

# ---------- Health ----------
@app.get("/health")
async def health():
    return {
        "ok": True,
        "time": int(time.time()),
        "tv_count": len(_tv_latest),
        "port_cached": _blofin_latest is not None,
    }

# ---------- TV snapshots ----------
@app.get("/tv/last")
async def tv_last():
    # returns the last batch as a dict of symbol -> state
    return {"ok": True, "count": len(_tv_latest), "data": _tv_latest}

# ---------- Blofin JSON (bot‑safe) ----------
@app.get("/blofin/latest")
async def blofin_latest():
    """
    Read‑only JSON for last BLOFIN push. Always returns {"fresh":bool,"ts":int,"data":dict|None}
    Never raises on shape changes.
    """
    if not _blofin_latest:
        return {"fresh": False, "ts": None, "data": None}
    ts = _blofin_last_ms or _blofin_latest.get("client_ts") or _blofin_latest.get("ts")
    return {
        "fresh": is_fresh(int(ts) if ts else None),
        "ts": int(ts) if ts else None,
        "data": _blofin_latest,
    }

# ---------- SSR helpers ----------
def _fmt(v: Any) -> str:
    return html.escape(str(v)) if v is not None else ""

def _render_port_html(latest: Optional[Dict[str, Any]]) -> str:
    """
    Build a minimal SSR table for the Port.
    Hard‑fails never: if shape is unknown, we render a friendly empty state.
    """
    server_ms = now_ms()
    fresh = False
    ts_ms = None
    rows_html = ""

    if latest:
        # latest is {"fresh":bool,"ts":int,"data":{...}}
        fresh = bool(latest.get("fresh"))
        ts_ms = latest.get("ts")
        payload = latest.get("data") or {}
        positions = _extract_positions(payload)

        for p in positions:
            inst = p.get("instId") or p.get("symbol") or "?"
            side = p.get("positionSide") or p.get("side") or "net"
            sz   = p.get("positions") or p.get("size") or p.get("qty") or "-"
            avg  = p.get("averagePrice") or p.get("avgPx") or "-"
            mark = p.get("markPrice") or "-"
            lev  = p.get("leverage") or "-"
            rows_html += f"<tr><td>{_fmt(inst)}</td><td>{_fmt(side)}</td><td>{_fmt(sz)}</td><td>{_fmt(avg)}</td><td>{_fmt(mark)}</td><td>{_fmt(lev)}</td></tr>"

    ts_txt = "-" if not ts_ms else time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(ts_ms)/1000))
    pill   = '<span style="padding:.15rem .45rem;border-radius:.5rem;font-size:.8rem;background:#2a6c2a;color:#dff0d8">fresh</span>' if fresh else '<span style="padding:.15rem .45rem;border-radius:.5rem;font-size:.8rem;background:#5a5a5a;color:#eee">stale</span>'

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

# ---------- SSR routes (aliases) ----------
@app.get("/port.html")
async def port_html():
    latest = await blofin_latest()
    # latest is a dict (not a JSONResponse), safe to pass straight in
    return HTMLResponse(_render_port_html(latest))

@app.get("/port2.html")
async def port2_html():
    latest = await blofin_latest()
    return HTMLResponse(_render_port_html(latest))

@app.get("/port_ssr.html")
async def port_ssr_html():
    latest = await blofin_latest()
    return HTMLResponse(_render_port_html(latest))

@app.get("/port2_ssr.html")
async def port2_ssr_html():
    latest = await blofin_latest()
    return HTMLResponse(_render_port_html(latest))

# ---------- Root ----------
@app.get("/")
async def root():
    return PlainTextResponse(
        "wwasd-relay online\n"
        "POST /tv?token=***  (WWASD_STATE or BLOFIN_POSITIONS)\n"
        "GET  /blofin/latest  (JSON)\n"
        "GET  /port2.html     (SSR table)\n"
        "GET  /health         (ok,tv_count,port_cached)\n"
    )


