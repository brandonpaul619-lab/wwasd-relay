# WWASD TradingView Bridge Runbook

This runbook describes how to deploy the WWASD TradingView bridge that
automatically exports CMPs, indicator states and chart snapshots for every
ticker in Brandon’s **Green** and **Macro** watchlists.  The system uses a
Pine v5 indicator to compute the states, Playwright to configure alerts, and a
Flask relay to forward TradingView webhooks to ChatGPT.

## 1. Prerequisites

* You have a **TradingView** account with the **Green List** and **Macro List**
  watchlists already created.  Ensure each ticker you care about is present
  in these lists.  The bridge will pull symbols directly from the UI.
* You have **LuxAlgo** indicators and your default template saved in TradingView.
  This template should include POC, TRAMA, RSI divergence and VRVP.  Name it
  something like `My Lux Template`.
* You have **Python** (3.8+) installed along with the following packages:

  ```bash
  pip install playwright Flask requests python-dotenv
  playwright install chromium
  ```
* Optionally, a Discord bot or ChatGPT ingestion endpoint to receive the JSON
  data.  You will configure its URL in `.env`.

## 2. Files

This repository contains the following key files:

| File | Description |
| --- | --- |
| `WWASD_State_Emitter.pine` | Pine v5 indicator that computes TVEM HTF/LTF, 1D VWAP and 1D EMA12 states and emits a JSON payload via `alert()` on every bar close. |
| `setup_alerts.py` | Playwright automation script that logs into TradingView, iterates your watchlists, loads your template and the WWASD indicator, and creates alerts on 1D/4H/1H/15M (and 5M when sniper mode is enabled). |
| `relay.py` | Flask server to receive TradingView webhooks and forward the JSON payload plus snapshot URL to ChatGPT or a Discord bot. |
| `.env.template` | Template for environment variables required by the scripts.  Copy to `.env` and fill in your values. |
| `RUNBOOK.md` | This document. |

## 3. Setup

1. **Copy environment template**

   ```bash
   cp wwasd_bridge_final/.env.template wwasd_bridge_final/.env
   # Edit .env and fill in your TradingView credentials, relay URL and ChatGPT webhook.
   ```

2. **Deploy the relay**

   The relay receives webhooks from TradingView and forwards them to ChatGPT/Discord.

   ```bash
   cd wwasd_bridge_final
   python relay.py
   # Relay listens on port defined in .env (default 8000)
   ```

3. **Add WWASD indicator to your TradingView account**

   1. Open TradingView in your browser.
   2. Create a new indicator from the Pine editor and paste the contents of
      `WWASD_State_Emitter.pine`.
   3. Save and add it to one of your charts.

## 4. Configure alerts via Playwright

Run the setup script for each watchlist.  It will log in, select the watchlist,
apply your template, add the WWASD indicator, and create alerts on each
timeframe.

```bash
python setup_alerts.py --watchlist green
python setup_alerts.py --watchlist macro
```

Notes:

* The script uses the `TEMPLATE_NAME` environment variable to find your saved
  template.  Make sure it matches exactly.
* Set `SNIPER_MODE=true` in `.env` before running if you want 5‑minute alerts.

## 5. Using the system

* **Continuous feed:** Once alerts are configured, TradingView will send
  webhook calls to `/tv` on each bar close for every ticker/timeframe.  The
  relay augments the payload with the snapshot URL and forwards it to
  ChatGPT/Discord.
* **On‑demand scan:** POST to `/scan-now` on the relay to trigger a full sweep.
  You can wire this endpoint to the ChatGPT command `WWASD` so that ChatGPT
  calls it and waits for the resulting flood of alerts.

## 6. Troubleshooting

* **Alerts not firing:** Verify that the WWASD indicator is active and that
  alerts are configured for each timeframe.  You can open the alerts panel in
  TradingView to confirm.
* **No snapshot URL:** Ensure the “Include chart snapshot” checkbox is enabled
  when creating alerts.  Without it, TradingView will not send images.
* **Relaying errors:** Check the relay logs.  Errors forwarding to ChatGPT
  usually indicate a misconfigured `CHATGPT_WEBHOOK_URL` or network issues.

## 7. Security

* Do not commit `.env` with real credentials.  Use `.env.template` as a guide.
* Restrict access to the relay (e.g., behind a firewall or with API tokens).  The
  sample code is provided for demonstration and is not hardened for public use.

## 8. Future Enhancements

* Automate handling of account login challenges (e.g., two‑factor auth) in
  Playwright.
* Persist the list of configured alerts and de‑duplicate when re‑running the
  setup script.
* Extend the relay to cache the last known state for each ticker/timeframe and
  filter duplicates.