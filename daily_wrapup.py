"""
daily_wrapup.py
----------------

This script compiles a daily summary of your TradingView alerts and market data,
and sends the summary to your alert relay for ChatGPT to process.  It is
designed to be copied and run without modification once you've configured
``alerts_log.json`` and the relay URL.

How it works:

1. Throughout the day, your alert automation (for example, the Playwright
   scanner) should append each alert event to a JSON file called
   ``alerts_log.json``.  Each entry in the file should be a dictionary with
   at least the keys ``ticker`` and ``ts`` (timestamp), and optionally
   ``signal`` or any other context.
2. At the end of the trading day, run this script.  It will:
   - Read all events for the current date from ``alerts_log.json``.
   - Aggregate the number of alerts per ticker.
   - Query BloFin's public API for the current price and 24‑hour
     change for each ticker.
   - Build a human‑readable summary message.
   - Send that summary to your relay via its ``/tv`` endpoint.

Configuration:

Set the following variables near the top of the script:

RELAY_URL = "https://2513494ca72c.ngrok-free.app/tv"
    The public URL of your relay server's ``/tv`` endpoint (including the
    ``https://.../tv`` path).  For example:

        RELAY_URL = "https://2513494ca72c.ngrok-free.app/tv"

``ALERT_LOG``
    The path to the JSON file where alerts are logged.  The default is
    ``alerts_log.json`` in the same directory as this script.

Once configured, run the script with:

    python daily_wrapup.py

Dependencies:

This script requires the ``requests`` library.  Install it with:

    pip install requests

Note:

This script does not access any account‑level data.  It uses BloFin's
public market API to fetch prices and computes simple summary statistics.
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List

import requests


# ----- Configuration -------------------------------------------------------

# Public relay URL (include the /tv path)
RELAY_URL = "https://your-ngrok-url.ngrok-free.app/tv"  # <-- Replace with your relay URL

# Path to the alert log file (JSON array of alert events)
ALERT_LOG = "alerts_log.json"

# --------------------------------------------------------------------------


def load_alerts(log_path: str) -> List[Dict]:
    """
    Load alert events from the specified JSON log file.

    The log file should contain a JSON array of dictionaries.  Each
    dictionary must include at least a ``ticker`` key and a timestamp ``ts``.
    The timestamp should be an ISO‑formatted string (UTC) or a Unix
    timestamp in milliseconds.

    Parameters
    ----------
    log_path : str
        Path to the alert log JSON file.

    Returns
    -------
    List[Dict]
        A list of alert dictionaries.  Returns an empty list if the file
        does not exist or is empty.
    """
    if not os.path.exists(log_path):
        return []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return []
    except Exception:
        return []


def parse_timestamp(ts) -> datetime:
    """
    Convert a timestamp (ISO string or Unix milliseconds) to a datetime object.

    Parameters
    ----------
    ts : Union[str, int, float]
        Timestamp value to parse.

    Returns
    -------
    datetime
        Parsed datetime in UTC.  If parsing fails, returns the current UTC time.
    """
    if isinstance(ts, (int, float)):
        # Assume milliseconds since epoch
        try:
            return datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def filter_today(alerts: List[Dict]) -> List[Dict]:
    """
    Filter a list of alerts to only include those that occurred today (UTC).

    Parameters
    ----------
    alerts : List[Dict]
        List of alert events.

    Returns
    -------
    List[Dict]
        Alerts that occurred on the current UTC date.
    """
    today = datetime.now(timezone.utc).date()
    return [a for a in alerts if parse_timestamp(a.get("ts")).date() == today]


def fetch_blofin_price(inst_id: str) -> Dict[str, str]:
    """
    Fetch the last price and 24‑hour change from BloFin's public ticker API.

    Parameters
    ----------
    inst_id : str
        Instrument ID in the form ``BTC-USDT`` or similar.

    Returns
    -------
    Dict[str, str]
        Dictionary containing keys ``last`` (last traded price), ``high24h``,
        and ``open24h``.  If the request fails, returns an empty dict.
    """
    try:
        resp = requests.get(
            "https://openapi.blofin.com/api/v1/market/tickers",
            params={"instId": inst_id},
            timeout=10,
        )
        data = resp.json().get("data")
        if data and isinstance(data, list):
            return {
                "last": data[0].get("last"),
                "high24h": data[0].get("high24h"),
                "open24h": data[0].get("open24h"),
            }
    except Exception:
        pass
    return {}


def build_summary(alerts_today: List[Dict]) -> str:
    """
    Build a textual summary of today's alerts and price movements.

    Parameters
    ----------
    alerts_today : List[Dict]
        Alerts that occurred today.

    Returns
    -------
    str
        A human‑readable summary of the day's activity.
    """
    # Count alerts per ticker
    counts: Dict[str, int] = defaultdict(int)
    for alert in alerts_today:
        ticker = alert.get("ticker")
        if ticker:
            counts[ticker] += 1

    # Build summary lines
    lines = []
    lines.append(f"Daily wrap‑up for {datetime.now(timezone.utc).date()}")
    lines.append("")
    if not counts:
        lines.append("No alerts were recorded today.")
    else:
        lines.append("Alerts per ticker:")
        for ticker, count in counts.items():
            lines.append(f"• {ticker}: {count} alert{'s' if count != 1 else ''}")
    lines.append("")

    # For each ticker, fetch current and 24h price data
    if counts:
        lines.append("24‑hour price snapshot:")
        for ticker in counts.keys():
            # Convert symbol from format like "BTCUSDT" or "BTC-USDT" to BloFin instId
            inst_id = ticker.replace("", "").replace("_", "-")
            price_data = fetch_blofin_price(inst_id)
            if price_data:
                last = price_data.get("last")
                high = price_data.get("high24h")
                open_ = price_data.get("open24h")
                # Compute percentage change from open to last
                pct_change = None
                try:
                    pct_change = (float(last) - float(open_)) / float(open_) * 100
                except Exception:
                    pass
                change_str = f"({pct_change:+.2f}% over 24h)" if pct_change is not None else ""
                lines.append(f"• {ticker}: last {last}, high {high}, open {open_} {change_str}")
            else:
                lines.append(f"• {ticker}: price data unavailable")
    return "\n".join(lines)


def send_summary_to_relay(summary: str) -> None:
    """
    Send the summary message to the relay via the /tv endpoint.

    Parameters
    ----------
    summary : str
        The summary text to send.
    """
    if not RELAY_URL or "your-ngrok-url" in RELAY_URL:
        print("Relay URL is not configured. Please set RELAY_URL at the top of the script.")
        return
    payload = {
        "message": summary,
        "type": "daily_summary",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        resp = requests.post(RELAY_URL, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"Summary sent to relay: status {resp.status_code}")
    except Exception as exc:
        print(f"Error sending summary: {exc}")


def main() -> None:
    """
    Main entry point for the daily wrap‑up script.
    """
    alerts = load_alerts(ALERT_LOG)
    alerts_today = filter_today(alerts)
    summary = build_summary(alerts_today)
    print(summary)
    send_summary_to_relay(summary)


if __name__ == "__main__":
    main()