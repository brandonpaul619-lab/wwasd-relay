# app.py  — WWASD Relay v2 (TV + Blofin)
import os
import time
import json
import hmac
import hashlib
import base64
import datetime
from typing import Dict, Any, List, Optional

import requests
from fastapi import FastAPI, Request, HTTPException
from starlette.middleware.cors import CORSMiddleware


# ------------------------------
# Helpers & env
# ------------------------------

def now_ms() -> int:
    return int(time.time() * 1000)


def split_env_list(name: str) -> List[str]:
    raw = os.getenv(name, "")
    # Normalize to UPPER w/out whitespace
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


# Watchlists
GREEN_LIST = split_env_list("GREEN_LIST")
MACRO_LIST = split_env_list("MACRO_LIST")
FULL_LIST  = split_env_list("FULL_LIST")

# Freshness window for /tv/latest
FRESH_CUTOFF_SECS = int(os.getenv("FRESH_CUTOFF_SECS", "5400"))  # default 90m

# Optional shared secret for /tv ingest
AUTH_SHARED_SECRET = os.getenv("AUTH_SHARED_SECRET", "").strip()

# Blofin creds (optional; needed only for /blofin/* pull-through routes)
BLOFIN_BASE_URL = os.getenv("BLOFIN_BASE_URL", "https://openapi.blofin.com").rstrip("/")
BLOFIN_API_KEY = os.getenv("BLOFIN_API_KEY", "")
BLOFIN_API_SECRET = os.getenv("BLOFIN_API_SECRET", "")
BLOFIN_PASSPHRASE = os.getenv("BLOFIN_PASSPHRASE", "")

# Blofin paths (keep default unless Blofin changes them)
BLOFIN_BALANCES_PATH = os.getenv("BLOFIN_BALANCES_PATH", "/api/v5/account/balance")
BLOFIN_POSITIONS_PATH = os.getenv("BLOFIN_POSITIONS_PATH", "/api/v5/account/positions")


# ------------------------------
# FastAPI app
# ------------------------------

app = FastAPI(title="WWASD Relay")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------
# In-memory caches
# ------------------------------

# Latest per-symbol TradingView WWASD_STATE
state_by_symbol: Dict[str, Dict[str, Any]] = {}

# Latest push-based Blofin snapshot (type == BLOFIN_POSITIONS) if you post it to /tv
blofin_positions_push: Optional[Dict[str, Any]] = None


# ------------------------------
# Security for /tv
# ------------------------------

def require_secret_if_set(req: Request, body: Dict[str, Any]) -> None:
    """If AUTH_SHARED_SECRET is set, require it via query ?token=... or JSON 'token'."""
    if not AUTH_SHARED_SECRET:
        return
    qs_token = req.query_params.get("token")
    body_token = body.get("token")
    if (qs_token or body_token) and (qs_token == AUTH_SHARED_SECRET or body_token == AUTH_SHARED_SECRET):
        return
    raise HTTPException(status_code=403, detail="Unauthorized")


# ------------------------------
# Root & health
# ------------------------------

@app.get("/")
def root():
    return {"ok": True, "service": "wwasd-relay", "docs": "/docs"}


@app.get("/health")
def health():
    return {"ok": True, "time": int(time.time()), "count": len(state_by_symbol)}


# ------------------------------
# TradingView ingest
# ------------------------------

@app.post("/tv")
async def tv_ingest(request: Request):
    """
    Accepts:
      - TradingView alerts (JSON or form/multipart with 'message' that contains JSON)
      - Any JSON with 'type' == 'WWASD_STATE' (per-symbol state)
      - Any JSON with 'type' == 'BLOFIN_POSITIONS' (account snapshot you push in)
    """
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            data = await request.json()
        else:
            # TV often posts form-encoded; JSON lives in 'message' (or 'payload')
            try:
                form = await request.form()
                payload = form.get("message") or form.get("payload") or ""
                data = json.loads(payload) if payload else {}
            except Exception:
                raw = await request.body()
                data = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad payload: {e}")

    # Stamp server receipt time
    data["server_received_ms"] = now_ms()

    # Optional secret check
    require_secret_if_set(request, data)

    typ = str(data.get("type", "")).upper()

    if typ == "WWASD_STATE":
        sym = str(data.get("symbol", "")).upper()
        if not sym:
            raise HTTPException(status_code=400, detail="Missing symbol for WWASD_STATE")
        state_by_symbol[sym] = data
        return {"ok": True, "stored": sym}

    if typ == "BLOFIN_POSITIONS":
        global blofin_positions_push
        blofin_positions_push = data
        return {"ok": True, "stored": "blofin_positions"}

    # Unknown -> accept no-op so chats don’t break
    return {"ok": True, "ignored": True}


def _filter_symbols(list_name: str) -> Optional[set]:
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
    """Return latest per-symbol WWASD_STATE; optionally filter by a named list."""
    sel = _filter_symbols(list)
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


# ------------------------------
# Push-based Blofin snapshot (if you post type=BLOFIN_POSITIONS into /tv)
# ------------------------------

@app.get("/blofin/latest")
def blofin_latest(max_age_secs: int = 900):  # default 15m freshness window
    if not blofin_positions_push:
        return {"fresh": False, "ts": None, "data": None}
    now = now_ms()
    fresh = (now - blofin_positions_push.get("server_received_ms", now)) <= max_age_secs * 1000
    return {"fresh": fresh, "ts": blofin_positions_push.get("server_received_ms"), "data": blofin_positions_push}


# ------------------------------
# Blofin pull-through (read-only) — requires creds in env
# ------------------------------

def _iso_ts() -> str:
    # 2025-08-16T23:59:59.123Z
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc) \
        .isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _blofin_headers(method: str, path: str, body: str = "") -> Dict[str, str]:
    """OKX-style signing used by Blofin v5 endpoints."""
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
    """Direct GET to Blofin balances (read-only)."""
    return _blofin_get(BLOFIN_BALANCES_PATH)


@app.get("/blofin/positions")
def blofin_positions():
    """Direct GET to Blofin positions (read-only)."""
    return _blofin_get(BLOFIN_POSITIONS_PATH)
