# WWASD Relay v2 (FastAPI)

Drop‑in relay for TradingView alerts. Accepts **JSON** from Pine (`Any alert() function call`) and **multipart** uploads from automation (screenshots). Caches the latest `WWASD_STATE` per symbol and exposes `/tv/latest` for the chat agent.

## Endpoints
- `POST /tv` — ingest alerts (JSON or multipart). If `type == "WWASD_STATE"`, it is stored by symbol.
- `GET /tv/latest?list=green&max_age_secs=5400` — newest state per symbol. Adds `is_fresh` based on `FRESH_CUTOFF_SECS`.
- `GET /health` — basic status.

## Env vars
- `SECRET_TOKEN` (optional) — require `?token=...` or `"token"` in body.
- `GREEN_LIST`, `MACRO_LIST`, `FULL_LIST` — comma‑separated symbols.
- `REDIS_URL` (optional) — persistence.
- `FRESH_CUTOFF_SECS` — default 5400 (90 min).

## Deploy on Render
Upload this repo, set env vars, build: `pip install -r requirements.txt`, start: `gunicorn -w 2 -k uvicorn.workers.UvicornWorker app:app`.
