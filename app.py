# app.py — WWASD Relay v2.1 (robust lists + tolerant matching + /snap)
import os, time, json, hmac, hashlib, base64, datetime
from typing import Dict, Any, List, Optional, Set

import requests
from fastapi import FastAPI, Request, HTTPException
from starlette.middleware.cors import CORSMiddleware

# ---------------- utils ----------------
def now_ms() -> int: 
    return int(time.time() * 1000)

def _strip(s: str) -> str:
    return (s or "").strip().strip('"\'' ).strip()

def _upper(s: str) -> str:
    return _strip(s).upper()

def _split_env_list(name: str) -> List[str]:
    raw = os.getenv(name, "")
    # tolerate full-string quotes and escaped newlines
    raw = raw.replace("\\n", " ").replace("\n", " ")
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    toks = [ _upper(t) for t in raw.split(",") if _upper(t) ]
    return toks

def _norm_variants(sym: str) -> Set[str]:
    """
    Matching variants:
      - uppercase & trimmed
      - strip vendor prefix before colon (BLOFIN:, CRYPTOCAP:, etc.)
      - slash/no-slash variants for pairs like LINK/USDT.P ↔ LINKUSDT.P
    """
    out: Set[str] = set()
    s = _upper(sym)
    out.add(s)

    # strip prefix before colon
    core = s.split(":", 1)[1] if ":" in s else s
    out.add(core)

    # slash <-> no-slash
    if "/" in core:
        out.add(core.replace("/", ""))
    else:
        # reinsert slash before USDT.P if present
        if core.endswith("USDT.P") and "/" not in core:
            base = core[:-6]  # drop 'USDT.P'
            out.add(f"{base}/USDT.P")

    return out

def _make_selector(name: str) -> Set[str]:
    toks = _split_env_list(name)
    out: Set[str] = set()
    for t in toks:
        out |= _norm_variants(t)
    return out

# ---------------- env ----------------
GREEN_LIST = _split_env_list("GREEN_LIST")
MACRO_LIST = _split_env_list("MACRO_LIST")
FULL_LIST  = _split_env_list("FULL_LIST")

SEL_GREEN = _make_selector("GREEN_LIST")
SEL_MACRO = _make_selector("MACRO_LIST")
SEL_FULL  = _make_selector("FULL_LIST")

FRESH_CUTOFF_SECS = int(os.getenv("FRESH_CUTOFF_SECS", "5400"))  # 90m default
AUTH_SHARED_SECRET = _strip(os.getenv("AUTH_SHARED_SECRET", ""))

# Optional Blofin pull-through (read-only; push path is via /tv type=BLOFIN_POSITIONS)
BLOFIN_BASE_URL    = _strip(os.getenv("BLOFIN_BASE_URL", "https://openapi.blofin.com").rstrip("/"))
BLOFIN_API_KEY     = _strip(os.getenv("BLOFIN_API_KEY", ""))
BLOFIN_API_SECRET  = _strip(os.getenv("BLOFIN_API_SECRET", ""))
BLOFIN_PASSPHRASE  = _strip(os.getenv("BLOFIN_PASSPHRASE", ""))
BLOFIN_BALANCES_PATH  = _strip(os.getenv("BLOFIN_BALANCES_PATH", "/api/v5/account/balance"))
BLOFIN_POSITIONS_PATH = _strip(os.getenv("BLOFIN_POSITIONS_PATH", "/api/v5/account/positions"))

# ---------------- app ----------------
app = FastAPI(title="WWASD Relay")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------------- state ----------------
state_by_symbol: Dict[str, Dict[str, Any]] = {}
blofin_positions_push: Optional[Dict[str, Any]] = None

# ---------------- helpers ----------------
def require_secret_if_set(req: Request, body: Dict[str, Any]) -> None:
    if not AUTH_SHARED_SECRET:
        return
    qs = req.query_params.get("token")
    bj = body.get("token")
    if (qs or bj) and (qs == AUTH_SHARED_SECRET or bj == AUTH_SHARED_SECRET):
        return
    raise HTTPException(status_code=403, detail="Unauthorized")

def _fresh(item: Dict[str, Any], max_age_secs: int) -> bool:
    now = now_ms()
    return (now - item.get("server_received_ms", now)) <= max_age_secs * 1000

def _list_selector(name: str) -> Optional[Set[str]]:
    ln = (name or "").lower()
    if ln == "green": return SEL_GREEN
    if ln == "macro": return SEL_MACRO
    if ln == "full":  return SEL_FULL
    return None

def _in_named_list(sym: str, list_name: str) -> bool:
    sel = _list_selector(list_name)
    if sel is None:
        return True
    return bool(_norm_variants(sym) & sel)

# ---------------- routes ----------------
@app.get("/")
def root(): 
    return {"ok": True, "service": "wwasd-relay", "docs": "/docs"}

@app.get("/health")
def health():
    return {"ok": True, "time": int(time.time()), "count": len(state_by_symbol)}

@app.post("/tv")
async def tv_ingest(request: Request):
    """
    Accepts:
      - TradingView alerts (JSON or form/multipart with 'message' JSON)
      - Any JSON with 'type' == 'WWASD_STATE' (per-symbol state)
      - Any JSON with 'type' == 'BLOFIN_POSITIONS' (account snapshot you push in)
    """
    try:
        ctype = request.headers.get("content-type", "")
        if "application/json" in ctype:
            data = await request.json()
        else:
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
    require_secret_if_set(request, data)

    typ = _upper(str(data.get("type", "")))

    if typ == "WWASD_STATE":
        sym = _upper(str(data.get("symbol", "")))
        if not sym: 
            raise HTTPException(status_code=400, detail="Missing symbol")
        state_by_symbol[sym] = data
        return {"ok": True, "stored": sym}

    if typ == "BLOFIN_POSITIONS":
        global blofin_positions_push
        blofin_positions_push = data
        return {"ok": True, "stored": "blofin_positions"}

    return {"ok": True, "ignored": True}

@app.get("/tv/latest")
def tv_latest(list: str = "", max_age_secs: int = FRESH_CUTOFF_SECS):
    items: List[Dict[str, Any]] = []
    for sym, item in state_by_symbol.items():
        if list and not _in_named_list(sym, list):
            continue
        out = dict(item)
        out["is_fresh"] = _fresh(item, max_age_secs)
        items.append(out)
    items.sort(key=lambda x: x.get("symbol", ""))
    return {"count": len(items), "items": items}

@app.get("/snap")
def snap(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: int = FRESH_CUTOFF_SECS):
    wanted = [ _strip(x).lower() for x in lists.split(",") if _strip(x) ]
    if not wanted: 
        wanted = ["green"]
    resp: Dict[str, Any] = {"ts": now_ms(), "lists": {}}
    for name in wanted:
        data = tv_latest(list=name, max_age_secs=max_age_secs)
        if fresh_only:
            data["items"] = [it for it in data["items"] if it.get("is_fresh")]
            data["count"] = len(data["items"])
        resp["lists"][name] = data
    return resp

# ---- push-based Blofin snapshot (via /tv type=BLOFIN_POSITIONS) ----
@app.get("/blofin/latest")
def blofin_latest(max_age_secs: int = 900):
    if not blofin_positions_push:
        return {"fresh": False, "ts": None, "data": None}
    now = now_ms()
    fresh = _fresh(blofin_positions_push, max_age_secs)
    return {
        "fresh": fresh,
        "ts": blofin_positions_push.get("server_received_ms"),
        "data": blofin_positions_push,
    }

# ---- optional Blofin pull-through (read-only) ----
def _iso_ts() -> str:
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)\
        .isoformat(timespec="milliseconds").replace("+00:00","Z")

def _blofin_headers(method: str, path: str, body: str = "") -> Dict[str, str]:
    if not (BLOFIN_BASE_URL and BLOFIN_API_KEY and BLOFIN_API_SECRET and BLOFIN_PASSPHRASE):
        raise HTTPException(status_code=503, detail="Blofin credentials not configured on server.")
    ts = _iso_ts()
    prehash = f"{ts}{method.upper()}{path}{body}"
    sign = base64.b64encode(
        hmac.new(BLOFIN_API_SECRET.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    ).decode()
    return {
        "OK-ACCESS-KEY": BLOFIN_API_KEY,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": BLOFIN_PASSPHRASE,
        "Content-Type": "application/json",
    }

def _blofin_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not path.startswith("/"): 
        path = "/" + path
    url = f"{BLOFIN_BASE_URL}{path}"
    headers = _blofin_headers("GET", path, "")
    r = requests.get(url, headers=headers, params=params, timeout=20)
    try: 
        j = r.json()
    except Exception: 
        j = {"raw": r.text}
    return {"status": r.status_code, "json": j}

@app.get("/blofin/balances")
def blofin_balances():  
    return _blofin_get(BLOFIN_BALANCES_PATH)

@app.get("/blofin/positions")
def blofin_positions(): 
    return _blofin_get(BLOFIN_POSITIONS_PATH)

