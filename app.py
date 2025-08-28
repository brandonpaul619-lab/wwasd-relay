# app.py — WWASD Relay v2.9 (adds /snap.json)
# - TV: /tv ingest (WWASD_STATE) → /snap, /tv/latest (+ SSR mirror)
# - Port: /tv ingest (BLOFIN_POSITIONS) with disk backup → /blofin/latest → /port2_ssr.html /port2.html
# - Adds /snap.json endpoint to serve snap data as plain JSON
# - Single worker; no static HTML files named port*.html in repo.

import os, time, json, html, datetime, threading
from typing import Dict, Any, Optional, List, Set
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

# ---------- Utils ----------
def now_ms() -> int: return int(time.time() * 1000)
def _strip(s: str) -> str:
    """Normalize a string by stripping whitespace and surrounding quotes."""
    # Start with a safe default string and remove outer whitespace
    s = (s or "").strip()
    # Remove a single leading and trailing double quote
    if s.startswith("\"") and s.endswith("\""):
        s = s[1:-1]
    # Remove a single leading and trailing single quote
    if s.startswith("'") and s.endswith("'"):
        s = s[1:-1]
    # Return the result with any additional whitespace trimmed
    return s.strip()
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

# ---------- Config ----------
AUTH_SHARED_SECRET = _strip(os.getenv("AUTH_SHARED_SECRET",""))
FRESH_CUTOFF_SECS  = int(os.getenv("FRESH_CUTOFF_SECS", "5400"))  # TV freshness window (90m default)
BLOFIN_TTL_SEC     = int(os.getenv("BLOFIN_TTL_SEC", "240"))       # Port freshness (4m)
BLOFIN_LATEST_PATH = _strip(os.getenv("BLOFIN_LATEST_PATH", "/tmp/blofin_latest.json"))

GREEN_LIST = _split_env_list("GREEN_LIST")
MACRO_LIST = _split_env_list("MACRO_LIST")
FULL_LIST  = _split_env_list("FULL_LIST")
SEL_GREEN, SEL_MACRO, SEL_FULL = _make_selector("GREEN_LIST"), _make_selector("MACRO_LIST"), _make_selector("FULL_LIST")

# ---------- App ----------
app = FastAPI(title="wwasd-relay")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["GET","POST"], allow_headers=["*"])

# ---------- In-memory stores ----------
_tv_latest: Dict[str, Dict[str, Any]] = {}    # symbol -> last WWASD_STATE (with server_received_ms)

_blofin_latest: Optional[Dict[str, Any]] = None
_blofin_last_ms: int = 0
_blofin_lock = threading.Lock()

def _fresh_ms(ts_ms: Optional[int], max_age_secs: int) -> bool:
    if not ts_ms: return False
    return (now_ms() - int(ts_ms)) <= (max_age_secs * 1000)

# ---------- Port disk hardening ----------
def _blofin_write_atomic(obj: Dict[str, Any]) -> None:
    try:
        d = os.path.dirname(BLOFIN_LATEST_PATH)
        if d: os.makedirs(d, exist_ok=True)
        tmp = BLOFIN_LATEST_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
        os.replace(tmp, BLOFIN_LATEST_PATH)  # atomic on Linux/Windows
    except Exception:
        pass  # in-memory still good

def _blofin_load_last() -> Optional[Dict[str, Any]]:
    try:
        with open(BLOFIN_LATEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

# preload last-good so /blofin/latest never starts empty after a restart
_blofin_latest = _blofin_load_last()

# ---------- Security ----------
def _require_token(req: Request) -> None:
    if not AUTH_SHARED_SECRET: return
    token = req.query_params.get("token") or req.headers.get("X-WWASD-Token")
    if token != AUTH_SHARED_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

# ---------- Helpers ----------
def _extract_positions(any_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Accept variant shapes:
      {"data":{"code":"0","data":[...]}}  (Blofin REST)
      {"data":[...]} or {"positions":[...]} or {"payload":{...}}
    """
    if not any_payload: return []
    data = any_payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("data"), list): return data["data"]
    if isinstance(data, list): return data
    if isinstance(any_payload.get("positions"), list): return any_payload["positions"]
    inner = any_payload.get("payload")
    if isinstance(inner, dict): return _extract_positions(inner)
    return []

def _list_selector(name: str) -> Optional[Set[str]]:
    ln = (name or "").lower()
    return SEL_GREEN if ln == "green" else SEL_MACRO if ln == "macro" else SEL_FULL if ln == "full" else None

def _in_named_list(sym: str, list_name: str) -> bool:
    sel = _list_selector(list_name)
    return True if sel is None else bool(_norm_variants(sym) & sel)

# ---------- Routes: root / health ----------
@app.get("/")
def root():
    return PlainTextResponse(
        "wwasd-relay online\n"
        "POST /tv?token=***  (WWASD_STATE or BLOFIN_POSITIONS)\n"
        "GET  /snap?lists=green,macro,full&fresh_only=1  (TV JSON)\n"
        "GET  /snap_ssr.html  (TV SSR)\n"
        "GET  /blofin/latest  (Port JSON)\n"
        "GET  /port2_ssr.html (Port SSR)\n"
        "GET  /port2.html     (Port live view)\n"
        "GET  /snap.json      (TV JSON for restricted clients)\n"
        "GET  /health         (ok,tv_count,port_cached)\n"
    )

@app.get("/health")
def health():
    return {"ok": True, "time": int(time.time()), "tv_count": len(_tv_latest), "port_cached": bool(_blofin_latest)}

# ---------- Ingest ----------
@app.post("/tv")
async def ingest_tv(request: Request):
    _require_token(request)
    # accept JSON or form payload
    try:
        ctype = request.headers.get("content-type","")
        if "application/json" in ctype:
            body = await request.json()
        else:
            try:
                form = await request.form()
                payload = form.get("message") or form.get("payload") or ""
                body = json.loads(payload) if payload else {}
            except Exception:
                raw = await request.body()
                body = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid payload: {e}")

    typ = _upper(body.get("type",""))
    now = now_ms()
    body["server_received_ms"] = now

    if typ == "WWASD_STATE":
        sym = _upper(body.get("symbol",""))
        if not sym: raise HTTPException(status_code=400, detail="missing symbol")
        _tv_latest[sym] = body
        return JSONResponse({"ok": True, "stored": sym}, headers={"Cache-Control": "no-store"})

    if typ == "BLOFIN_POSITIONS":
        global _blofin_latest, _blofin_last_ms
        with _blofin_lock:
            _blofin_latest = body
            _blofin_last_ms = now
            _blofin_write_atomic(body)
        return JSONResponse({"ok": True, "stored": "blofin_positions"}, headers={"Cache-Control": "no-store"})

    return JSONResponse({"ok": True, "ignored": True}, headers={"Cache-Control": "no-store"})

# ---------- TV read endpoints ----------
def _tv_collect(list_name: Optional[str], fresh_only: int, max_age_secs: int) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for sym, item in _tv_latest.items():
        if list_name and not _in_named_list(sym, list_name): continue
        out = dict(item)
        ts = out.get("server_received_ms") or out.get("ts")
        out["is_fresh"] = _fresh_ms(ts, max_age_secs)
        items.append(out)
    items.sort(key=lambda x: x.get("symbol",""))
    if fresh_only: items = [it for it in items if it.get("is_fresh")]
    return {"count": len(items), "items": items}

@app.get("/tv/latest")
def tv_latest(list: str = "", fresh_only: int = 0, max_age_secs: int = FRESH_CUTOFF_SECS):
    name = (list or "").strip().lower()
    return _tv_collect(name or None, fresh_only, max_age_secs)

@app.get("/snap")
def snap(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: int = FRESH_CUTOFF_SECS):
    wanted = [ _strip(x).lower() for x in lists.split(",") if _strip(x) ] or ["green"]
    resp: Dict[str, Any] = {"ts": now_ms(), "lists": {}}
    for name in wanted:
        resp["lists"][name] = _tv_collect(name, fresh_only, max_age_secs)
    return resp

@app.get("/snap_ssr.html")
def snap_ssr(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: int = FRESH_CUTOFF_SECS):
    snap_json = snap(lists=lists, fresh_only=fresh_only, max_age_secs=max_age_secs)
    parts: List[str] = []
    for name, data in snap_json["lists"].items():
        rows = "".join(
            f"<tr><td>{html.escape(it.get('symbol',''))}</td>"
            f"<td>{'fresh' if it.get('is_fresh') else 'stale'}</td></tr>"
            for it in data.get("items",[])
        ) or "<tr><td colspan='2'>No items</td></tr>"
        parts.append(
            f"<h2 style='font:600 14px system-ui;margin:12px 0 6px'>{name.upper()} — count {data.get('count',0)}</h2>"
            f"<table style='width:100%;border-collapse:collapse'>"
            f"<thead><tr><th style='text-align:left;border-bottom:1px solid #1f2937;padding:6px 5px'>Symbol</th>"
            f"<th style='text-align:left;border-bottom:1px solid #1f2937;padding:6px 5px'>Fresh</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    html_doc = (
        "<!doctype html><meta charset='utf-8'/><title>WWASD Snap</title>"
        "<body style='background:#0b0f14;color:#e6edf3;font:14px system-ui;padding:16px'>"
        + "".join(parts) + "</body>"
    )
    return HTMLResponse(html_doc, headers={"Cache-Control": "no-store"})

# ---------- New plain JSON endpoint ----------
@app.get("/snap.json")
def snap_json(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: int = FRESH_CUTOFF_SECS):
    """
    Return the snap JSON for simpler consumption by bots.  Instead of sending an
    application/json content type (which some sandboxed environments block),
    this route serialises the snap payload into a JSON string and serves it
    as plain text with a JSON media type.  This avoids fetch restrictions
    while preserving the same information.
    """
    payload = snap(lists=lists, fresh_only=fresh_only, max_age_secs=max_age_secs)
    # Serve the JSON as plain text; some environments block application/json
    return PlainTextResponse(json.dumps(payload), media_type="application/json")

# ---------- Additional HTML wrapper for JSON (to circumvent browser sandbox blocking) ----------
@app.get("/snap_raw.html")
def snap_raw_html(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: int = FRESH_CUTOFF_SECS):
    """
    Return the snap JSON wrapped in a <pre> tag as HTML.  Some sandboxed browsers
    will not load JSON or text/plain responses from external domains, but they
    allow HTML to render.  This route escapes the JSON and embeds it into a
    <pre> element so it can be viewed and copied from a normal browser.
    """
    payload = snap(lists=lists, fresh_only=fresh_only, max_age_secs=max_age_secs)
    json_str = json.dumps(payload)
    escaped  = html.escape(json_str)
    return HTMLResponse(f"<pre>{escaped}</pre>", headers={"Cache-Control": "no-store"})

# ---------- Port JSON ----------
@app.get("/blofin/latest")
def blofin_latest():
    global _blofin_last_ms, _blofin_latest
    if not _blofin_latest:
        # try disk on first access
        disk = _blofin_load_last()
        if disk:
            with _blofin_lock:
                _blofin_latest = disk
                _blofin_last_ms = disk.get("server_received_ms") or disk.get("ts") or now_ms()
        else:
            return {"fresh": False, "ts": None, "count": 0, "positions": [], "data": None}
    ts = _blofin_last_ms or _blofin_latest.get("client_ts") or _blofin_latest.get("ts")
    fresh = _fresh_ms(int(ts) if ts else None, BLOFIN_TTL_SEC)
    positions = _extract_positions(_blofin_latest.get("data") if _blofin_latest else None)
    return {
        "fresh": fresh,
        "ts": int(ts) if ts else None,
        "count": len(positions),
        "positions": positions,
        "data": _blofin_latest,
    }

# ---------- Port SSR ----------

def _fmt(v: Any) -> str: return html.escape(str(v)) if v is not None else ""

def _render_port_html(latest: Optional[Dict[str, Any]]) -> str:
    fresh = False; ts_ms = None; rows_html = ""
    if latest:
        fresh = bool(latest.get("fresh")); ts_ms = latest.get("ts")
        positions = latest.get("positions") or []
        for p in positions:
            inst = p.get("instId") or p.get("symbol") or "?"
            side = (p.get("positionSide") or p.get("posSide") or p.get("side") or "net").upper()
            sz   = p.get("positions") or p.get("pos") or p.get("size") or p.get("qty") or "-"
            avg  = p.get("averagePrice") or p.get("avgPx") or p.get("avg") or "-"
            mark = p.get("markPrice") or p.get("markPx") or p.get("mark") or "-"
            lev  = p.get("leverage") or p.get("lever") or "-"
            rows_html += f"<tr><td>{_fmt(inst)}</td><td>{_fmt(side)}</td><td>{_fmt(sz)}</td><td>{_fmt(avg)}</td><td>{_fmt(mark)}</td><td>{_fmt(lev)}</td></tr>"
    ts_txt = "-" if not ts_ms else time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(ts_ms)/1000))
    pill = '<span style="padding:.15rem .45rem;border-radius:.5rem;font-size:.8rem;background:#2a6c2a;color:#dff0d8">fresh</span>' \
           if fresh else '<span style="padding:.15rem .45rem;border-radius:.5rem;font-size:.8rem;background:#5a5a5a;color:#eee">stale</span>'
    table = rows_html or '<tr><td colspan="6" style="opacity:.6">No open positions</td></tr>'
    return f"""<!doctype html><html><head><meta charset="utf-8"/><title>WWASD Port</title>
<style>
 body{{background:#0f1115;color:#e6e6e6;font:14px/1.4 system-ui,Segoe UI,Inter,Arial}}
 .wrap{{max-width:980px;margin:24px auto;padding:0 16px}}
 h1{{font-size:18px;margin:0 0 8px}}
 table{{width:100%;border-collapse:collapse;border-spacing:0;margin-top:12px}}
 th,td{{padding:8px 10px;border-bottom:1px solid #222;white-space:nowrap}}
 th{{text-align:left;color:#a9b0bc;font-weight:600}}
 .sub{{color:#8a92a6;font-size:.9rem}}
</style>
</head><body><div class="wrap">
  <h1>WWASD Port <span class="sub">last update (server): {ts_txt} {pill}</span></h1>
  <table>
    <thead><tr><th>Instrument</th><th>Side</th><th>Sz</th><th>Avg</th><th>Mark</th><th>Lev</th></tr></thead>
    <tbody>{table}</tbody>
  </table>
</div></body></html>"""

@app.get("/port2_ssr.html")
def port2_ssr_html(): return HTMLResponse(_render_port_html(blofin_latest()), headers={"Cache-Control":"no-store"})

@app.get("/port2.html")
def port2_html():
    html_doc = """<!doctype html><html lang="en"><head><meta charset="utf-8">
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
    const r = await fetch('/blofin/latest', {cache:'no-store'}); const o = await r.json();
    err.textContent=""; pill.textContent = o.fresh ? "FRESH" : "STALE";
    pill.className = "pill " + (o.fresh ? "fresh" : "stale");
    stamp.textContent = "server ts: " + (o.ts ?? "—");
    const data = (o && o.positions) || [];
    tb.innerHTML=""; let sum=0;
    for(const p of data){
      const side = (p.positionSide||p.posSide||p.side||"").toUpperCase();
      const up = parseFloat(p.unrealizedPnl||0); sum += (isFinite(up)?up:0);
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${p.instId||p.symbol||""}</td><td>${side}</td><td>${fmt(p.positions||p.pos||p.size||p.qty)}</td>
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
    return HTMLResponse(html_doc, headers={"Cache-Control":"no-store"})

