#!/usr/bin/env python3
"""
Jericho Beach Wind Forecast Alert
Uses Playwright to load the page, then intercepts extractwinddata.php responses
directly when clicking Next Day. No reliance on Highcharts timing.
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import httpx
from playwright.async_api import async_playwright

SITE_URL = "https://bigwavedave.ca/jerichobch.html?site=20"
WIND_THRESHOLD = 10
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
PACIFIC = timezone(timedelta(hours=-7))


async def get_datepicker(page):
    """Get the current datepicker value."""
    return await page.evaluate("() => { const el = document.getElementById('datepicker'); return el ? el.value : null; }")


async def get_model2_from_highcharts(page):
    """Extract Model2 values directly from Highcharts (with fresh reference)."""
    vals = await page.evaluate(r"""
        () => {
            try {
                if (typeof Highcharts === 'undefined' || !Highcharts.charts) return null;
                // Get a fresh reference to all current charts
                const charts = Highcharts.charts.filter(c => c);
                const out = [];
                for (const c of charts) {
                    if (!c.series) continue;
                    for (const s of c.series) {
                        const nm = String(s.name || '');
                        if (/model\s*2/i.test(nm)) {
                            // Try processedYData first (post-render), then yData
                            const ydata = s.processedYData || s.yData || [];
                            for (const v of ydata) {
                                if (typeof v === 'number') out.push(v);
                            }
                        }
                    }
                }
                return out.length > 0 ? out : null;
            } catch(e) { return null; }
        }
    """)
    if vals:
        return [float(x) for x in vals]
    return []


async def main():
    print("=" * 50, flush=True)
    print("Jericho Beach Wind Alert Check", flush=True)
    print(f"Alert if any Model2 forecast >{WIND_THRESHOLD} knots", flush=True)
    print("=" * 50, flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled', '--disable-dev-shm-usage']
        )
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            viewport={'width': 1920, 'height': 1080},
        )
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => false });")

        # Capture API responses
        captured_responses = {}

        async def handle_response(response):
            if "extractwinddata" in response.url:
                try:
                    body = await response.text()
                    captured_responses[response.url] = body
                    print(f"  [CAPTURED] {response.url} ({len(body)} chars)", flush=True)
                except Exception:
                    pass

        page.on("response", handle_response)

        print(f"Loading: {SITE_URL}", flush=True)
        await page.goto(SITE_URL, wait_until="commit", timeout=60000)
        await page.wait_for_timeout(12000)

        has_hc = await page.evaluate("() => typeof Highcharts !== 'undefined'")
        print(f"Highcharts available: {has_hc}", flush=True)
        if not has_hc:
            await page.wait_for_timeout(15000)
            has_hc = await page.evaluate("() => typeof Highcharts !== 'undefined'")
            if not has_hc:
                print("ERROR: Highcharts never loaded", flush=True)
                await browser.close()
                sys.exit(1)

        # Print initial captured response (from page load)
        dp_today = await get_datepicker(page)
        print(f"\nToday: {dp_today}", flush=True)

        if captured_responses:
            print(f"Initial API responses captured: {len(captured_responses)}", flush=True)
            for url, body in captured_responses.items():
                print(f"  URL: {url}", flush=True)
                print(f"  Body preview: {body[:800]}", flush=True)
                print(flush=True)

        # Also read from Highcharts for comparison
        m2_hc = await get_model2_from_highcharts(page)
        print(f"Highcharts Model2: {m2_hc}", flush=True)

        # Click next for day+1 and day+2
        for day_offset in range(1, 3):
            old_dp = await get_datepicker(page)
            captured_responses.clear()

            print(f"\n--- Clicking Next (day +{day_offset}) ---", flush=True)
            await page.click("#NextButton")

            # Wait for datepicker to change
            for i in range(40):
                new_dp = await get_datepicker(page)
                if new_dp and new_dp != old_dp:
                    print(f"  Datepicker: {old_dp} -> {new_dp} ({(i+1)*0.5:.1f}s)", flush=True)
                    break
                await page.wait_for_timeout(500)

            # Wait for API response to be captured
            for i in range(20):
                if captured_responses:
                    break
                await page.wait_for_timeout(500)

            # Wait extra time for chart to fully re-render
            await page.wait_for_timeout(5000)

            dp_now = await get_datepicker(page)
            print(f"  Day: {dp_now}", flush=True)

            if captured_responses:
                for url, body in captured_responses.items():
                    print(f"  API response: {body[:800]}", flush=True)
            else:
                print(f"  WARNING: No API response captured", flush=True)

            # Read from Highcharts too
            m2_hc = await get_model2_from_highcharts(page)
            print(f"  Highcharts Model2: {m2_hc}", flush=True)

        await browser.close()

    # Parse all captured API responses to find Model2 data
    print(f"\n{'='*50}", flush=True)
    print("Parsing all captured API responses...", flush=True)

    all_model2 = {}
    for url, body in captured_responses.items():
        # Try JSON parse
        try:
            data = json.loads(body)
            print(f"  JSON keys: {list(data.keys()) if isinstance(data, dict) else type(data)}", flush=True)
        except json.JSONDecodeError:
            # Try to find model2 data via regex
            m = re.search(r'model2\s*[=:]\s*\[([^\]]+)\]', body, re.IGNORECASE)
            if m:
                nums = re.findall(r'-?\d+(?:\.\d+)?', m.group(1))
                print(f"  Regex found model2: {nums[:20]}", flush=True)

    print("\nDone. Check output above to determine response format.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
