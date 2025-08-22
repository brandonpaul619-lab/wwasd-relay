import os, json, time, hmac, hashlib
from flask import Flask, request, jsonify, abort
from dotenv import load_dotenv
import requests

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
AUTH_SHARED    = os.getenv("AUTH_SHARED_SECRET", "")
PORT           = int(os.getenv("RELAY_PORT", "5000"))
DRY_RUN        = os.getenv("DRY_RUN", "false").lower() == "true"

app = Flask(__name__)

def verify_signature(req):
    if not AUTH_SHARED:
        return True
    provided = req.headers.get("X-Signature", "")
    mac = hmac.new(AUTH_SHARED.encode(), req.data, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, mac)

def normalize_payload(raw):
    if isinstance(raw, dict) and isinstance(raw.get("message"), str):
        try:
            inner = json.loads(raw["message"])
            raw.update(inner)
        except Exception:
            pass
    out = {
        "symbol": raw.get("symbol") or raw.get("ticker") or raw.get("s"),
        "timeframe": raw.get("timeframe") or raw.get("tf") or raw.get("interval"),
        "cmp": float(raw.get("CMP") or raw.get("close") or raw.get("price") or 0),
        "vwap": raw.get("VWAP") or raw.get("vwap"),
        "ema12": raw.get("EMA12") or raw.get("ema12"),
        "tvem_htf": raw.get("TVEM_HTF") or raw.get("tvem_htf"),
        "tvem_ltf": raw.get("TVEM_LTF") or raw.get("tvem_ltf"),
        "qvwap_1d": raw.get("QVWAP_1D") or raw.get("qvwap_1d"),
        "ts": int(raw.get("ts") or time.time()),
        "raw": raw
    }
    return out

def call_gpt5(struct):
    if DRY_RUN:
        return {
            "symbol": struct.get("symbol") or "UNKNOWN",
            "timeframe": struct.get("timeframe") or "UNKNOWN",
            "cmp": struct.get("cmp") or 0,
            "bias": "neutral",
            "structure": "dry-run",
            "confirmations": ["dry-run"],
            "sniper": {"entry": struct.get("cmp") or 0, "dca": struct.get("cmp") or 0, "sl": 0, "tp1": 0, "tp2": 0, "leverage": 10, "rating": 5.0},
            "notes": "DRY_RUN=true â€” OpenAI call skipped"
        }
    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY")
    url = "https://api.openai.com/v1/responses"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    schema = {
        "name": "wwasd_sniper",
        "schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "timeframe": {"type": "string"},
                "cmp": {"type": "number"},
                "bias": {"type": "string", "enum": ["long","short","neutral"]},
                "structure": {"type": "string"},
                "confirmations": {"type": "array", "items": {"type": "string"}},
                "sniper": {
                    "type": "object",
                    "properties": {
                        "entry": {"type": "number"},
                        "dca": {"type": "number"},
                        "sl": {"type": "number"},
                        "tp1": {"type": "number"},
                        "tp2": {"type": "number"},
                        "leverage": {"type": "integer"},
                        "rating": {"type": "number"}
                    },
                    "required": ["entry","sl","tp1","tp2","leverage","rating"]
                },
                "notes": {"type": "string"}
            },
            "required": ["symbol","timeframe","cmp","bias","structure","confirmations","sniper"]
        },
        "strict": True
    }
    prompt = [
        {"role":"system","content":[{"type":"text","text":"Analyze TradingView alert JSON from WWASD_State_Emitter using Arsh structure then Sherlock confirmations. Return ONLY JSON that matches the schema."}]},
        {"role":"user","content":[{"type":"text","text": json.dumps(struct, ensure_ascii=False)}]}
    ]
    body = {"model": "gpt-5", "input": prompt, "response_format": {"type":"json_schema","json_schema": schema}, "verbosity":"low", "reasoning":{"effort":"medium"}}
    r = requests.post(url, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    data = r.json()
    content = data["output"][0]["content"][0]["text"]
    return json.loads(content)

@app.route("/tv", methods=["POST"])
def tv():
    if not verify_signature(request): abort(401)
    raw = request.get_json(silent=True)
    if not raw:
        try: raw = json.loads(request.data.decode("utf-8","ignore"))
        except Exception: raw = {"message": request.data.decode("utf-8","ignore")}
    struct = normalize_payload(raw)
    try:
        analysis = call_gpt5(struct)
    except Exception as e:
        return jsonify({"status":"error","detail":str(e)}), 500
    return jsonify({"status":"ok","analysis":analysis}), 200

@app.route("/scan-now", methods=["GET","POST"])
def scan_now():
    if request.method == "POST":
        raw = request.get_json(silent=True) or {}
        struct = normalize_payload(raw)
        try:
            analysis = call_gpt5(struct)
        except Exception as e:
            return jsonify({"status":"error","detail":str(e)}), 500
        return jsonify(analysis), 200
    return jsonify({"ok": True, "hint": "POST a sample alert body to test (DRY_RUN recommended)"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
