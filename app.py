# app.py — WWASD Relay v2.3 (adds /snap_ssr.html + /blofin_ssr.html)
import os, time, json, hmac, hashlib, base64, datetime
from typing import Dict, Any, List, Optional, Set
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from starlette.middleware.cors import CORSMiddleware

# ---------- utils ----------
def now_ms() -> int: return int(time.time() * 1000)
def _strip(s: str) -> str: return (s or "").strip().strip('"\'' ).strip()
def _upper(s: str) -> str: return _strip(s).upper()
def _split_env_list(name: str) -> List[str]:
    raw = os.getenv(name, "")
    raw = raw.replace("\\n", " ").replace("\n", " ")
    if raw.startswith('"') and raw.endswith('"'): raw = raw[1:-1]
    return [ _upper(t) for t in raw.split(",") if _upper(t) ]

def _norm_variants(sym: str) -> Set[str]:
    out: Set[str] = set()
    s = _upper(sym); out.add(s)
    core = s.split(":", 1)[1] if ":" in s else s; out.add(core)
    if "/" in core: out.add(core.replace("/", ""))
    else:
        if core.endswith("USDT.P") and "/" not in core:
            base = core[:-6]; out.add(f"{base}/USDT.P")
    return out

def _make_selector(name: str) -> Set[str]:
    out: Set[str] = set()
    for t in _split_env_list(name): out |= _norm_variants(t)
    return out

# ---------- env ----------
GREEN_LIST = _split_env_list("GREEN_LIST")
MACRO_LIST = _split_env_list("MACRO_LIST")
FULL_LIST  = _split_env_list("FULL_LIST")
SEL_GREEN, SEL_MACRO, SEL_FULL = _make_selector("GREEN_LIST"), _make_selector("MACRO_LIST"), _make_selector("FULL_LIST")
FRESH_CUTOFF_SECS = int(os.getenv("FRESH_CUTOFF_SECS", "5400"))
AUTH_SHARED_SECRET = _strip(os.getenv("AUTH_SHARED_SECRET",""))

BLOFIN_BASE_URL    = _strip(os.getenv("BLOFIN_BASE_URL", "https://openapi.blofin.com").rstrip("/"))
BLOFIN_API_KEY     = _strip(os.getenv("BLOFIN_API_KEY", ""))
BLOFIN_API_SECRET  = _strip(os.getenv("BLOFIN_API_SECRET", ""))
BLOFIN_PASSPHRASE  = _strip(os.getenv("BLOFIN_PASSPHRASE", ""))
BLOFIN_BALANCES_PATH  = _strip(os.getenv("BLOFIN_BALANCES_PATH", "/api/v5/account/balance"))
BLOFIN_POSITIONS_PATH = _strip(os.getenv("BLOFIN_POSITIONS_PATH", "/api/v5/account/positions"))

# ---------- app ----------
app = FastAPI(title="WWASD Relay v2.3")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------- state ----------
state_by_symbol: Dict[str, Dict[str, Any]] = {}
blofin_positions_push: Optional[Dict[str, Any]] = None

# ---------- helpers ----------
def require_secret_if_set(req: Request, body: Dict[str, Any]) -> None:
    if not AUTH_SHARED_SECRET: return
    qs = req.query_params.get("token"); bj = body.get("token")
    if (qs or bj) and (qs == AUTH_SHARED_SECRET or bj == AUTH_SHARED_SECRET): return
    raise HTTPException(status_code=403, detail="Unauthorized")

def _fresh(item: Dict[str, Any], max_age_secs: int) -> bool:
    return (now_ms() - item.get("server_received_ms", now_ms())) <= max_age_secs * 1000

def _list_selector(name: str) -> Optional[Set[str]]:
    ln = (name or "").lower()
    return SEL_GREEN if ln=="green" else SEL_MACRO if ln=="macro" else SEL_FULL if ln=="full" else None

def _in_named_list(sym: str, list_name: str) -> bool:
    sel = _list_selector(list_name)
    return True if sel is None else bool(_norm_variants(sym) & sel)

# ---------- routes ----------
@app.get("/")
def root(): return {"ok": True, "service": "wwasd-relay", "docs": "/docs"}

@app.get("/health")
def health(): return {"ok": True, "time": int(time.time()), "tv_count": len(state_by_symbol), "port_cached": bool(blofin_positions_push)}

@app.post("/tv")
async def tv_ingest(request: Request):
    try:
        ctype = request.headers.get("content-type","")
        if "application/json" in ctype: data = await request.json()
        else:
            try:
                form = await request.form(); payload = form.get("message") or form.get("payload") or ""
                data = json.loads(payload) if payload else {}
            except Exception:
                raw = await request.body(); data = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad payload: {e}")

    data["server_received_ms"] = now_ms()
    require_secret_if_set(request, data)
    typ = _upper(str(data.get("type","")))

    if typ == "WWASD_STATE":
        sym = _upper(str(data.get("symbol",""))); 
        if not sym: raise HTTPException(status_code=400, detail="Missing symbol")
        state_by_symbol[sym] = data; 
        return {"ok": True, "stored": sym}

    if typ == "BLOFIN_POSITIONS":
        global blofin_positions_push; blofin_positions_push = data
        return {"ok": True, "stored": "blofin_positions"}

    return {"ok": True, "ignored": True}

@app.get("/tv/latest")
def tv_latest(list: str = "", max_age_secs: int = FRESH_CUTOFF_SECS):
    items: List[Dict[str, Any]] = []
    for sym, item in state_by_symbol.items():
        if list and not _in_named_list(sym, list): continue
        out = dict(item); out["is_fresh"] = _fresh(item, max_age_secs); items.append(out)
    items.sort(key=lambda x: x.get("symbol","")); 
    return {"count": len(items), "items": items}

@app.get("/snap")
def snap(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: int = FRESH_CUTOFF_SECS):
    wanted = [ _strip(x).lower() for x in lists.split(",") if _strip(x) ] or ["green"]
    resp: Dict[str, Any] = {"ts": now_ms(), "lists": {}}
    for name in wanted:
        data = tv_latest(list=name, max_age_secs=max_age_secs)
        if fresh_only: data["items"] = [it for it in data["items"] if it.get("is_fresh")]; data["count"] = len(data["items"])
        resp["lists"][name] = data
    return resp

@app.get("/blofin/latest")
def blofin_latest(max_age_secs: int = 900):
    if not blofin_positions_push: return {"fresh": False, "ts": None, "data": None}
    return {"fresh": _fresh(blofin_positions_push, max_age_secs),
            "ts": blofin_positions_push.get("server_received_ms"), "data": blofin_positions_push}

# ----- Port SSR -----
def _fmt_ts(ts_ms: Optional[int]) -> str:
    if not ts_ms: return ""
    return datetime.datetime.fromtimestamp(int(ts_ms)/1000.0).strftime("%Y-%m-%d %H:%M:%S")

def _render_port_html(payload: Optional[Dict[str, Any]]) -> str:
    fresh_tag, ts, rows = "", "", ""
    if payload and payload.get("data"):
        ts = _fmt_ts(payload.get("ts") or (payload["data"].get("server_received_ms")))
        fresh_tag = "fresh" if payload.get("fresh") else "stale"
        positions = (payload["data"].get("data") or {}).get("data") or []
        if isinstance(positions, dict): positions = positions.get("positions", [])
        for p in positions:
            inst = str(p.get("instId") or p.get("symbol") or "")
            side = str(p.get("posSide") or p.get("side") or "").upper()
            sz   = str(p.get("pos") or p.get("size") or "")
            avg  = str(p.get("avgPx") or p.get("avg") or "")
            mark = str(p.get("markPx") or p.get("mark") or "")
            lev  = str(p.get("lever") or p.get("leverage") or "")
            rows += f"<tr><td>{inst}</td><td>{side}</td><td>{sz}</td><td>{avg}</td><td>{mark}</td><td>{lev}</td></tr>"
    if not rows: rows = "<tr><td colspan='6'>No open positions</td></tr>"
    return f"""<!doctype html><html><head><meta charset="utf-8"/><title>Port SSR</title>
<style>body{{font-family:system-ui,Segoe UI,Arial,sans-serif;background:#0b0f14;color:#e6edf3;margin:0;padding:20px}}
h1{{font-size:18px;margin:0 0 8px}} small{{color:#9aa7b2}}
table{{width:100%;border-collapse:collapse;margin-top:10px}}
th,td{{border-bottom:1px solid #1f2937;padding:8px 6px;text-align:left;font-size:14px}}
.tag{{display:inline-block;padding:2px 8px;border-radius:6px;background:#1f2937;margin-left:8px}}
.tag.fresh{{background:#064e3b}} .tag.stale{{background:#4a044e}}</style></head>
<body><h1>WWASD Port <span class="tag {fresh_tag}">{fresh_tag or "unknown"}</span></h1>
<small>Last update (server): {ts}</small>
<table><thead><tr><th>Instrument</th><th>Side</th><th>Sz</th><th>Avg</th><th>Mark</th><th>Lev</th></tr></thead>
<tbody>{rows}</tbody></table></body></html>"""

@app.get("/port2_ssr.html", response_class=HTMLResponse)
def port_ssr(): return HTMLResponse(_render_port_html(blofin_latest()))

# ----- NEW: Snap SSR (HTML mirror of /snap) -----
def _render_snap_html(lists: str, fresh_only: int, max_age_secs: int) -> str:
    wanted = [ _strip(x).lower() for x in lists.split(",") if _strip(x) ] or ["green"]
    parts: List[str] = []
    for name in wanted:
        data = snap(lists=name, fresh_only=fresh_only, max_age_secs=max_age_secs)["lists"][name]
        rows = ""
        for it in data["items"]:
            sym = it.get("symbol",""); isf = it.get("is_fresh")
            rows += f"<tr><td>{sym}</td><td>{'fresh' if isf else 'stale'}</td></tr>"
        if not rows: rows = "<tr><td colspan='2'>No items</td></tr>"
        parts.append(f"<h2>{name.upper()} — count {data['count']}</h2>"
                     f"<table><thead><tr><th>Symbol</th><th>Fresh</th></tr></thead><tbody>{rows}</tbody></table>")
    return f"""<!doctype html><html><head><meta charset="utf-8"/><title>Snap SSR</title>
<style>body{{font-family:system-ui,Segoe UI,Arial,sans-serif;background:#0b0f14;color:#e6edf3;margin:0;padding:20px}}
h2{{font-size:16px;margin:14px 0 6px}} table{{width:100%;border-collapse:collapse;margin-top:4px}}
th,td{{border-bottom:1px solid #1f2937;padding:6px 5px;text-align:left;font-size:13px}}</style></head>
<body><small>{datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}Z</small>{''.join(parts)}</body></html>"""

@app.get("/snap_ssr.html", response_class=HTMLResponse)
def snap_ssr(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: int = FRESH_CUTOFF_SECS):
    return HTMLResponse(_render_snap_html(lists, fresh_only, max_age_secs))

# ----- optional BloFin pull-through (unchanged) -----
def _iso_ts() -> str:
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)\
        .isoformat(timespec="milliseconds").replace("+00:00","Z")

def _blofin_headers(method: str, path: str, body: str = "") -> Dict[str, str]:
    if not (BLOFIN_BASE_URL and BLOFIN_API_KEY and BLOFIN_API_SECRET and BLOFIN_PASSPHRASE):
        raise HTTPException(status_code=503, detail="Blofin credentials not configured on server.")
    ts = _iso_ts()
    prehash = f"{ts}{method.upper()}{path}{body}"
    sign = base64.b64encode(hmac.new(BLOFIN_API_SECRET.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()).decode()
    return {"OK-ACCESS-KEY": BLOFIN_API_KEY, "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": BLOFIN_PASSPHRASE,
            "Content-Type": "application/json"}

def _blofin_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not path.startswith("/"): path = "/" + path
    url = f"{BLOFIN_BASE_URL}{path}"; headers = _blofin_headers("GET", path, "")
    r = requests.get(url, headers=headers, params=params, timeout=20)
    try: j = r.json()
    except Exception: j = {"raw": r.text}
    return {"status": r.status_code, "json": j}

@app.get("/blofin/balances")
def blofin_balances():  return _blofin_get(BLOFIN_BALANCES_PATH)
@app.get("/blofin/positions")
def blofin_positions(): return _blofin_get(BLOFIN_POSITIONS_PATH)

