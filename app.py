# app.py — WWASD Relay v2.4 (FastAPI) — hardened Port; Green/Full/Macro unchanged
import os, time, json, hmac, hashlib, base64, datetime, threading
from typing import Dict, Any, List, Optional, Set
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
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

# NEW (hardening knobs)
BLOFIN_TTL_SEC      = int(os.getenv("BLOFIN_TTL_SEC", "240"))  # freshness window for /blofin/latest
BLOFIN_LATEST_PATH  = _strip(os.getenv("BLOFIN_LATEST_PATH", "/tmp/blofin_latest.json"))

# ---------- app ----------
app = FastAPI(title="WWASD Relay v2.4")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------- state ----------
state_by_symbol: Dict[str, Dict[str, Any]] = {}
blofin_positions_push: Optional[Dict[str, Any]] = None

# ---------- hardening: atomic disk backup for Port ----------
_blofin_lock = threading.Lock()

def _blofin_write_atomic(obj: Dict[str, Any]) -> None:
    try:
        d = os.path.dirname(BLOFIN_LATEST_PATH)
        if d: os.makedirs(d, exist_ok=True)
        tmp = BLOFIN_LATEST_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
        os.replace(tmp, BLOFIN_LATEST_PATH)  # atomic on Linux/Windows
    except Exception:
        pass  # in‑mem copy still updated

def _blofin_load_last() -> Optional[Dict[str, Any]]:
    try:
        with open(BLOFIN_LATEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

# preload last-good (if any) so /blofin/latest never starts empty after a restart
blofin_positions_push = _blofin_load_last() or blofin_positions_push

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

def _no_store_json(data: Dict[str, Any]) -> JSONResponse:
    return JSONResponse(data, headers={"Cache-Control": "no-store"})

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
        sym = _upper(str(data.get("symbol","")))
        if not sym: raise HTTPException(status_code=400, detail="Missing symbol")
        state_by_symbol[sym] = data
        return _no_store_json({"ok": True, "stored": sym})

    if typ == "BLOFIN_POSITIONS":
        global blofin_positions_push
        with _blofin_lock:
            blofin_positions_push = data
            _blofin_write_atomic(data)  # atomic disk backup
        return _no_store_json({"ok": True, "stored": "blofin_positions"})

    return _no_store_json({"ok": True, "ignored": True})

@app.get("/tv/latest")
def tv_latest(list: str = "", max_age_secs: int = FRESH_CUTOFF_SECS):
    items: List[Dict[str, Any]] = []
    for sym, item in state_by_symbol.items():
        if list and not _in_named_list(sym, list): continue
        out = dict(item); out["is_fresh"] = _fresh(item, max_age_secs); items.append(out)
    items.sort(key=lambda x: x.get("symbol",""))
    return {"count": len(items), "items": items}

@app.get("/snap")
def snap(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: int = FRESH_CUTOFF_SECS):
    wanted = [ _strip(x).lower() for x in lists.split(",") if _strip(x) ] or ["green"]
    resp: Dict[str, Any] = {"ts": now_ms(), "lists": {}}
    for name in wanted:
        data = tv_latest(list=name, max_age_secs=max_age_secs)
        if fresh_only:
            data["items"] = [it for it in data["items"] if it.get("is_fresh")]
            data["count"] = len(data["items"])
        resp["lists"][name] = data
    return resp

# ------- Hardened Port: live JSON (never 500) + dynamic HTML -------
@app.get("/blofin/latest")
def blofin_latest(max_age_secs: int = BLOFIN_TTL_SEC):
    global blofin_positions_push
    obj = blofin_positions_push
    if not obj:
        obj = _blofin_load_last()
        if obj: blofin_positions_push = obj
    if not obj:
        return _no_store_json({"fresh": False, "ts": None, "age_sec": None, "data": None})
    ts = obj.get("server_received_ms") or obj.get("ts")
    now = now_ms()
    age_sec = None if not ts else round((now - int(ts)) / 1000.0, 2)
    fresh = bool(ts) and (age_sec is not None) and (age_sec <= max_age_secs)
    return _no_store_json({"fresh": fresh, "ts": ts, "age_sec": age_sec, "data": obj})

# Existing SSR view (kept)
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
            side = str(p.get("posSide") or p.get("side") or p.get("positionSide") or "").upper()
            sz   = str(p.get("pos") or p.get("size") or p.get("positions") or "")
            avg  = str(p.get("avgPx") or p.get("avg") or p.get("averagePrice") or "")
            mark = str(p.get("markPx") or p.get("mark") or p.get("markPrice") or "")
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

# NEW: live, bot‑safe HTML that fetches /blofin/latest (auto‑refresh, never blocks)
@app.get("/port2.html", response_class=HTMLResponse)
def port2_html():
    html = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>WWASD Port</title>
<style>
 body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Arial,sans-serif;margin:24px}
 .row{display:flex;gap:8px;align-items:center}
 .pill{padding:4px 10px;border-radius:999px;font-size:12px;color:#fff}
 .fresh{background:#16a34a}.stale{background:#b91c1c}.warn{background:#ca8a04}
 table{width:100%;border-collapse:collapse;margin-top:12px}
 th,td{border-bottom:1px solid #e5e7eb;padding:8px;text-align:left;font-size:14px}
 code{background:#f3f4f6;padding:2px 6px;border-radius:4px}
 .muted{color:#6b7280}
</style></head><body>
<div class="row">
  <div id="statusPill" class="pill stale">STALE</div>
  <div id="age" class="muted"></div>
</div>
<div class="muted" id="stamp"></div>
<div id="upnl" style="margin-top:8px;font-weight:600;"></div>
<table id="t"><thead><tr>
<th>Symbol</th><th>Side</th><th>Qty</th><th>Avg</th><th>Mark</th><th>uPnL</th><th>Lev</th>
</tr></thead><tbody id="tb"></tbody></table>
<div id="err" class="muted"></div>
<script>
const fmt = n => (n==null||isNaN(n))? "" : (+n).toLocaleString(undefined,{maximumFractionDigits:8});
async function load(){
  let pill=document.getElementById('statusPill'), tb=document.getElementById('tb'),
      age=document.getElementById('age'), stamp=document.getElementById('stamp'),
      upnl=document.getElementById('upnl'), err=document.getElementById('err');
  try{
    const r = await fetch('/blofin/latest', {cache:'no-store'});
    const o = await r.json();
    err.textContent="";
    pill.textContent = o.fresh ? "FRESH" : "STALE";
    pill.className = "pill " + (o.fresh ? "fresh" : "stale");
    age.textContent = (o.age_sec==null?"":("age: "+o.age_sec+"s"));
    stamp.textContent = "server ts: " + (o.ts ?? "—");
    const data = (o && o.data && o.data.data && o.data.data.data) || [];
    tb.innerHTML="";
    let sum=0;
    for(const p of data){
      const side = (p.positionSide||p.posSide||p.side||"").toUpperCase();
      const up = parseFloat(p.unrealizedPnl||0);
      sum += (isFinite(up)?up:0);
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${p.instId||p.symbol||""}</td><td>${side}</td><td>${fmt(p.positions||p.pos||p.size)}</td>
                      <td>${fmt(p.averagePrice||p.avg||p.avgPx)}</td><td>${fmt(p.markPrice||p.mark||p.markPx)}</td>
                      <td>${fmt(up)}</td><td>${fmt(p.leverage||p.lever)}</td>`;
      tb.appendChild(tr);
    }
    upnl.textContent = "uPnL (sum): " + fmt(sum);
  }catch(e){
    pill.textContent = "ERROR"; pill.className="pill warn";
    err.textContent = "Fetch failed. Will retry… " + e;
  }
}
load(); setInterval(load, 12000);
</script></body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})

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

