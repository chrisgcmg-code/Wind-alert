#!/usr/bin/env python3
"""
Jericho Beach Wind Alert - DIAGNOSTIC VERSION
Captures full ChangeDate function and monitors what happens when Next is clicked.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
from playwright.async_api import async_playwright

SITE_URL = "https://bigwavedave.ca/jerichobch.html?site=20"
WIND_THRESHOLD = 10
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")


async def get_model2_values(page):
    """Extract Model2 values using multiple methods."""
    vals = await page.evaluate(r"""
        () => {
            try {
                if (typeof Highcharts !== 'undefined' && Highcharts.charts) {
                    const out = [];
                    for (const c of Highcharts.charts) {
                        if (!c || !c.series) continue;
                        for (const s of c.series) {
                            const nm = (s && s.name) ? String(s.name) : '';
                            if (/model\s*2/i.test(nm)) {
                                if (Array.isArray(s.yData)) out.push(...s.yData);
                            }
                        }
                    }
                    if (out.length > 0) return out;
                }
            } catch(e) {}
            try {
                if (typeof model2 !== 'undefined' && Array.isArray(model2)) return model2;
            } catch(e) {}
            return null;
        }
    """)
    if vals:
        return [float(x) for x in vals if isinstance(x, (int, float))]
    return []


async def main():
    print("=" * 50, flush=True)
    print("DIAGNOSTIC RUN", flush=True)
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

        # Monitor all network requests
        requests_log = []
        page.on("request", lambda req: requests_log.append(req.url))

        print(f"Loading: {SITE_URL}", flush=True)
        await page.goto(SITE_URL, wait_until="commit", timeout=60000)
        await page.wait_for_timeout(10000)

        # 1) Print FULL ChangeDate function
        cd_full = await page.evaluate("() => typeof ChangeDate === 'function' ? ChangeDate.toString() : 'NOT FOUND'")
        print(f"\n--- FULL ChangeDate function ---", flush=True)
        print(cd_full, flush=True)
        print(f"--- END ---\n", flush=True)

        # 2) Print datepicker value
        dp = await page.evaluate("() => { const el = document.getElementById('datepicker'); return el ? el.value : 'NOT FOUND'; }")
        print(f"Datepicker value: {dp}", flush=True)

        # 3) Get today's Model2
        m2_today = await get_model2_values(page)
        print(f"Today Model2: {m2_today}", flush=True)

        # 4) Clear request log, then click Next and monitor
        requests_log.clear()
        print(f"\nClicking NextButton...", flush=True)

        # Click and wait for potential navigation
        try:
            await page.click("#NextButton")
        except Exception as e:
            print(f"Click error: {e}", flush=True)

        # Wait and observe
        await page.wait_for_timeout(8000)

        # 5) Check what happened
        new_url = page.url
        print(f"URL after click: {new_url}", flush=True)

        dp_after = await page.evaluate("() => { const el = document.getElementById('datepicker'); return el ? el.value : 'NOT FOUND'; }")
        print(f"Datepicker after click: {dp_after}", flush=True)

        m2_after = await get_model2_values(page)
        print(f"Model2 after click: {m2_after}", flush=True)

        # 6) Show network requests triggered by the click
        print(f"\nNetwork requests after click ({len(requests_log)}):", flush=True)
        for url in requests_log[:20]:
            print(f"  {url}", flush=True)
        if len(requests_log) > 20:
            print(f"  ... and {len(requests_log) - 20} more", flush=True)

        # 7) Check if page title changed
        title = await page.title()
        print(f"Page title: {title}", flush=True)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
