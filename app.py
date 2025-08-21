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
from fastapi.responses import HTMLResponse, JSONResponse


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

# ------------------------------
# Public mirrors for /snap (HTML + JSON)
# ------------------------------

@app.get("/snap.raw")
def snap_raw(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: int = FRESH_CUTOFF_SECS):
    """JSON mirror of the /snap aggregator for external readers."""
    # Reuse existing /snap function logic directly
    data = snap(lists=lists, fresh_only=bool(fresh_only), max_age_secs=max_age_secs)
    return JSONResponse(content=data)

@app.get("/snap.html", response_class=HTMLResponse)
def snap_html(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: int = FRESH_CUTOFF_SECS):
    """Human-readable mirror of /snap (pretty-printed JSON) for browsers."""
    data = snap(lists=lists, fresh_only=bool(fresh_only), max_age_secs=max_age_secs)
    body = (
        "<!doctype html><meta charset='utf-8'><title>WWASD Snap</title>"
        "<style>body{font-family:system-ui,Segoe UI,Arial;padding:16px}"
        "code{background:#f3f3f3;padding:2px 4px;border-radius:4px}"
        "pre{white-space:pre-wrap;word-break:break-word}</style>"
        "<h1>WWASD Snapshot</h1>"
        f"<p><strong>lists</strong>=<code>{lists}</code> · "
        f"<strong>fresh_only</strong>=<code>{bool(fresh_only)}</code> · "
        f"<strong>max_age_secs</strong>=<code>{max_age_secs}</code></p>"
        "<pre>" + json.dumps(data, indent=2) + "</pre>"
    )
    return HTMLResponse(content=body)
# =========================
# WWASD DESK Mirror Routes
# Paste this block at the END of app.py (after your existing routes)
# =========================

from typing import Optional
import json
from fastapi.responses import HTMLResponse, JSONResponse

# --- helpers to resolve your existing aggregator functions safely ---
def _resolve_snap(lists: str = "green,macro,full", fresh_only: bool = True, max_age_secs: Optional[int] = None):
    """
    Calls your existing snap aggregator in a safe/compatible way.
    Tries get_snap(...) first; if not present, tries snap(...).
    """
    # Try helper-style aggregator
    try:
        return get_snap(lists=lists, fresh_only=fresh_only) if max_age_secs is None else get_snap(lists=lists, fresh_only=fresh_only, max_age_secs=max_age_secs)
    except NameError:
        pass
    except Exception:
        # If get_snap exists but signature differs, try best-effort fallback
        try:
            return get_snap(lists=lists, fresh_only=fresh_only)
        except Exception:
            pass

    # Try route-style function named `snap`
    try:
        return snap(lists=lists, fresh_only=fresh_only) if max_age_secs is None else snap(lists=lists, fresh_only=fresh_only, max_age_secs=max_age_secs)
    except NameError:
        pass
    except Exception:
        # Try minimal signature
        try:
            return snap(lists=lists, fresh_only=fresh_only)
        except Exception as e:
            raise RuntimeError(f"WWASD mirror could not call your snap aggregator: {e}")

    raise RuntimeError("WWASD mirror could not find get_snap(...) or snap(...). Please expose one of those names.")

def _resolve_port_latest():
    """
    Calls your existing blofin portfolio fetcher.
    Tries get_blofin_latest() first, then blofin_latest(), then /blofin/latest route func if exposed.
    """
    # Preferred helper
    try:
        return get_blofin_latest()
    except NameError:
        pass
    except Exception:
        # If exists with different signature, try anyway
        try:
            return get_blofin_latest()
        except Exception:
            pass

    # Alternate helper name
    try:
        return blofin_latest()
    except NameError:
        pass
    except Exception:
        try:
            return blofin_latest()
        except Exception as e:
            raise RuntimeError(f"WWASD mirror could not call your portfolio getter: {e}")

    # If neither helper is available, you can import the function your /blofin/latest route uses and call it here.
    raise RuntimeError("WWASD mirror could not find get_blofin_latest() or blofin_latest(). Please expose one of those names.")

# --- mirror endpoints ---
@app.get("/snap.raw")
def wwasd_snap_raw(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: Optional[int] = None):
    """
    JSON mirror of your existing /snap aggregator.
    Access example:
      /snap.raw?lists=green,macro,full&fresh_only=1
      /snap.raw?lists=green&fresh_only=0&max_age_secs=1800
    """
    data = _resolve_snap(lists=lists, fresh_only=bool(fresh_only), max_age_secs=max_age_secs)
    return JSONResponse(content=data)

@app.get("/snap.html", response_class=HTMLResponse)
def wwasd_snap_html(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: Optional[int] = None):
    """
    Human-readable mirror (pretty-printed JSON) for browsers and external tools.
    """
    data = _resolve_snap(lists=lists, fresh_only=bool(fresh_only), max_age_secs=max_age_secs)
    body = (
        "<!doctype html><meta charset='utf-8'><title>WWASD Snap</title>"
        "<style>body{font-family:system-ui,Segoe UI,Arial;padding:16px}"
        "h1{margin:0 0 8px 0} code{background:#f2f2f2;padding:2px 6px;border-radius:6px}"
        "pre{white-space:pre-wrap;word-break:break-word;background:#0f172a;color:#e5e7eb;padding:12px;border-radius:8px;}</style>"
        "<h1>WWASD Snapshot</h1>"
        f"<p><strong>lists</strong>=<code>{lists}</code> · <strong>fresh_only</strong>=<code>{bool(fresh_only)}</code>"
        + (f" · <strong>max_age_secs</strong>=<code>{max_age_secs}</code>" if max_age_secs is not None else "")
        + "</p>"
        "<pre>" + json.dumps(data, indent=2) + "</pre>"
    )
    return HTMLResponse(content=body)

@app.get("/port.raw")
def wwasd_port_raw():
    """
    JSON mirror of your Blofin latest portfolio snapshot.
    Access example:
      /port.raw
    """
    data = _resolve_port_latest()
    return JSONResponse(content=data)

@app.get("/port.html", response_class=HTMLResponse)
def wwasd_port_html():
    """
    Human-readable HTML view of Blofin positions.
    Access example:
      /port.html
    """
    data = _resolve_port_latest()
    body = (
        "<!doctype html><meta charset='utf-8'><title>WWASD Port</title>"
        "<style>body{font-family:system-ui,Segoe UI,Arial;padding:16px}"
        "h1{margin:0 0 8px 0} code{background:#f2f2f2;padding:2px 6px;border-radius:6px}"
        "pre{white-space:pre-wrap;word-break:break-word;background:#0f172a;color:#e5e7eb;padding:12px;border-radius:8px;}</style>"
        "<h1>Blofin Portfolio Snapshot</h1>"
        "<pre>" + json.dumps(data, indent=2) + "</pre>"
    )
    return HTMLResponse(content=body)
# -------- Pretty Port page (read-only view over /blofin/latest) --------
@app.get("/port2.html")
def port2_html():
    # We return raw HTML; no template engine needed; doesn't touch TV routes.
    from fastapi import Response
    html = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>WWASD Port</title>
  <style>
    :root { --bg:#0b0b0c; --card:#141417; --muted:#9aa0a6; --pos:#18c964; --neg:#ff4d4f; --text:#e6e6e6; }
    html,body{background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,Arial,sans-serif;margin:0}
    .wrap{max-width:1100px;margin:24px auto;padding:0 16px}
    h1{font-size:22px;margin:0 0 12px}
    .meta{color:var(--muted);margin:6px 0 16px}
    table{width:100%;border-collapse:collapse;background:var(--card);border-radius:8px;overflow:hidden}
    th,td{padding:10px 8px;border-bottom:1px solid #232327;text-align:right;white-space:nowrap}
    th{font-weight:600;text-align:left;background:#1b1b20}
    tr:last-child td{border-bottom:none}
    .sym{font-weight:600;text-align:left}
    .pos{color:var(--pos);font-weight:600}
    .neg{color:var(--neg);font-weight:600}
    .pill{display:inline-block;padding:2px 8px;border-radius:999px;background:#1e1e24;color:#d0d0d0;font-size:12px}
    .fresh{color:var(--pos)} .stale{color:var(--neg)}
    .small{font-size:12px;color:var(--muted)}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>WWASD Port <span id="fresh" class="pill">loading…</span></h1>
    <div id="meta" class="meta">Fetching latest…</div>
    <table>
      <thead>
        <tr>
          <th>Symbol</th>
          <th>Type</th>
          <th>Side</th>
          <th>Qty</th>
          <th>Avg</th>
          <th>Mark</th>
          <th>uPnL</th>
          <th>Lev</th>
          <th>Liq</th>
        </tr>
      </thead>
      <tbody id="rows">
        <tr><td colspan="9" class="small">Waiting for data…</td></tr>
      </tbody>
    </table>
  </div>

  <script>
    const $ = (s)=>document.querySelector(s);
    const fmt = (n, d=6) => {
      if (n===null || n===undefined || n==="") return "";
      const x = Number(n); if (!isFinite(x)) return n;
      return x.toLocaleString(undefined, {maximumFractionDigits:d});
    };
    async function load(){
      try{
        const res = await fetch('/blofin/latest?x=' + Date.now(), {cache:'no-store'});
        const j   = await res.json();
        const latestTs = j.ts ? new Date(j.ts).toLocaleString() : '—';
        const fresh = !!j.fresh;
        const raw   = (((j||{}).data||{}).data||{});         // raw BloFin envelope
        const items = Array.isArray(raw.data) ? raw.data : []; // positions array

        // header meta
        $('#fresh').textContent = fresh ? 'fresh' : 'stale';
        $('#fresh').className   = 'pill ' + (fresh ? 'fresh' : 'stale');
        let pnlTotal = 0;

        // rows
        let rows = items.map(p=>{
          const pnl = Number(p.unrealizedPnl || 0) || 0;
          pnlTotal += pnl;
          const cls = pnl >= 0 ? 'pos' : 'neg';
          return `<tr>
            <td class="sym">${p.instId||''}</td>
            <td>${p.instType||''}</td>
            <td>${p.positionSide||''}</td>
            <td>${fmt(p.positions,6)}</td>
            <td>${fmt(p.averagePrice,6)}</td>
            <td>${fmt(p.markPrice,6)}</td>
            <td class="${cls}">${fmt(p.unrealizedPnl,4)}</td>
            <td>${p.leverage||''}</td>
            <td>${fmt(p.liquidationPrice,6)}</td>
          </tr>`;
        }).join('');

        if (!rows) rows = `<tr><td colspan="9" class="small">No open positions.</td></tr>`;
        $('#rows').innerHTML = rows;
        $('#meta').innerHTML = `Updated: ${latestTs} • uPnL total: <b class="${pnlTotal>=0?'pos':'neg'}">${fmt(pnlTotal,4)}</b>`;
      }catch(e){
        $('#rows').innerHTML = `<tr><td colspan="9" class="small">Error: ${String(e).slice(0,200)}</td></tr>`;
        $('#fresh').textContent = 'error'; $('#fresh').className='pill stale';
      }
    }
    load(); setInterval(load, 15000);
  </script>
</body>
</html>
"""
    return Response(content=html, media_type="text/html")
# -------- Pretty Port page (read-only over /blofin/latest) --------
@app.get("/port2.html")
def port2_html():
    # local import so we don't disturb global imports
    from fastapi.responses import HTMLResponse
    html = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>WWASD Port</title>
  <style>
    :root { --bg:#0b0b0c; --card:#141417; --muted:#9aa0a6; --pos:#18c964; --neg:#ff4d4f; --text:#e6e6e6; }
    html,body{background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,Arial,sans-serif;margin:0}
    .wrap{max-width:1100px;margin:24px auto;padding:0 16px}
    h1{font-size:22px;margin:0 0 12px}
    .meta{color:var(--muted);margin:6px 0 16px}
    table{width:100%;border-collapse:collapse;background:var(--card);border-radius:8px;overflow:hidden}
    th,td{padding:10px 8px;border-bottom:1px solid #232327;text-align:right;white-space:nowrap}
    th{font-weight:600;text-align:left;background:#1b1b20}
    tr:last-child td{border-bottom:none}
    .sym{font-weight:600;text-align:left}
    .pos{color:var(--pos);font-weight:600}
    .neg{color:var(--neg);font-weight:600}
    .pill{display:inline-block;padding:2px 8px;border-radius:999px;background:#1e1e24;color:#d0d0d0;font-size:12px}
    .fresh{color:var(--pos)} .stale{color:var(--neg)}
    .small{font-size:12px;color:var(--muted)}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>WWASD Port <span id="fresh" class="pill">loading…</span></h1>
    <div id="meta" class="meta">Fetching latest…</div>
    <table>
      <thead>
        <tr>
          <th>Symbol</th>
          <th>Type</th>
          <th>Side</th>
          <th>Qty</th>
          <th>Avg</th>
          <th>Mark</th>
          <th>uPnL</th>
          <th>Lev</th>
          <th>Liq</th>
        </tr>
      </thead>
      <tbody id="rows">
        <tr><td colspan="9" class="small">Waiting for data…</td></tr>
      </tbody>
    </table>
  </div>

  <script>
    const $ = (s)=>document.querySelector(s);
    const fmt = (n, d=6) => {
      if (n===null || n===undefined || n==="") return "";
      const x = Number(n); if (!isFinite(x)) return n;
      return x.toLocaleString(undefined, {maximumFractionDigits:d});
    };
    async function load(){
      try{
        const res = await fetch('/blofin/latest?x=' + Date.now(), {cache:'no-store'});
        const j   = await res.json();
        const latestTs = j.ts ? new Date(j.ts).toLocaleString() : '—';
        const fresh = !!j.fresh;
        const raw   = (((j||{}).data||{}).data||{});         // raw BloFin envelope
        const items = Array.isArray(raw.data) ? raw.data : []; // positions array

        // header/meta
        $('#fresh').textContent = fresh ? 'fresh' : 'stale';
        $('#fresh').className   = 'pill ' + (fresh ? 'fresh' : 'stale');

        // rows
        let pnlTotal = 0;
        let rows = items.map(p=>{
          const pnl = Number(p.unrealizedPnl || 0) || 0;
          pnlTotal += pnl;
          const cls = pnl >= 0 ? 'pos' : 'neg';
          return `<tr>
            <td class="sym">${p.instId||''}</td>
            <td>${p.instType||''}</td>
            <td>${p.positionSide||''}</td>
            <td>${fmt(p.positions,6)}</td>
            <td>${fmt(p.averagePrice,6)}</td>
            <td>${fmt(p.markPrice,6)}</td>
            <td class="${cls}">${fmt(p.unrealizedPnl,4)}</td>
            <td>${p.leverage||''}</td>
            <td>${fmt(p.liquidationPrice,6)}</td>
          </tr>`;
        }).join('');
        if (!rows) rows = `<tr><td colspan="9" class="small">No open positions.</td></tr>`;
        $('#rows').innerHTML = rows;

        const totalCls = (pnlTotal>=0?'pos':'neg');
        $('#meta').innerHTML = `Updated: ${latestTs} • uPnL total: <b class="${totalCls}">${fmt(pnlTotal,4)}</b>`;
      }catch(e){
        $('#rows').innerHTML = `<tr><td colspan="9" class="small">Error: ${String(e).slice(0,200)}</td></tr>`;
        $('#fresh').textContent = 'error'; $('#fresh').className='pill stale';
      }
    }
    load(); setInterval(load, 15000);
  </script>
</body>
</html>
"""
    return HTMLResponse(html)
# ================== INDICATORS (12EMA D + QVWAP) ==================
import time
from fastapi import Request, HTTPException

AUTH_SHARED_SECRET = os.getenv("AUTH_SHARED_SECRET", "")

# in‑memory indicator state keyed by symbol, e.g. "BTC-USDT"
INDICATORS = {"ts": 0, "data": {}}

def _now_ms() -> int:
    return int(time.time() * 1000)

@app.post("/tv_indicators")
async def tv_indicators(req: Request):
    """TradingView webhook endpoint for indicator values.
       URL must include ?token=AUTH_SHARED_SECRET
       Body: {"type":"INDICATORS","data":[{"sym":"BTC-USDT","ema12d":..., "qvwap":..., "ts":...}, ...]}
    """
    token = req.query_params.get("token", "")
    if not AUTH_SHARED_SECRET or token != AUTH_SHARED_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    try:
        payload = await req.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad json: {e}")

    items = (payload or {}).get("data", [])
    now = _now_ms()
    for it in items:
        sym = (it.get("sym") or it.get("symbol") or "").upper()
        if not sym:
            continue
        INDICATORS["data"][sym] = {
            "ema12d": float(it.get("ema12d")) if it.get("ema12d") is not None else None,
            "qvwap":  float(it.get("qvwap"))  if it.get("qvwap")  is not None else None,
            "ts":     int(it.get("ts") or now),
        }
        INDICATORS["ts"] = now
    return {"ok": True, "stored": len(items)}

@app.get("/indicators/latest")
def indicators_latest():
    ts = INDICATORS.get("ts", 0)
    fresh = bool(ts and (_now_ms() - ts) < 180_000)  # 3 min freshness
    return {"fresh": fresh, "ts": ts, "data": INDICATORS}

# -------- Pretty Port page (with EMA/QVWAP columns) --------
@app.get("/port2.html")
def port2_html():
    from fastapi.responses import HTMLResponse
    html = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>WWASD Port</title>
  <style>
    :root { --bg:#0b0b0c; --card:#141417; --muted:#9aa0a6; --pos:#18c964; --neg:#ff4d4f; --text:#e6e6e6; }
    html,body{background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,Arial,sans-serif;margin:0}
    .wrap{max-width:1200px;margin:24px auto;padding:0 16px}
    h1{font-size:22px;margin:0 0 12px}
    .meta{color:var(--muted);margin:6px 0 16px}
    table{width:100%;border-collapse:collapse;background:var(--card);border-radius:8px;overflow:hidden}
    th,td{padding:10px 8px;border-bottom:1px solid #232327;text-align:right;white-space:nowrap}
    th{font-weight:600;text-align:left;background:#1b1b20}
    tr:last-child td{border-bottom:none}
    .sym{font-weight:600;text-align:left}
    .pos{color:var(--pos);font-weight:600}
    .neg{color:var(--neg);font-weight:600}
    .pill{display:inline-block;padding:2px 8px;border-radius:999px;background:#1e1e24;color:#d0d0d0;font-size:12px}
    .fresh{color:var(--pos)} .stale{color:var(--neg)}
    .small{font-size:12px;color:var(--muted)}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>WWASD Port <span id="fresh" class="pill">loading…</span></h1>
    <div id="meta" class="meta">Fetching latest…</div>
    <table>
      <thead>
        <tr>
          <th>Symbol</th>
          <th>Type</th>
          <th>Side</th>
          <th>Qty</th>
          <th>Avg</th>
          <th>Mark</th>
          <th>uPnL</th>
          <th>Lev</th>
          <th>Liq</th>
          <th>EMA12 (D)</th>
          <th>QVWAP</th>
        </tr>
      </thead>
      <tbody id="rows">
        <tr><td colspan="11" class="small">Waiting for data…</td></tr>
      </tbody>
    </table>
  </div>

  <script>
    const $ = (s)=>document.querySelector(s);
    const fmt = (n, d=6) => {
      if (n===null || n===undefined || n==="") return "";
      const x = Number(n); if (!isFinite(x)) return n;
      return x.toLocaleString(undefined, {maximumFractionDigits:d});
    };

    async function load(){
      try{
        // 1) Positions
        const resP = await fetch('/blofin/latest?x=' + Date.now(), {cache:'no-store'});
        const jp   = await resP.json();
        const latestTs = jp.ts ? new Date(jp.ts).toLocaleString() : '—';
        const fresh = !!jp.fresh;
        const raw   = (((jp||{}).data||{}).data||{});
        const items = Array.isArray(raw.data) ? raw.data : [];

        // 2) Indicators
        const resI = await fetch('/indicators/latest?x=' + Date.now(), {cache:'no-store'});
        const ji   = await resI.json();
        const ind  = (((ji||{}).data||{}).data||{}); // { "BTC-USDT": {ema12d:..., qvwap:...}, ... }

        // header/meta
        $('#fresh').textContent = fresh ? 'fresh' : 'stale';
        $('#fresh').className   = 'pill ' + (fresh ? 'fresh' : 'stale');

        // rows
        let pnlTotal = 0;
        let rows = items.map(p=>{
          const sym = String(p.instId||'').toUpperCase();
          const pnl = Number(p.unrealizedPnl || 0) || 0;
          pnlTotal += pnl;
          const cls = pnl >= 0 ? 'pos' : 'neg';
          const ii  = ind[sym] || {};
          return `<tr>
            <td class="sym">${sym}</td>
            <td>${p.instType||''}</td>
            <td>${p.positionSide||''}</td>
            <td>${fmt(p.positions,6)}</td>
            <td>${fmt(p.averagePrice,6)}</td>
            <td>${fmt(p.markPrice,6)}</td>
            <td class="${cls}">${fmt(p.unrealizedPnl,4)}</td>
            <td>${p.leverage||''}</td>
            <td>${fmt(p.liquidationPrice,6)}</td>
            <td>${fmt(ii.ema12d,6)}</td>
            <td>${fmt(ii.qvwap,6)}</td>
          </tr>`;
        }).join('');

        if (!rows) rows = `<tr><td colspan="11" class="small">No open positions.</td></tr>`;
        $('#rows').innerHTML = rows;

        const totalCls = (pnlTotal>=0?'pos':'neg');
        $('#meta').innerHTML = `Updated: ${latestTs} • uPnL total: <b class="${totalCls}">${fmt(pnlTotal,4)}</b>`;
      }catch(e){
        $('#rows').innerHTML = `<tr><td colspan="11" class="small">Error: ${String(e).slice(0,200)}</td></tr>`;
        $('#fresh').textContent = 'error'; $('#fresh').className='pill stale';
      }
    }
    load(); setInterval(load, 15000);
  </script>
</body>
</html>
"""
    return HTMLResponse(html)
# -------- Capital Lock Protocol mode --------
CAPITAL_MODE = os.getenv("CAPITAL_MODE", "Normal")  # Default is Normal

@app.get("/capital/mode")
def get_capital_mode():
    return {"mode": CAPITAL_MODE}
