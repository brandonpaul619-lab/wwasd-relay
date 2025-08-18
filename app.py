# app.py — WWASD Relay v2.2 (TV + Blofin + Snap)
import os
import re
import time
import json
import hmac
import hashlib
import base64
import datetime
from typing import Dict, Any, List, Optional, Iterable

import requests
from fastapi import FastAPI, Request, HTTPException
from starlette.middleware.cors import CORSMiddleware


# ------------------------------
# Helpers & normalization
# ------------------------------

def now_ms() -> int:
    return int(time.time() * 1000)


_MACRO_BARE_HINTS = ("TOTAL",)  # e.g., TOTAL, TOTAL2, TOTAL3, TOTALA
_MACRO_SUFFIX_HINTS = (".D", ".C")  # e.g., USDT.D, BTC.D, OTHERS.D, MEME.C


def looks_like_macro_bare(sym_no_ns: str) -> bool:
    """Heuristic: bare macro tickers (without CRYPTOCAP:)."""
    u = sym_no_ns.upper()
    return (
        u.startswith(_MACRO_BARE_HINTS)
        or any(u.endswith(sfx) for sfx in _MACRO_SUFFIX_HINTS)
    )


def canonical_symbol(sym: str) -> str:
    """
    Normalize common forms used by TradingView alerts into a single canonical key.

    Examples:
      LINK/USDT.P       -> BLOFIN:LINKUSDT.P
      LINKUSDT.P        -> BLOFIN:LINKUSDT.P
      BLOFIN:LINKUSDT.P -> BLOFIN:LINKUSDT.P
      TOTAL3            -> CRYPTOCAP:TOTAL3
      USDT.D            -> CRYPTOCAP:USDT.D
      CRYPTOCAP:TOTAL3  -> CRYPTOCAP:TOTAL3
    """
    s = (sym or "").strip().upper().replace(" ", "")
    if not s:
        return s

    # Already namespaced
    if ":" in s:
        ns, rest = s.split(":", 1)
        if ns == "BLOFIN":
            # Sometimes TV can emit BLOFIN:LINK/USDT.P
            rest = rest.replace("/", "")
            return f"{ns}:{rest}"
        return f"{ns}:{rest}"

    # Macro (bare)
    if looks_like_macro_bare(s):
        return f"CRYPTOCAP:{s}"

    # Blofin USDT perps (no namespace)
    if "/USDT.P" in s:
        return f"BLOFIN:{s.replace('/', '')}"
    if s.endswith("USDT.P"):
        return f"BLOFIN:{s}"

    # Fallback: return as-is (uppercased)
    return s


def _split_env_list(name: str) -> List[str]:
    raw = os.getenv(name, "")
    out: List[str] = []
    for piece in raw.split(","):
        tok = piece.strip()
        if not tok:
            continue
        out.append(canonical_symbol(tok))
    return out


# ------------------------------
# Env & config
# ------------------------------

# Watchlists (any mix of forms; we normalize above)
GREEN_LIST = _split_env_list("GREEN_LIST")
MACRO_LIST = _split_env_list("MACRO_LIST")
FULL_LIST  = _split_env_list("FULL_LIST")

# Freshness window for /tv/latest
FRESH_CUTOFF_SECS = int(os.getenv("FRESH_CUTOFF_SECS", "5400"))  # default 90m

# Optional shared secret for /tv ingest
AUTH_SHARED_SECRET = os.getenv("AUTH_SHARED_SECRET", "").strip()

# Blofin creds (optional; needed only for /blofin/* pull-through routes)
BLOFIN_BASE_URL  = os.getenv("BLOFIN_BASE_URL", "https://openapi.blofin.com").rstrip("/")
BLOFIN_API_KEY   = os.getenv("BLOFIN_API_KEY", "")
BLOFIN_API_SECRET = os.getenv("BLOFIN_API_SECRET", "")
BLOFIN_PASSPHRASE = os.getenv("BLOFIN_PASSPHRASE", "")

# Blofin paths
BLOFIN_BALANCES_PATH  = os.getenv("BLOFIN_BALANCES_PATH", "/api/v5/account/balance")
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

# Latest per-symbol TradingView WWASD_STATE (keyed by canonical symbol)
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
    return {
        "ok": True,
        "service": "wwasd-relay",
        "version": "2.2",
        "docs": "/docs",
    }


@app.get("/health")
def health():
    return {"ok": True, "time": int(time.time()), "count": len(state_by_symbol)}


@app.get("/tv/symbols")
def tv_symbols():
    """Quick debug: see normalized watchlists as the server sees them."""
    return {
        "green": sorted(set(GREEN_LIST)),
        "macro": sorted(set(MACRO_LIST)),
        "full":  sorted(set(FULL_LIST)),
        "stored_keys": sorted(state_by_symbol.keys()),
    }


# ------------------------------
# TradingView ingest
# ------------------------------

def _coerce_json_from_tv_request(raw_body: bytes, content_type: str, form_obj: Optional[dict]) -> Dict[str, Any]:
    """
    TV sometimes posts as:
      - application/json (already JSON)
      - form/multipart with a 'message'/'payload' string containing JSON
      - raw text/bytes that are JSON
    """
    data: Dict[str, Any] = {}
    if "application/json" in (content_type or ""):
        try:
            data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Bad JSON: {e}")
    else:
        # try form('message'/'payload')
        if form_obj:
            payload = form_obj.get("message") or form_obj.get("payload") or ""
            if payload:
                try:
                    data = json.loads(payload)
                    return data
                except Exception as e:
                    raise HTTPException(status_code=400, detail=f"Bad form JSON: {e}")
        # try raw
        if raw_body:
            try:
                data = json.loads(raw_body.decode("utf-8"))
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Bad payload: {e}")
    return data


@app.post("/tv")
async def tv_ingest(request: Request):
    """
    Accepts:
      - TradingView alerts (JSON or form/multipart with 'message' that contains JSON)
      - Any JSON with 'type' == 'WWASD_STATE' (per-symbol state)
      - Any JSON with 'type' == 'BLOFIN_POSITIONS' (account snapshot you push in)
    """
    # Read body safely (support both JSON & form)
    content_type = request.headers.get("content-type", "")
    form_obj = None
    raw = await request.body()
    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        try:
            form_obj = await request.form()
        except Exception:
            form_obj = None

    data = _coerce_json_from_tv_request(raw, content_type, form_obj) if (raw or form_obj) else {}
    data["server_received_ms"] = now_ms()

    # Optional secret check
    require_secret_if_set(request, data)

    typ = str(data.get("type", "")).upper()

    if typ == "WWASD_STATE":
        raw_sym = str(data.get("symbol", "")).strip()
        if not raw_sym:
            raise HTTPException(status_code=400, detail="Missing symbol for WWASD_STATE")
        sym = canonical_symbol(raw_sym)
        # Store canonical; keep raw for transparency if different
        if raw_sym.upper() != sym:
            data["raw_symbol"] = raw_sym.upper()
        data["symbol"] = sym
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

    # Because we store by canonical keys, also canonicalize the filter set
    if sel is not None:
        sel = set(canonical_symbol(s) for s in sel)

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


# ------------------------------
# Snapshot aggregator (green/macro/full + port)
# ------------------------------

@app.get("/snap")
def snap(
    lists: str = "green,macro",      # comma list: green,macro,full
    fresh_only: bool = True,         # drop stale rows by default
    max_age_secs: int = FRESH_CUTOFF_SECS,
):
    out: Dict[str, Any] = {"ts": now_ms()}
    for name in [s.strip().lower() for s in lists.split(",") if s.strip()]:
        bucket = tv_latest(list=name, max_age_secs=max_age_secs)
        if fresh_only:
            bucket["items"] = [it for it in bucket["items"] if it.get("is_fresh")]
            bucket["count"] = len(bucket["items"])
        out[name] = bucket
    out["port"] = blofin_latest(max_age_secs=900)
    return out
