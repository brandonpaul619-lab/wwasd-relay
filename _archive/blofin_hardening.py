# blofin_hardening.py  —  Permanent hardening for Blofin Port endpoints
import os, json, time, threading
from flask import Blueprint, current_app

TTL_SEC = int(os.getenv("BLOFIN_TTL_SEC", "240"))  # freshness window
DATA_PATH = os.getenv("BLOFIN_LATEST_PATH", "/tmp/blofin_latest.json")

class _BlofinStore:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.last = {"fresh": False, "ts": None, "data": None}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.last = json.load(f)
        except Exception:
            pass

    def _write_atomic(self, obj):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
        os.replace(tmp, self.path)  # atomic on POSIX & Windows

    def update(self, payload: dict):
        now = int(time.time() * 1000)
        ts = int(payload.get("ts") or now)
        obj = {
            "fresh": True,                  # will be recomputed on read
            "ts": ts,                       # source timestamp from pusher
            "data": payload,                # full payload from pusher
            "server_received_ms": now,      # server arrival
        }
        with self.lock:
            self.last = obj
            try:
                self._write_atomic(obj)
            except Exception:
                # keep in-memory copy even if disk write fails
                pass

    def latest(self) -> dict:
        now = int(time.time() * 1000)
        with self.lock:
            obj = dict(self.last)  # shallow copy

        ts = obj.get("ts")
        has_data = bool(obj.get("data"))
        fresh = bool(ts) and (now - int(ts)) <= TTL_SEC * 1000
        obj["fresh"] = has_data and fresh
        obj["age_sec"] = None if not ts else round((now - int(ts)) / 1000, 2)
        return obj

_store = _BlofinStore(DATA_PATH)
bp = Blueprint("blofin", __name__)

@bp.route("/blofin/latest")
def blofin_latest():
    """NEVER 500. Always returns a simple JSON envelope with .fresh/.ts/.age_sec/.data."""
    obj = _store.latest()
    resp = current_app.response_class(
        response=json.dumps(obj, separators=(",", ":")),
        status=200,
        mimetype="application/json",
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp

@bp.route("/port2.html")
def port2_html():
    # Minimal, bot-safe HTML that *fetches* /blofin/latest and renders with stale banner.
    html = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WWASD Port</title>
<style>
 body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Arial,sans-serif;margin:24px}
 .row{display:flex;gap:8px;align-items:center}
 .pill{padding:4px 10px;border-radius:999px;font-size:12px;color:#fff}
 .fresh{background:#16a34a}.stale{background:#b91c1c}.warn{background:#ca8a04}
 table{width:100%;border-collapse:collapse;margin-top:12px}
 th,td{border-bottom:1px solid #e5e7eb;padding:8px;text-align:left;font-size:14px}
 code{background:#f3f4f6;padding:2px 6px;border-radius:4px}
 .muted{color:#6b7280}
</style>
</head><body>
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
      const side = (p.positionSide||"").toUpperCase();
      const up = parseFloat(p.unrealizedPnl||0);
      sum += (isFinite(up)?up:0);
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${p.instId||""}</td><td>${side}</td><td>${fmt(p.positions)}</td>
                      <td>${fmt(p.averagePrice)}</td><td>${fmt(p.markPrice)}</td>
                      <td>${fmt(up)}</td><td>${fmt(p.leverage)}</td>`;
      tb.appendChild(tr);
    }
    upnl.textContent = "uPnL (sum): " + fmt(sum);
  }catch(e){
    pill.textContent = "ERROR"; pill.className="pill warn";
    err.textContent = "Fetch failed. Will retry… " + e;
  }
}
load();
setInterval(load, 12000);
</script>
</body></html>"""
    resp = current_app.response_class(response=html, status=200, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp

def register_blofin_hardening(app):
    app.register_blueprint(bp)

def handle_blofin_payload(payload: dict):
    """Call this inside your existing /tv handler when type == 'BLOFIN_POSITIONS'."""
    _store.update(payload)
