"""
WWASD TradingView Alert Setup (with DEBUG prints)

Automates creation of TradingView alerts for your "green" and "macro" watchlists.
- Uses Playwright to control TradingView, applies your template, adds WWASD_State_Emitter.
- Creates alerts for 1D/4H/1H and 5M when SNIPER_MODE=true (otherwise 15M).
- Auto‑tokenizes the relay webhook with AUTH_SHARED_SECRET so /tv accepts posts.

Usage:
    python setup_alerts.py --watchlist green
    python setup_alerts.py --watchlist macro

Environment (set before running):
    RELAY_WEBHOOK_URL   -> e.g. https://wwasd-relay.onrender.com/tv   (no token needed; we append it)
    AUTH_SHARED_SECRET  -> same value you have on Render (or AUTL_SHARED_SECRET)
    TEMPLATE_NAME       -> your saved TV template name (e.g. "WWASD_State_Emitter")
    SNIPER_MODE         -> "true" to use 5M instead of 15M
    HEADFUL             -> "true" to see the browser; omit/false for headless
    TV_SESSION_COOKIE   -> (recommended) a valid TradingView session cookie
    TV_USERNAME/TV_PASSWORD -> fallback only if you don’t supply TV_SESSION_COOKIE
"""

import argparse
import asyncio
import os
from typing import List
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

print("DEBUG: Script started")

TIMEFRAMES = {"1D": "D", "4H": "240", "1H": "60", "15M": "15", "5M": "5"}

# ---------------- small helpers ----------------
async def safe_click(page, selector: str, label: str, timeout: int = 15000) -> bool:
    try:
        await page.wait_for_selector(selector, timeout=timeout)
        await page.click(selector)
        print(f"DEBUG: clicked -> {label}")
        return True
    except PWTimeout:
        print(f"DEBUG: TIMEOUT waiting to click -> {label} ({selector})")
        return False
    except Exception as e:
        print(f"DEBUG: ERROR clicking {label}: {e}")
        return False

async def safe_fill(page, selector: str, value: str, label: str, timeout: int = 15000) -> bool:
    try:
        await page.wait_for_selector(selector, timeout=timeout)
        await page.fill(selector, value)
        print(f"DEBUG: filled -> {label}")
        return True
    except PWTimeout:
        print(f"DEBUG: TIMEOUT waiting to fill -> {label} ({selector})")
        return False
    except Exception as e:
        print(f"DEBUG: ERROR filling {label}: {e}")
        return False

# ---------------- core steps ----------------
async def login(page) -> None:
    """
    Login via session cookie (preferred) or TV_USERNAME/TV_PASSWORD (fallback).
    """
    cookie = os.environ.get("TV_SESSION_COOKIE", "")  # <— no more hard‑coded cookie
    user   = os.environ.get("TV_USERNAME")
    pwd    = os.environ.get("TV_PASSWORD")

    if cookie:
        await page.context.add_cookies([
            {"name":"sessionid","value":cookie,"domain":".tradingview.com","path":"/","httpOnly":True,"secure":True},
            {"name":"auth_id",  "value":cookie,"domain":".tradingview.com","path":"/","httpOnly":True,"secure":True},
        ])
        print("DEBUG: sessionid/auth_id cookies set from env")

    await page.goto("https://www.tradingview.com/chart/")
    print("DEBUG: navigated to /chart/")

    if not cookie and user and pwd:
        print("DEBUG: attempting email login")
        await safe_click(page, "text=Sign in", "Sign in button")
        await safe_click(page, "text=Sign in with email", "Sign in with email")
        await safe_fill(page, "input[name='username']", user, "username")
        await safe_fill(page, "input[name='password']", pwd, "password")
        await safe_click(page, "button[type='submit']", "submit login")
        try:
            await page.wait_for_selector("div[class*='chart-container']", timeout=30000)
            print("DEBUG: login complete, chart container visible")
        except PWTimeout:
            print("DEBUG: login may have 2FA or changed UI; continuing anyway")

async def get_symbols_from_watchlist(page) -> List[str]:
    print("DEBUG: collecting symbols from watchlist")
    try:
        await page.wait_for_selector("[data-symbol]", timeout=10000)
        symbols = await page.evaluate("""
            () => Array.from(document.querySelectorAll('[data-symbol]'))
                        .map(el => el.getAttribute('data-symbol'))
                        .filter(Boolean)
        """)
        print(f"DEBUG: found {len(symbols)} symbols (data-symbol)")
        return symbols
    except Exception as e:
        print(f"DEBUG: ERROR reading watchlist: {e}")
        return []

async def apply_template_and_indicator(page, template_name: str) -> None:
    print(f"DEBUG: applying template '{template_name}' and WWASD emitter")
    await safe_click(page, "button[aria-label='Indicators']", "Indicators button")
    await safe_fill(page, "input[placeholder*='Search']", template_name, "indicator search")
    await asyncio.sleep(0.5)
    await safe_click(page, f"text={template_name}", f"template {template_name}")
    await safe_fill(page, "input[placeholder*='Search']", "WWASD_State_Emitter", "indicator search (WWASD)")
    await asyncio.sleep(0.5)
    await safe_click(page, "text=WWASD_State_Emitter", "WWASD_State_Emitter")
    await page.keyboard.press("Escape")
    print("DEBUG: template/indicator applied")

async def create_alert(page, webhook_url: str) -> None:
    print("DEBUG: creating alert")
    await safe_click(page, "button[aria-label='Alerts']", "Alerts button")
    await safe_click(page, "text=Create alert", "Create alert")
    # Once-per-bar-close if the dropdown exists (some Pine alerts hide it)
    await safe_click(page, "select[name='frequency']", "frequency dropdown")
    try:
        await page.select_option("select[name='frequency']", "once_per_bar_close")
        print("DEBUG: frequency set to once_per_bar_close")
    except Exception as e:
        print(f"DEBUG: frequency select skipped: {e}")
    await safe_fill(page, "input[name='webhook_url']", webhook_url, "webhook_url")
    try:
        await page.check("input[name='include_snapshot']")
        print("DEBUG: include_snapshot checked")
    except Exception:
        print("DEBUG: snapshot checkbox not found (UI variant)")
    await safe_click(page, "button:has-text('Create')", "Create (save alert)")
    await page.wait_for_timeout(1000)
    print("DEBUG: alert created")

def _tokenize(url: str, token: str) -> str:
    """
    Ensure ?token=<AUTH_SHARED_SECRET> is present on the webhook URL.
    """
    if not url or not token:
        return url
    u = urlparse(url)
    q = dict(parse_qsl(u.query))
    if "token" not in q:
        q["token"] = token
    return urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode(q), u.fragment))

async def process_symbol(page, symbol: str, relay_url: str, sniper: bool, template_name: str) -> None:
    print(f"DEBUG: processing symbol {symbol}")
    await page.goto(f"https://www.tradingview.com/chart/?symbol={symbol}")
    await page.wait_for_timeout(1000)
    await apply_template_and_indicator(page, template_name)
    # Replace 15M with 5M when sniper mode is enabled
    tfs = ["1D", "4H", "1H", "15M"]
    if sniper:
        tfs = ["1D", "4H", "1H", "5M"]
    for tf in tfs:
        print(f"DEBUG: switching timeframe -> {tf}")
        await safe_click(page, "button[data-name='timeframe']", "timeframe menu")
        await safe_click(page, f"text={tf}", f"timeframe {tf}")
        await page.wait_for_timeout(1200)
        await create_alert(page, relay_url)

async def main(watchlist_name: str) -> None:
    print(f"DEBUG: Entering main() with watchlist: {watchlist_name}")
    relay_url  = os.environ.get("RELAY_WEBHOOK_URL")
    secret     = os.environ.get("AUTH_SHARED_SECRET") or os.environ.get("AUTL_SHARED_SECRET")
    template   = os.environ.get("TEMPLATE_NAME")
    sniper     = os.environ.get("SNIPER_MODE", "false").lower() == "true"
    headful    = os.environ.get("HEADFUL", "false").lower() == "true"

    # Auto‑tokenize the webhook
    relay_url = _tokenize(relay_url, secret)
    print(f"DEBUG: relay_url={relay_url}")
    print(f"DEBUG: template_name={template}")
    print(f"DEBUG: sniper_mode={sniper}")
    print(f"DEBUG: headful={'true' if headful else 'false'}")

    if not relay_url or not template:
        print("DEBUG: Missing RELAY_WEBHOOK_URL or TEMPLATE_NAME. Exiting.")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headful)
        print("DEBUG: browser launched")
        context = await browser.new_context()
        page    = await context.new_page()
        await login(page)
        print("DEBUG: login() returned")
        print("DEBUG: IMPORTANT -> Make the target watchlist active in TV before running.")
        symbols = await get_symbols_from_watchlist(page)
        if not symbols:
            print("DEBUG: No symbols found in active watchlist. Exiting.")
            await browser.close()
            return
        for idx, symbol in enumerate(symbols, 1):
            print(f"DEBUG: [{idx}/{len(symbols)}]")
            try:
                await process_symbol(page, symbol, relay_url, sniper, template)
            except Exception as e:
                print(f"DEBUG: ERROR while processing {symbol}: {e}")
        await browser.close()
        print("DEBUG: done; browser closed")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Set up TradingView alerts for WWASD")
    parser.add_argument("--watchlist", choices=["green", "macro"], required=True,
                        help="Which watchlist to process (make it active in TV first)")
    args = parser.parse_args()
    asyncio.run(main(args.watchlist))
