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
- TV_LATEST_CACHE_PATH = /tmp/tv_latest.json
- BLOFIN_CACHE_PATH = /tmp/blofin_latest.json
- SNAP_CACHE_PATH = /tmp/snap_cache.json

## Deploy on Render
Upload this repo, set env vars, build: `pip install -r requirements.txt`, start: `gunicorn -w 2 -k uvicorn.workers.UvicornWorker app:app`.
v3
WWASD Relay — Ops README

Purpose: serve clean, low‑friction feeds from TradingView alerts and Blofin positions to TV Desk & Port Desk. Everything here is append‑only and compatible with your existing consumers.

TL;DR (do these first)

Windows Task Scheduler (local push every 2 minutes):

Program/script: cscript.exe

Add arguments: //B //Nologo "C:\Users\brand\OneDrive\Attachments\Desktop\wwasd_bridge_final\wwasd_bridge_final\silent_push_blofin.vbs"

Start in: C:\Users\brand\OneDrive\Attachments\Desktop\wwasd_bridge_final\wwasd_bridge_final

Trigger: Repeat task every 2 minutes (indefinitely).

Confirm the last write time in watchdog.log updates every 2–3 minutes.

Health checks (Render):

/health → 200 + JSON

/blofin/latest → shows fresh: true with a current timestamp

/snap_table.html?fresh_only=1&lists=green,full,macro → shows live rows for TV Desk

/snap.csv?fresh_only=1&lists=green,full,macro → same data in CSV for sandboxes

Endpoints (served by app.py)

System

GET /health — quick ping; confirms the process is alive and can read caches.

TradingView state snapshots

GET /snap_raw.html?fresh_only=1&lists=green,full,macro — SSR view you already use.

GET /snap.csv?fresh_only=1&lists=… — flat CSV (works in restricted sandboxes).

GET /snap_table.html?fresh_only=1&lists=… — simple HTML table (no JS).

Blofin

GET /blofin/latest — last pushed positions as JSON (fresh, ts, positions[…]).

Port Desk

GET /port2_ssr.html — SSR portfolio view (reads the same caches).

Freshness semantics: a feed is fresh if the latest cache file timestamp is within a short grace window (target ≈ ≤ 5 min). If your scheduler is every 2 min, seeing fresh: false usually means the pusher didn’t run recently or the Render instance restarted and has no recent file in /tmp.

How “freshness” works (and why it sometimes goes “STALE”)

Render Free dyno sleeps/rotates. Memory resets on cold start. If your app only holds in‑memory state, SSR pages look “stale.”

Your fix: the pusher writes files that the app reads:

Blofin → writes to the path in BLOFIN_CACHE_PATH (/tmp/blofin_latest.json).

TV latest (optional consolidation) → TV_LATEST_CACHE_PATH.

SSR/CSV read those files each request. No in‑memory dependency → no “cold instance” loss.

Windows Task Scheduler — correct setup

Trigger

Begin the task: On a schedule

Daily; Repeat task every 2 minutes; for a duration of: Indefinitely

Enabled; Run whether user is logged on or not.

Action

Program/script: cscript.exe

Add arguments:
//B //Nologo "C:\Users\brand\OneDrive\Attachments\Desktop\wwasd_bridge_final\wwasd_bridge_final\silent_push_blofin.vbs"

Start in:
C:\Users\brand\OneDrive\Attachments\Desktop\wwasd_bridge_final\wwasd_bridge_final

Notes

//B and //Nologo suppress windows; the quoted path points to your VBS wrapper, which runs push_positions.cmd, which calls blofin_positions_pusher.py.

Verify watchdog.log (same folder) time‑stamps advance every 2–3 minutes.

TV Desk & Port Desk—what to pull

TV Desk: use HTML table or CSV in restricted sandboxes:

/snap_table.html?fresh_only=1&lists=green,full,macro

/snap.csv?fresh_only=1&lists=green,full,macro

Port Desk: SSR page:

/port2_ssr.html

If a sandbox says it “can hit the URL but can’t read the body”:

Prefer the CSV endpoint above (parses almost everywhere).

TradingView indicator wiring (so the feed contains the new fields)

One script instance only: All active alerts must reference the same saved WWASD_State_Emitter (not a “Save As…” clone).

Your v.88 emits:

rsi block (with rsi_50_up/down, divergences),

ctx (Monday range mon_state),

trend (macro from OTHERS.D vs STABLE.C.D),

htf {sig, rating}, and ltf {sig, rating} (5m/15m/60m voting).

If you don’t see the fields: open Manage alerts → check Condition → study name. It must be the same script file you edited (no duplicates). Edit & Save the alert once to bind to the updated compiled binary (don’t recreate all 200).

Macro logic (cheat sheet, for clarity):

OTHERS.D ↑ and STABLE.C.D ↓ → risk‑on (BUY bias).

OTHERS.D ↓ and STABLE.C.D ↑ → risk‑off (SELL bias). 

Monday bias (why mon_state exists): reclaiming back inside Monday’s range often flips the weekly bias; deviations above/below Monday then reclaiming can set directional trades toward the opposite side of the range. That’s what Port/TV use in context tags. 

Common “STALE” causes & quick fixes

Scheduler didn’t run (PC asleep, user logged out without “Run whether user is logged on or not,” or Task disabled).

Fix: Re‑enable task; confirm “Last Run Result = The operation completed successfully.”

Wrong working dir (no “Start in” → scripts can’t find .env/files).

Fix: set Start in exactly as above.

Render restarted (free plan idle rotation). No cache file yet.

Fix: wait one pusher cycle (2–3 minutes) or manually run silent_push_blofin.vbs.

Paths aren’t set in Render.

Fix: add BLOFIN_CACHE_PATH & TV_LATEST_CACHE_PATH, redeploy.

File map (what’s doing what)

app.py — Flask (or similar) app serving /health, /blofin/latest, **/snap_*/*SSR pages, reading JSON caches in /tmp.

blofin_positions_pusher.py — calls Blofin API and writes BLOFIN_CACHE_PATH JSON.

silent_push_blofin.vbs → push_positions.cmd → Python pusher (headless, silent).

watchdog*.cmd/.ps1 — optional keep‑alive wrappers & logging.

“This looks right but Port Desk says they can’t pull”

Many ChatGPT tool sandboxes can hit URLs but block JSON/JS. That’s why you added CSV + HTML table endpoints—ask them to use those exact endpoints.

If they still see “no open positions,” check the fresh flag at /blofin/latest. If false, it’s a local push problem (see Scheduler section).

Appendix — trading semantics (for your assistants)

Macro: we score risk using dominance flows: OTHERS vs STABLE (USDT). OTHERS up / STABLE down → alt risk‑on. The inverse → risk‑off.

Bias (Monday): deviation + reclaim of Monday’s high/low sets a directional “draw on liquidity” toward the opposite side; this powers the mon_state you broadcast. 

Support playbook (what to send me if something breaks)

A screenshot of Task Scheduler → Last Run Result and the Action dialog (Program/Arguments/Start in).

The last 20 lines of watchdog.log.

The JSON from /blofin/latest.

The first 20 rows from /snap.csv?fresh_only=1&lists=green,full,macro.

The exact Condition → study name from one active TradingView alert that just fired.
