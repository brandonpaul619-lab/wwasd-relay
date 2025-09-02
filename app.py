# app.py — WWASD Relay v2.7 (adds /snap.json)
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
    s = (s or "").strip()
    if s.startswith(("'", '"')) and s.endswith(("'", '"')) and len(s) >= 2:
        s = s[1:-1]
    return s

def _upper(s: Optional[str]) -> str: return (s or "").strip().upper()

def _split_env_list(key: str) -> List[str]:
    raw = os.getenv(key, "") or ""
    if not raw: return []
    # support commas and whitespace
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
TV_LATEST_CACHE_PATH = _strip(os.getenv("TV_LATEST_CACHE_PATH", "/tmp/tv_latest.json"))


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
    data = any_payload.get("data", any_payload)
    if isinstance(data, dict) and "data" in data: data = data["data"]
    if isinstance(data, dict) and "positions" in data: data = data["positions"]
    if isinstance(data, list): return data
    return []

def _in_named_list(sym: str, name: str) -> bool:
    sset = SEL_GREEN if name == "green" else SEL_MACRO if name == "macro" else SEL_FULL
    if not sset: return True
    norm = _norm_variants(sym)
    return any(n in sset for n in norm)

def _dict(item: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow-copy dict (don’t leak internal refs)."""
    return dict(item or {})

# ---------- Home ----------
@app.get("/")
def home():
    return PlainTextResponse(
        "WWASD Relay\n"
        "POST /tv             (ingest; WWASD_STATE or BLOFIN_POSITIONS)\n"
        "GET  /tv/latest      (TV collation snapshot)\n"
        "GET  /snap           (TV collation for lists)\n"
        "GET  /snap_ssr.html  (SSR list preview)\n"
        "GET  /snap_raw.html  (HTML-wrapped JSON)\n"
        "GET  /snap_plain.txt (plain text JSON)\n"
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
    payload = _tv_collect(name or None, fresh_only, max_age_secs)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})

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
    # Pretty-print the JSON with indentation to ensure line breaks for sandbox viewers
    json_str = json.dumps(payload, indent=2)
    escaped  = html.escape(json_str)
    return HTMLResponse(f"<pre>{escaped}</pre>", headers={"Cache-Control": "no-store"})

# ---------- Plain-text snapshot (for environments that can’t render HTML or JSON) ----------
@app.get("/snap_plain.txt")
def snap_plain_txt(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: int = FRESH_CUTOFF_SECS):
    """
    Return the snap JSON as indented plain text.  Some sandboxed browsers won’t
    display JSON or HTML properly, but they will display a text/plain response.
    The indentation makes it readable and parsable.
    """
    payload = snap(lists=lists, fresh_only=fresh_only, max_age_secs=max_age_secs)
    pretty  = json.dumps(payload, indent=2)
    return PlainTextResponse(pretty, media_type="text/plain")

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
    return f"""
<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>WWASD Port</title>
<style>
 body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Arial,sans-serif;margin:24px;background:#0b0f14;color:#e6edf3}}
 table{{width:100%;border-collapse:collapse;margin-top:12px}}
 th,td{{border-bottom:1px solid #1f2937;padding:8px;text-align:left;font-size:14px}}
 .pill{{padding:.15rem .45rem;border-radius:.5rem;font-size:.8rem}}
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
  <div class="muted">Server time: <span id="srvts"></span></div>
</div>
<pre id="err" style="color:#9a3412"></pre>
<table>
  <thead><tr><th>Instrument</th><th>Side</th><th>Sz</th><th>Avg</th><th>Mark</th><th>Lev</th></tr></thead>
  <tbody id="tb"></tbody>
</table>
<div class="muted" id="upnl">uPnL (sum): -</div>
<script>
async function load(){
  const pill = document.getElementById('statusPill');
  const tb   = document.getElementById('tb');
  const err  = document.getElementById('err');
  const upnl = document.getElementById('upnl');
  const srv  = document.getElementById('srvts');
  try{
    const r = await fetch('/blofin/latest', {cache:'no-store'});
    const j = await r.json();
    srv.textContent = new Date().toISOString().slice(0,19).replace('T',' ');
    tb.innerHTML = '';
    let sum = 0;
    const fresh = !!j.fresh;
    pill.textContent = fresh ? 'FRESH' : 'STALE';
    pill.className = 'pill ' + (fresh ? 'fresh' : 'stale');
    const fmt = (v)=> (v==null ? '-' : (typeof v==='number' ? v.toFixed(4) : String(v)));
    for(const p of (j.positions||[])){
      const tr = document.createElement('tr');
      const side = (p.positionSide||p.posSide||p.side||'net').toUpperCase();
      const up = Number(p.unrealizedPnl || p.upl || p.uPnL || 0);
      sum += isFinite(up) ? up : 0;
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
# ──────────────────────────────────────────────────────────────────────────────
# ADD: Tiny TV disk cache + CSV/HTML snapshot endpoints (append‑only)
# ──────────────────────────────────────────────────────────────────────────────

# Safe atomic write of the in‑memory TV snapshot to disk
def _tv_write_atomic(obj: Dict[str, Dict[str, Any]]) -> None:
    try:
        path = TV_LATEST_CACHE_PATH
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            # Store a minimal object we can re‑hydrate from
            json.dump({"items": obj, "ts": now_ms()}, f, separators=(",", ":"), ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        # Non‑fatal; in‑memory state still used
        pass

# Load the last snapshot if it exists (pre‑warm after cold start)
def _tv_load_last() -> Optional[Dict[str, Dict[str, Any]]]:
    try:
        path = TV_LATEST_CACHE_PATH
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items")
        return items if isinstance(items, dict) else None
    except Exception:
        return None

# Pre‑warm _tv_latest at boot (no route changes)
try:
    _pre = _tv_load_last()
    if _pre:
        _tv_latest.update(_pre)
except Exception:
    pass

# Background saver: write snapshot every ~10s so /snap* never starts empty
def _tv_saver_loop():
    while True:
        time.sleep(10)
        try:
            _tv_write_atomic(_tv_latest)
        except Exception:
            pass

try:
    # Start once; daemon thread so it won't block shutdown
    _tv_saver_thr_started  # type: ignore[name-defined]
except NameError:
    _tv_saver_thr_started = True
    threading.Thread(target=_tv_saver_loop, daemon=True).start()

# Helper to flatten snap() into rows for CSV/HTML
def _rows_from_snap(snap_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    lists_obj = snap_json.get("lists", {}) or {}
    for _, pack in lists_obj.items():
        for it in pack.get("items", []) or []:
            sym = it.get("symbol", "")
            if sym in seen:
                continue
            seen.add(sym)
            oneD = (it.get("mtf") or {}).get("1D") or {}
            htf  = it.get("htf") or {}
            rows.append({
                "symbol": sym,
                "cmp": it.get("cmp"),
                "ema12_state": oneD.get("ema12_state"),
                "qvwap_state": oneD.get("qvwap_state") or oneD.get("qv_state"),
                "hh": oneD.get("hh"),
                "hl": oneD.get("hl"),
                "lh": oneD.get("lh"),
                "ll": oneD.get("ll"),
                "rsi": oneD.get("rsi", it.get("rsi")),
                "is_fresh": it.get("is_fresh"),
                "htf_sig": htf.get("sig"),
                "htf_rating": htf.get("rating"),
            })
    return rows
    
# CSV snapshot (desk‑friendly)
@app.get("/snap.csv")
def snap_csv(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: int = FRESH_CUTOFF_SECS):
    payload = snap(lists=lists, fresh_only=fresh_only, max_age_secs=max_age_secs)
    rows = _rows_from_snap(payload)
    cols = ["symbol","cmp","ema12_state","qvwap_state","hh","hl","lh","ll","rsi","is_fresh","htf_sig","htf_rating"]

    def _fmt(v: Any) -> str:
        if isinstance(v, bool): return "true" if v else "false"
        if v is None: return ""
        s = str(v)
        if any(c in s for c in [",", "\"", "\n"]):
            s = s.replace("\"", "\"\"")
            s = f"\"{s}\""
        return s

    lines = [",".join(cols)]
    for r in rows:
        lines.append(",".join(_fmt(r.get(c)) for c in cols))
    body = "\n".join(lines)
    return PlainTextResponse(body, media_type="text/csv", headers={"Cache-Control": "no-store"})

# Simple HTML table snapshot (for sandboxes that can’t parse JSON)
@app.get("/snap_table.html")
def snap_table_html(lists: str = "green,macro,full", fresh_only: int = 1, max_age_secs: int = FRESH_CUTOFF_SECS):
    payload = snap(lists=lists, fresh_only=fresh_only, max_age_secs=max_age_secs)
    rows = _rows_from_snap(payload)
    head = f"""<!doctype html><meta charset="utf-8"><title>WWASD Snap Table</title>
<style>
 body{{background:#0b0f14;color:#e6edf3;font:14px system-ui;padding:16px}}
 table{{width:100%;border-collapse:collapse}}
 th,td{{border-bottom:1px solid #1f2937;padding:6px 5px;text-align:left}}
 th{{color:#a9b0bc;font-weight:600}}
 .good{{color:#34d399}} .bad{{color:#f87171}} .muted{{color:#94a3b8}}
</style>
<h1>WWASD Snapshot <span class="muted">(fresh_only={fresh_only})</span></h1>
<table><thead><tr>
  <th>Symbol</th><th>CMP</th><th>EMA12</th><th>QVWAP</th><th>HH</th><th>HL</th><th>LH</th><th>LL</th>
  <th>RSI</th><th>Fresh</th><th>HTF Sig</th><th>HTF Rating</th>
</tr></thead><tbody>"""
    def td(v: Any, cls: str = "") -> str:
        s = html.escape("" if v is None else str(v))
        c = f' class="{cls}"' if cls else ""
        return f"<td{c}>{s}</td>"

    body_rows = []
    for r in rows:
        fresh_cls = "good" if r.get("is_fresh") else "bad"
        body_rows.append(
            "<tr>" +
            td(r.get("symbol")) + td(r.get("cmp")) + td(r.get("ema12_state")) + td(r.get("qvwap_state")) +
            td(r.get("hh")) + td(r.get("hl")) + td(r.get("lh")) + td(r.get("ll")) +
            td(r.get("rsi")) + td("fresh" if r.get("is_fresh") else "stale", fresh_cls) +
            td(r.get("htf_sig")) + td(r.get("htf_rating")) +
            "</tr>"
        )
    html_doc = head + "\n".join(body_rows) + "</tbody></table>"
    return HTMLResponse(html_doc, headers={"Cache-Control": "no-store"})
# ──────────────────────────────────────────────────────────────────────────────
