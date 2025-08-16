import os, time, json, re
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

try:
    import redis  # optional
except Exception:
    redis = None

# -------------------- Env & Config --------------------
SECRET_TOKEN = os.getenv("SECRET_TOKEN")  # optional
REDIS_URL = os.getenv("REDIS_URL")       # optional
GREEN_LIST = os.getenv("GREEN_LIST", "")
MACRO_LIST = os.getenv("MACRO_LIST", "")
FULL_LIST  = os.getenv("FULL_LIST", "")
FRESH_CUTOFF_SECS = int(os.getenv("FRESH_CUTOFF_SECS", "5400"))  # default 90m

# -------------------- Storage --------------------
r = None
if REDIS_URL and redis is not None:
    try:
        r = redis.from_url(REDIS_URL, decode_responses=True)
    except Exception:
        r = None

memory_store: Dict[str, Dict[str, Any]] = {}  # last WWASD_STATE per symbol
recent_events: List[Dict[str, Any]] = []      # other events / charts (optional)

# -------------------- App --------------------
app = FastAPI(title="WWASD Relay v2", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Regex helpers
macro_re   = re.compile(r"^[A-Z0-9:\._\-]+$")
perp_noslash_re = re.compile(r"^[A-Z0-9]+USDT\.P$")
perp_slash_re   = re.compile(r"^[A-Z0-9]+/USDT\.P$")

def now_ms() -> int:
    return int(time.time() * 1000)

def normalize_symbol(sym: Optional[str]) -> Optional[str]:
    if not sym:
        return sym
    s = str(sym).strip().upper()
    # strip vendor prefix (e.g., BLOFIN:)
    if ":" in s:
        s = s.split(":", 1)[1]
    # add slash for perps if missing
    if perp_noslash_re.match(s) and "/" not in s:
        s = s.replace("USDT.P", "/USDT.P")
    return s

def pick_list(name: Optional[str]) -> List[str]:
    if not name:
        return []
    name = name.lower().strip()
    mapping = {
        "green": [s.strip() for s in GREEN_LIST.split(",") if s.strip()],
        "macro": [s.strip() for s in MACRO_LIST.split(",") if s.strip()],
        "full":  [s.strip() for s in FULL_LIST.split(",") if s.strip()],
    }
    return mapping.get(name, [])

def save_state(state: Dict[str, Any]) -> None:
    sym = state.get("symbol")
    if not sym:
        return
    key = f"state:{sym}"
    data = json.dumps(state, separators=(",", ":"))
    if r:
        try:
            r.set(key, data)
            r.sadd("symbols", sym)
        except Exception:
            pass
    memory_store[sym] = state

def load_states() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if r:
        try:
            syms = r.smembers("symbols")
            for s in syms:
                raw = r.get(f"state:{s}")
                if raw:
                    out[s] = json.loads(raw)
        except Exception:
            pass
    # overlay in-memory
    out.update(memory_store)
    return out

@app.get("/health")
async def health():
    return {"ok": True, "time": int(time.time()), "count": len(memory_store)}

@app.post("/tv")
async def tv_ingest(request: Request, token: Optional[str] = Query(default=None)):
    """
    Accepts:
    - JSON body with Pine alert payload nested under "message" (TradingView Any alert() function call)
    - Raw JSON body already shaped like WWASD_STATE
    - multipart/form-data from automation (e.g., screenshots) — stored as recent events only
    """
    # Optional token check
    if SECRET_TOKEN:
        supplied = token
        if not supplied:
            try:
                tmp = await request.json()
                supplied = tmp.get("token")
            except Exception:
                supplied = None
        if supplied != SECRET_TOKEN:
            raise HTTPException(status_code=401, detail="bad token")

    ctype = request.headers.get("content-type", "")
    if "multipart/form-data" in ctype:
        # Accept screenshots / breadcrumbs without strict schema
        form = await request.form()
        fields = {k: str(v) for k, v in form.items()}
        symbol = normalize_symbol(fields.get("symbol") or fields.get("ticker"))
        payload = {
            **fields,
            "symbol": symbol,
            "type": fields.get("type") or "chart",
            "server_received_ms": now_ms(),
            "is_chart": True,
        }
        recent_events.append(payload)
        if len(recent_events) > 2000:
            recent_events.pop(0)
        return {"ok": True, "ingested": "chart"}

    # Otherwise parse JSON
    try:
        body = await request.json()
    except Exception:
        raw = await request.body()
        try:
            body = json.loads(raw.decode("utf-8", "ignore"))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

    # If message is a JSON string, merge it
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        try:
            inner = json.loads(body["message"])
            for k, v in inner.items():
                body.setdefault(k, v)
        except Exception:
            pass

    # Normalize symbol
    symbol = normalize_symbol(body.get("symbol") or body.get("ticker"))
    if symbol:
        body["symbol"] = symbol

    # Save WWASD_STATE, store others as recent events
    if body.get("type") == "WWASD_STATE" and symbol:
        body["server_received_ms"] = now_ms()
        save_state(body)
    else:
        body["server_received_ms"] = now_ms()
        recent_events.append(body)
        if len(recent_events) > 2000:
            recent_events.pop(0)

    return {"ok": True}

@app.get("/tv/latest")
async def latest(
    list: Optional[str] = None,
    symbols: Optional[str] = None,
    max_age_secs: Optional[int] = None,
    max_items: int = 500,
):
    states = list(load_states().values())

    # Filter by list or explicit symbols
    subset: set = set()
    if symbols:
        subset.update({normalize_symbol(s) for s in symbols.split(",") if s.strip()})
    wl = pick_list(list)
    if wl:
        subset.update({normalize_symbol(s) for s in wl})
    if subset:
        states = [s for s in states if normalize_symbol(s.get("symbol")) in subset]

    # Age filter
    if max_age_secs is not None:
        cutoff = now_ms() - int(max_age_secs) * 1000
        states = [s for s in states if int(s.get("server_received_ms", 0)) >= cutoff]

    # Sort newest first
    states.sort(key=lambda s: s.get("server_received_ms", 0), reverse=True)

    # Freshness flag
    fresh_cutoff_ms = now_ms() - FRESH_CUTOFF_SECS * 1000
    for s in states:
        s["is_fresh"] = int(s.get("server_received_ms", 0)) >= fresh_cutoff_ms

    return {"count": len(states[:max_items]), "items": states[:max_items]}
