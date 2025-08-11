"""
WWASD TradingView Alert Setup (with DEBUG prints)

This script automates the creation of TradingView alerts for each symbol in your
"green" and "macro" watchlists. It uses Playwright to control the TradingView UI,
applies a saved indicator template, adds the WWASD state emitter, and creates
alerts for 1D, 4H, 1H, 15M (and optionally 5M) timeframes. It logs progress
messages beginning with "DEBUG:" so you can see what it’s doing.

Usage:
    python setup_alerts.py --watchlist green
    python setup_alerts.py --watchlist macro

Environment variables (set these in PowerShell before running):
    RELAY_WEBHOOK_URL   -> URL of your relay (e.g. https://xxxx.ngrok-free.app/tv)
    TEMPLATE_NAME       -> Name of your saved indicator template (e.g. "WWASD_State_Emitter")
    SNIPER_MODE         -> "true" to include 5M alerts; otherwise omit or "false"
    HEADFUL             -> "true" to show the browser UI; omit or "false" for headless

The script below hard‑codes your TradingView session cookie. If you prefer to
keep it outside of the code, set TV_SESSION_COOKIE in your environment instead
and delete the hard‑coded value.
"""

import argparse
import asyncio
import os
from typing import List

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ---- DEBUG banner ------------------------------------------------------------
print("DEBUG: Script started")

# TF map used when we need the code version
TIMEFRAMES = {"1D": "D", "4H": "240", "1H": "60", "15M": "15", "5M": "5"}


# ---- small helpers -----------------------------------------------------------
async def safe_click(page, selector: str, label: str, timeout: int = 15000) -> bool:
    """Attempt to click a selector, logging timeout or errors."""
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
    """Attempt to fill a selector, logging timeout or errors."""
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


# ---- core steps --------------------------------------------------------------
async def login(page) -> None:
    """
    Log in to TradingView using a session cookie or username/password.

    This version hard‑codes your session cookie. If you prefer environment
    variables, set TV_SESSION_COOKIE and remove the hard‑coded value here.
    """
    # Hard‑coded cookie value (replace with a fresh one as needed)
    cookie = "tq3t9ctrohl0sg8x55ch97y6uc1l0rve"
    # Credentials as fallback (only used if cookie is empty)
    user = os.environ.get("TV_USERNAME")
    pwd  = os.environ.get("TV_PASSWORD")

    if cookie:
        # Set both possible cookie names before navigating
        await page.context.add_cookies([
            {
                "name": "sessionid",
                "value": cookie,
                "domain": ".tradingview.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
            },
            {
                "name": "auth_id",
                "value": cookie,
                "domain": ".tradingview.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
            },
        ])
        print("DEBUG: sessionid/auth_id cookies set")

    # Navigate to the chart page
    await page.goto("https://www.tradingview.com/chart/")
    print("DEBUG: navigated to /chart/")

    # If no cookie and username/password are provided, attempt an email login
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
    """
    Return a list of symbols from the active watchlist.

    This function scans for any element with a `data-symbol` attribute, which
    matches the current TradingView watchlist DOM. It waits until at least one
    such element is present before extracting values.
    """
    print("DEBUG: collecting symbols from watchlist")
    try:
        # Wait for at least one symbol to appear
        await page.wait_for_selector("[data-symbol]", timeout=10000)
        symbols = await page.evaluate(
            """
            () => {
                const els = document.querySelectorAll('[data-symbol]');
                return Array.from(els)
                    .map(el => el.getAttribute('data-symbol'))
                    .filter(Boolean);
            }
            """
        )
        print(f"DEBUG: found {len(symbols)} symbols (data-symbol)")
        return symbols
    except Exception as e:
        print(f"DEBUG: ERROR reading watchlist: {e}")
        return []


async def apply_template_and_indicator(page, template_name: str) -> None:
    """Apply the saved indicator template and add the WWASD emitter."""
    print(f"DEBUG: applying template '{template_name}' and WWASD emitter")
    # Open Indicators panel
    await safe_click(page, "button[aria-label='Indicators']", "Indicators button")
    # Search and load the template
    await safe_fill(page, "input[placeholder*='Search']", template_name, "indicator search")
    await asyncio.sleep(0.5)
    await safe_click(page, f"text={template_name}", f"template {template_name}")
    # Add the WWASD_State_Emitter indicator
    await safe_fill(page, "input[placeholder*='Search']", "WWASD_State_Emitter", "indicator search (WWASD)")
    await asyncio.sleep(0.5)
    await safe_click(page, "text=WWASD_State_Emitter", "WWASD_State_Emitter")
    # Close the indicators panel
    await page.keyboard.press("Escape")
    print("DEBUG: template/indicator applied")


async def create_alert(page, webhook_url: str) -> None:
    """Create an alert on the current chart with snapshot and webhook."""
    print("DEBUG: creating alert")
    await safe_click(page, "button[aria-label='Alerts']", "Alerts button")
    await safe_click(page, "text=Create alert", "Create alert")
    # Frequency: once per bar close
    await safe_click(page, "select[name='frequency']", "frequency dropdown")
    try:
        await page.select_option("select[name='frequency']", "once_per_bar_close")
        print("DEBUG: frequency set to once_per_bar_close")
    except Exception as e:
        print(f"DEBUG: frequency select failed (may not be present with Pine alert()): {e}")
    # Webhook URL + snapshot
    await safe_fill(page, "input[name='webhook_url']", webhook_url, "webhook_url")
    try:
        await page.check("input[name='include_snapshot']")
        print("DEBUG: include_snapshot checked")
    except Exception:
        print("DEBUG: snapshot checkbox not found (UI variant)")
    # Save alert
    await safe_click(page, "button:has-text('Create')", "Create (save alert)")
    await page.wait_for_timeout(1000)
    print("DEBUG: alert created")


async def process_symbol(page, symbol: str, relay_url: str, sniper: bool, template_name: str) -> None:
    """
    For a given symbol, iterate through timeframes and configure alerts.
    """
    print(f"DEBUG: processing symbol {symbol}")
    await page.goto(f"https://www.tradingview.com/chart/?symbol={symbol}")
    await page.wait_for_timeout(1000)
    await apply_template_and_indicator(page, template_name)
    tfs = ["1D", "4H", "1H", "15M"]
    if sniper:
        tfs.append("5M")
    for tf in tfs:
        print(f"DEBUG: switching timeframe -> {tf}")
        await safe_click(page, "button[data-name='timeframe']", "timeframe menu")
        await safe_click(page, f"text={tf}", f"timeframe {tf}")
        await page.wait_for_timeout(1200)
        await create_alert(page, relay_url)


async def main(watchlist_name: str) -> None:
    """Main entry point: set up alerts for the specified watchlist."""
    print(f"DEBUG: Entering main() with watchlist: {watchlist_name}")
    relay_url    = os.environ.get("RELAY_WEBHOOK_URL")
    template_name = os.environ.get("TEMPLATE_NAME")
    sniper_mode  = os.environ.get("SNIPER_MODE", "false").lower() == "true"
    headful      = os.environ.get("HEADFUL", "false").lower() == "true"
    print(f"DEBUG: relay_url={relay_url}")
    print(f"DEBUG: template_name={template_name}")
    print(f"DEBUG: sniper_mode={sniper_mode}")
    print(f"DEBUG: headful={'true' if headful else 'false'}")
    if not relay_url or not template_name:
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
                await process_symbol(page, symbol, relay_url, sniper_mode, template_name)
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
