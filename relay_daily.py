"""
Simple relay server to forward TradingView webhook alerts to ChatGPT (or any
downstream consumer).  This server exposes two endpoints:

    POST /tv      Receives alerts from TradingView.  The JSON payload emitted by
                  the WWASD_State_Emitter indicator is parsed, augmented with
                  the accompanying snapshot URL (if provided by TradingView),
                  and forwarded to the ChatGPT ingestion API or a Discord bot.

    POST /scan-now
                  Triggers an immediate scan of all tickers and timeframes.
                  When called, your Playwright automation should loop over the
                  watchlists, apply the indicator and template, and generate
                  immediate alerts.  This endpoint simply acknowledges the
                  request; the scan logic is expected to run in the alert
                  automation layer.

The server uses Flask for simplicity.  To run:

    export RELAY_PORT=8000
    export CHATGPT_WEBHOOK_URL=https://your-chatgpt-endpoint.example/api
    python relay.py

Note: Do **not** expose this server publicly without securing it (e.g., using
API keys).  The implementation is intentionally simple and should be adapted
for production use.
"""

import json
import os
from datetime import datetime, timezone

from flask import Flask, request, jsonify
import requests
from collections import defaultdict

app = Flask(__name__)

# Retrieve ChatGPT destination from environment.
CHATGPT_WEBHOOK_URL = os.environ.get("CHATGPT_WEBHOOK_URL", "http://localhost:9999/ingest")

# In-memory log of today's alerts. Each element is a dict containing at least
# 'ticker' and 'ts'. This will be appended to every time /tv receives a
# webhook. It resets when a new UTC day starts.
alert_log = []
last_log_date = None

def reset_alert_log_if_new_day() -> None:
    """
    Check if the current UTC date differs from the date of the last logged
    alert. If it does, clear the in-memory alert log.
    """
    global last_log_date, alert_log
    current_date = datetime.now(timezone.utc).date()
    if last_log_date is None or current_date != last_log_date:
        alert_log = []
        last_log_date = current_date


def forward_to_chatgpt(payload: dict):
    """Send the alert payload to ChatGPT or a Discord bot via webhook."""
    try:
        resp = requests.post(CHATGPT_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"Error forwarding alert: {e}")


@app.route("/tv", methods=["POST"])
def tradingview_webhook():
    """
    Handle TradingView webhook POST.  TradingView sends a JSON payload in the
    alert message (the result of Pine's alert() call) and may include a chart
    snapshot URL in the top‑level JSON body under `image` or `image_url`.
    """
    # TradingView sends a plain text body by default.  Attempt to parse as JSON.
    body = request.data.decode('utf-8', errors='ignore')
    snapshot_url = None
    # Some versions of TradingView include an `image` field containing a URL.
    try:
        tv_payload = request.get_json(force=False, silent=True)
        if tv_payload and isinstance(tv_payload, dict):
            snapshot_url = tv_payload.get("image") or tv_payload.get("image_url")
            # The message itself may be under the "message" key if provided.
            body = tv_payload.get("message", body)
    except Exception:
        pass
    # Clean up the JSON string (payload) from the Pine alert.
    try:
        alert_data = json.loads(body)
    except json.JSONDecodeError:
        # If parsing fails, wrap in a dict with raw body.
        alert_data = {"raw": body.strip()}
    # Append snapshot URL and timestamp.
    alert_data["snapshot_url"] = snapshot_url
    alert_data["ts"] = datetime.now(timezone.utc).isoformat()
    # Forward to ChatGPT/Discord.
    forward_to_chatgpt(alert_data)

    # Log the alert for daily summary
    reset_alert_log_if_new_day()
    # Record only basic info to avoid storing large payloads
    alert_log.append({
        "ticker": alert_data.get("ticker"),
        "signal": alert_data.get("signal"),
        "ts": alert_data["ts"],
    })
    return jsonify({"status": "ok"})


@app.route("/scan-now", methods=["POST"])
def scan_now():
    """
    Endpoint to request an immediate scan of all watchlist symbols.  Your alert
    automation should detect this call and iterate through every ticker/timeframe
    (as Playwright does during initial setup) to emit a fresh set of alerts.
    """
    # In practice, you could enqueue a job or emit a message to your
    # automation worker here.  For demonstration we simply log the request.
    print("Received on‑demand scan request.")
    return jsonify({"status": "scan scheduled"})


@app.route("/daily-summary", methods=["GET"])
def daily_summary():
    """
    Provide a summary of today's alerts and 24-hour price snapshots. This
    endpoint returns a JSON object with a 'summary' field containing a
    human-readable text summary. It is intended to be queried on-demand
    (e.g., by ChatGPT) and does not push anything to ChatGPT automatically.
    """
    # Reset the log if the date has changed
    reset_alert_log_if_new_day()
    # Aggregate counts per ticker
    counts = defaultdict(int)
    for entry in alert_log:
        ticker = entry.get("ticker")
        if ticker:
            counts[ticker] += 1
    lines = []
    lines.append(f"Daily wrap-up for {datetime.now(timezone.utc).date()}")
    lines.append("")
    if not counts:
        lines.append("No alerts have been logged today.")
    else:
        lines.append("Alerts per ticker:")
        for ticker, count in counts.items():
            plural = "s" if count != 1 else ""
            lines.append(f"• {ticker}: {count} alert{plural}")
        lines.append("")
        lines.append("24-hour price snapshot:")
        for ticker in counts.keys():
            # Convert ticker to BloFin instrument ID (e.g. BTCUSDT -> BTC-USDT)
            inst_id = ticker.replace("_", "-")
            try:
                resp = requests.get(
                    "https://openapi.blofin.com/api/v1/market/tickers",
                    params={"instId": inst_id},
                    timeout=10,
                )
                data = resp.json().get("data")
                if data and isinstance(data, list):
                    last = data[0].get("last")
                    high24h = data[0].get("high24h")
                    open24h = data[0].get("open24h")
                    pct_change = None
                    try:
                        pct_change = (float(last) - float(open24h)) / float(open24h) * 100
                    except Exception:
                        pass
                    change_str = f"({pct_change:+.2f}% over 24h)" if pct_change is not None else ""
                    lines.append(
                        f"• {ticker}: last {last}, high {high24h}, open {open24h} {change_str}"
                    )
                else:
                    lines.append(f"• {ticker}: price data unavailable")
            except Exception:
                lines.append(f"• {ticker}: price data unavailable")
    summary_text = "\n".join(lines)
    return jsonify({"summary": summary_text})


if __name__ == "__main__":
    port = int(os.environ.get("RELAY_PORT", 8000))
    app.run(host="0.0.0.0", port=port)