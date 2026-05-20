#!/usr/bin/env python3
"""
Jericho Beach Wind Forecast Alert
Uses Playwright to bypass Cloudflare, extracts Model2 data for today + 2 days.
Waits for datepicker to change after clicking Next (proven to work).
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
PACIFIC = timezone(timedelta(hours=-7))


async def get_datepicker(page):
    """Get the current datepicker value."""
    return await page.evaluate("() => { const el = document.getElementById('datepicker'); return el ? el.value : null; }")


async def wait_for_datepicker_change(page, old_value, timeout=20):
    """Wait until datepicker value changes from old_value."""
    for i in range(timeout * 2):
        val = await get_datepicker(page)
        if val and val != old_value:
            print(f"  Datepicker changed: {old_value} -> {val} (after {(i+1)*0.5:.1f}s)", flush=True)
            return val
        await page.wait_for_timeout(500)
    print(f"  Datepicker did not change after {timeout}s", flush=True)
    return None


async def get_model2_values(page):
    """Extract Model2 values from Highcharts."""
    vals = await page.evaluate(r"""
        () => {
            try {
                if (typeof Highcharts === 'undefined' || !Highcharts.charts) return null;
                const out = [];
                for (const c of Highcharts.charts) {
                    if (!c || !c.series) continue;
                    for (const s of c.series) {
                        const nm = (s && s.name) ? String(s.name) : '';
                        if (/model\s*2/i.test(nm)) {
                            if (Array.isArray(s.yData)) {
                                for (const v of s.yData) {
                                    if (typeof v === 'number') out.push(v);
                                }
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


async def get_model1_values(page):
    """Extract Model1 values from Highcharts."""
    vals = await page.evaluate(r"""
        () => {
            try {
                if (typeof Highcharts === 'undefined' || !Highcharts.charts) return null;
                const out = [];
                for (const c of Highcharts.charts) {
                    if (!c || !c.series) continue;
                    for (const s of c.series) {
                        const nm = (s && s.name) ? String(s.name) : '';
                        if (/model\s*1/i.test(nm)) {
                            if (Array.isArray(s.yData)) {
                                for (const v of s.yData) {
                                    if (typeof v === 'number') out.push(v);
                                }
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


async def wait_for_chart_update(page, timeout=15):
    """Wait for Highcharts to finish updating after a date change."""
    # Wait for any AJAX requests to complete by checking if chart is not loading
    for i in range(timeout * 2):
        loading = await page.evaluate("""
            () => {
                try {
                    const charts = Highcharts.charts.filter(c => c);
                    if (!charts.length) return true;
                    // Check if any series is still loading
                    return charts[0].loadingShown || false;
                } catch(e) { return false; }
            }
        """)
        if not loading and i > 4:  # wait at least 2 seconds
            return True
        await page.wait_for_timeout(500)
    return True


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

        # Monitor network to confirm data requests
        data_requests = []
        def on_response(response):
            if "extractwinddata" in response.url:
                data_requests.append(response.url)
        page.on("response", on_response)

        print(f"Loading: {SITE_URL}", flush=True)
        await page.goto(SITE_URL, wait_until="commit", timeout=60000)
        await page.wait_for_timeout(10000)

        has_hc = await page.evaluate("() => typeof Highcharts !== 'undefined'")
        print(f"Highcharts available: {has_hc}", flush=True)
        if not has_hc:
            await page.wait_for_timeout(15000)
            has_hc = await page.evaluate("() => typeof Highcharts !== 'undefined'")
            if not has_hc:
                print("ERROR: Highcharts never loaded", flush=True)
                await browser.close()
                sys.exit(1)

        # Collect data for today + 2 days
        all_model2 = {}  # day_str -> values
        all_model1 = {}

        for day_offset in range(3):
            if day_offset > 0:
                # Click next day
                old_dp = await get_datepicker(page)
                print(f"\nClicking Next (day +{day_offset})...", flush=True)
                data_requests.clear()
                await page.click("#NextButton")

                # Wait for datepicker to change
                new_dp = await wait_for_datepicker_change(page, old_dp)
                if not new_dp:
                    print(f"  Datepicker stuck at {old_dp}, trying JS fallback...", flush=True)
                    await page.evaluate("ChangeDate(1)")
                    await page.wait_for_timeout(2000)
                    new_dp = await get_datepicker(page)
                    print(f"  Datepicker after JS: {new_dp}", flush=True)

                # Wait for the data request to fire and chart to update
                await page.wait_for_timeout(5000)
                await wait_for_chart_update(page)

                # Confirm data request fired
                if data_requests:
                    print(f"  Data request: {data_requests[-1]}", flush=True)
                else:
                    print(f"  WARNING: No extractwinddata request detected", flush=True)

            dp_value = await get_datepicker(page)
            m2 = await get_model2_values(page)
            m1 = await get_model1_values(page)
            print(f"\nDay: {dp_value}", flush=True)
            print(f"  Model2: {len(m2)} points, values: {m2}", flush=True)
            print(f"  Model1: {len(m1)} points, values: {m1}", flush=True)

            all_model2[dp_value or f"day{day_offset}"] = m2
            all_model1[dp_value or f"day{day_offset}"] = m1

        await browser.close()

    # Analyze results
    print(f"\n{'='*50}", flush=True)
    print("Summary:", flush=True)

    all_m2_flat = []
    all_m1_flat = []
    windy_days = []

    for day_str, vals in all_model2.items():
        if vals:
            day_max = max(vals)
            above = [v for v in vals if v > WIND_THRESHOLD]
            print(f"  {day_str} Model2: max={day_max}kt, {len(above)} readings >{WIND_THRESHOLD}kt", flush=True)
            all_m2_flat.extend(vals)
            if above:
                windy_days.append({"day": day_str, "model": "Model2", "peak": day_max, "count": len(above), "values": above})

    for day_str, vals in all_model1.items():
        if vals:
            day_max = max(vals)
            above = [v for v in vals if v > WIND_THRESHOLD]
            print(f"  {day_str} Model1: max={day_max}kt, {len(above)} readings >{WIND_THRESHOLD}kt", flush=True)
            all_m1_flat.extend(vals)
            if above:
                windy_days.append({"day": day_str, "model": "Model1", "peak": day_max, "count": len(above), "values": above})

    if windy_days:
        print(f"\nALERT TRIGGERED - {len(windy_days)} windy period(s) found!", flush=True)

        if NTFY_TOPIC:
            lines = ["Jericho Beach Wind Alert!", ""]
            for w in windy_days:
                lines.append(f"{w['day']} {w['model']}: peak {w['peak']:.0f}kt ({w['count']} readings >{WIND_THRESHOLD}kt)")
            lines.append("")
            lines.append("https://bigwavedave.ca/jerichobch.html?site=20")
            message = chr(10).join(lines)

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"https://ntfy.sh/{NTFY_TOPIC}",
                    content=message.encode("utf-8"),
                    headers={"Title": "Wind Alert - Jericho Beach", "Priority": "high", "Tags": "wind_face"},
                )
                print(f"Notification sent (status {resp.status_code})", flush=True)
            print("--- Notification ---", flush=True)
            print(message, flush=True)
            print("---", flush=True)
        else:
            print("WARNING: NTFY_TOPIC not set. Skipping notification.", flush=True)
    else:
        print(f"\nNo wind >{WIND_THRESHOLD}kt in forecast. All clear.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
