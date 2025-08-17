# app.py  — WWASD Relay v2 (TV + Blofin)
import os
import time
import json
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

def now_ms() -> int:
    return int(time.time() * 1000)

def split_env_list(name: str) -> List[str]:
    raw = os.getenv(name, "")
    return [s.strip().upper() for s in raw.split(",") if s.strip()]

GREEN_LIST = split_env_list("GREEN_LIST")
MACRO_LIST = split_env_list("MACRO_LIST")
FULL_LIST  = split_env_list("FULL_LIST")

FRESH_CUTOFF_SECS = int(os.getenv("FRESH_CUTOFF_SECS", "5400"))  # default 90 min
AUTH_SHARED_SECRET = os.getenv("AUTH_SHARED_SECRET", "").strip()  # optional

app = FastAPI(title="WWASD Relay")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# In‑memory caches
state_by_symbol: Dict[str, Dict[str, Any]] = {}  # latest WWASD_STATE per symbol
blofin_positions: Optional[Dict[str, Any]] = None  # latest BLOFIN_POSITIONS snapshot

def require_secret_if_set(req: Request, body: Dict[str, Any]):
    if not AUTH_SHARED_SECRET:
        return
    qs_token = req.query_params.get("token")
    body_token = body.get("token")
    if (qs_token or body_token) and (qs_token == AUTH_SHARED_SECRET or body_token == AUTH_SHARED_SECRET):
        return
    raise HTTPException(status_code=403, detail="Unauthorized")

@app.get("/health")
def health():
    return {"ok": True, "time": int(time.time()), "count": len(state_by_symbol)}

@app.post("/tv")
async def tv_ingest(request: Request):
    """
    Accepts:
      - TradingView alerts (JSON or form/multipart with a JSON 'message' field)
      - Any JSON with 'type' == 'WWASD_STATE' (per-symbol state)
      - Any JSON with 'type' == 'BLOFIN_POSITIONS' (account snapshot)
    """
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            data = await request.json()
        else:
            # TV can send as form-encoded; pull JSON out of 'message' or raw body.
            try:
                form = await request.form()
                payload = form.get("message") or form.get("payload") or ""
                data = json.loads(payload) if payload else {}
            except Exception:
                raw = await request.body()
                data = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad payload: {e}")

    data["server_received_ms"] = now_ms()

    # Optional shared secret
    require_secret_if_set(request, data)

    typ = str(data.get("type", "")).upper()

    if typ == "WWASD_STATE":
        sym = str(data.get("symbol", "")).upper()
        if not sym:
            raise HTTPException(status_code=400, detail="Missing symbol for WWASD_STATE")
        state_by_symbol[sym] = data
        return {"ok": True, "stored": sym}

    if typ == "BLOFIN_POSITIONS":
        global blofin_positions
        blofin_positions = data
        return {"ok": True, "stored": "blofin_positions"}

    # Unknown types are accepted (no-op)
    return {"ok": True, "ignored": True}

def filter_symbols(list_name: str) -> Optional[set]:
    ln = (list_name or "").lower().strip()
    if ln == "green":
        return set(GREEN_LIST)
    if ln == "macro":
        return set(MACRO_LIST)
    if ln == "full":
        return set(FULL_LIST)
    return None

@app.get("/tv/latest")
def tv_latest(list: str = "", max_age_secs: int = FRESH_CUTOFF_SECS):
    sel = filter_symbols(list)
    now = now_ms()
    items: List[Dict[str, Any]] = []
    for sym, item in state_by_symbol.items():
        if sel is not None and sym not in sel:
            continue
        fresh = (now - item.get("server_received_ms", now)) <= max_age_secs * 1000
        out = dict(item)
        out["is_fresh"] = fresh
        items.append(out)

    items.sort(key=lambda x: x.get("symbol", ""))
    return {"count": len(items), "items": items}

@app.get("/blofin/latest")
def blofin_latest(max_age_secs: int = 900):  # default: 15 minutes freshness window
    if not blofin_positions:
        return {"fresh": False, "ts": None, "data": None}
    now = now_ms()
    fresh = (now - blofin_positions.get("server_received_ms", now)) <= max_age_secs * 1000
    return {"fresh": fresh, "ts": blofin_positions.get("server_received_ms"), "data": blofin_positions}


    return {"count": len(states_list[:max_items]), "items": states_list[:max_items]}
