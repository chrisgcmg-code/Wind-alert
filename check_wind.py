#!/usr/bin/env python3
"""
Jericho Beach Wind Forecast Alert
Loads bigwavedave.ca once, then sets datepicker + calls getMet() for each day.
Waits for Cloudflare challenge on initial load only.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
from playwright.async_api import async_playwright

BASE_URL = "https://bigwavedave.ca/jerichobch.html?site=20"
WIND_THRESHOLD = 10
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
PACIFIC = timezone(timedelta(hours=-7))


async def wait_for_highcharts(page, timeout=30):
    """Wait for Highcharts to be available and have model data."""
    for i in range(timeout):
        ready = await page.evaluate(r"""
            () => {
                try {
                    if (typeof Highcharts === 'undefined') return false;
                    const charts = Highcharts.charts.filter(c => c);
                    if (!charts.length) return false;
                    for (const s of charts[0].series) {
                        if (/model/i.test(s.name) && s.yData && s.yData.some(v => typeof v === 'number')) return true;
                    }
                    return false;
                } catch(e) { return false; }
            }
        """)
        if ready:
            return True
        await page.wait_for_timeout(1000)
    return False


async def get_model_values(page):
    """Extract Model1 and Model2 values from Highcharts."""
    return await page.evaluate(r"""
        () => {
            try {
                if (typeof Highcharts === 'undefined' || !Highcharts.charts) return null;
                const result = {};
                for (const c of Highcharts.charts) {
                    if (!c || !c.series) continue;
                    for (const s of c.series) {
                        const nm = String(s.name || '');
                        if (/model/i.test(nm)) {
                            const ydata = s.yData || [];
                            const xdata = s.xData || [];
                            const points = [];
                            for (let i = 0; i < ydata.length; i++) {
                                if (typeof ydata[i] === 'number') {
                                    points.push({time: xdata[i], value: ydata[i]});
                                }
                            }
                            if (points.length > 0) {
                                result[nm] = points;
                            }
                        }
                    }
                }
                return Object.keys(result).length > 0 ? result : null;
            } catch(e) { return null; }
        }
    """)


async def main():
    print("=" * 50, flush=True)
    print("Jericho Beach Wind Alert Check", flush=True)
    print(f"Alert if any Model2 forecast >{WIND_THRESHOLD} knots", flush=True)
    print("=" * 50, flush=True)

    now_pacific = datetime.now(PACIFIC)
    days = [(now_pacific + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)]
    print(f"Checking days: {days}", flush=True)

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

        # Load the page once
        print(f"\nLoading: {BASE_URL}", flush=True)
        await page.goto(BASE_URL, wait_until="commit", timeout=60000)
        print("  Waiting for page to settle...", flush=True)
        await page.wait_for_timeout(12000)

        has_hc = await page.evaluate("() => typeof Highcharts !== 'undefined'")
        print(f"  Highcharts available: {has_hc}", flush=True)
        if not has_hc:
            print("  Waiting additional 15s...", flush=True)
            await page.wait_for_timeout(15000)
            has_hc = await page.evaluate("() => typeof Highcharts !== 'undefined'")
            print(f"  Highcharts available (2nd check): {has_hc}", flush=True)
            if not has_hc:
                print("ERROR: Highcharts never loaded", flush=True)
                await browser.close()
                sys.exit(1)

        has_model = await wait_for_highcharts(page)
        print(f"  Model data ready: {has_model}", flush=True)

        all_data = {}

        for day_str in days:
            print(f"\n--- {day_str} ---", flush=True)

            # Set datepicker and call getMet to load the day's data
            current_dp = await page.evaluate("() => document.getElementById('datepicker')?.value")
            if current_dp != day_str:
                print(f"  Setting datepicker from {current_dp} to {day_str}...", flush=True)
                await page.evaluate(f"document.getElementById('datepicker').value = '{day_str}'")
                await page.evaluate("getMet(0)")
                # Wait for chart to update with new data
                await page.wait_for_timeout(8000)
            else:
                print(f"  Already on {day_str}", flush=True)

            dp_now = await page.evaluate("() => document.getElementById('datepicker')?.value")
            print(f"  Datepicker: {dp_now}", flush=True)

            data = await get_model_values(page)
            if data:
                all_data[day_str] = data
                for model_name, points in data.items():
                    vals = [pt["value"] for pt in points]
                    print(f"  {model_name}: {len(vals)} points, values: {vals}", flush=True)
            else:
                print(f"  No model data", flush=True)

        await browser.close()

    # Analyze
    print(f"\n{'='*50}", flush=True)
    print("Summary:", flush=True)

    windy_alerts = []
    for day_str, models in all_data.items():
        for model_name, points in models.items():
            vals = [pt["value"] for pt in points]
            if not vals:
                continue
            day_max = max(vals)
            above = [v for v in vals if v > WIND_THRESHOLD]
            print(f"  {day_str} {model_name}: max={day_max}kt, {len(above)} readings >{WIND_THRESHOLD}kt", flush=True)
            if above:
                windy_alerts.append({
                    "day": day_str,
                    "model": model_name,
                    "peak": day_max,
                    "count": len(above),
                    "times": [pt for pt in points if pt["value"] > WIND_THRESHOLD]
                })

    if windy_alerts:
        print(f"\nALERT TRIGGERED - {len(windy_alerts)} windy period(s)!", flush=True)

        lines = ["Jericho Beach Wind Alert!", ""]
        for w in windy_alerts:
            lines.append(f"{w['day']} {w['model']}: peak {w['peak']:.0f}kt ({w['count']} readings >{WIND_THRESHOLD}kt)")
            top = sorted(w["times"], key=lambda x: x["value"], reverse=True)[:3]
            for t in top:
                if t.get("time"):
                    pt_time = datetime.fromtimestamp(t["time"] / 1000, tz=PACIFIC)
                    lines.append(f"  {pt_time.strftime('%a %-I%p')}: {t['value']:.0f}kt")
        lines.append("")
        lines.append("https://bigwavedave.ca/jerichobch.html?site=20")
        message = chr(10).join(lines)

        if NTFY_TOPIC:
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
            print("WARNING: NTFY_TOPIC not set.", flush=True)
    else:
        print(f"\nNo wind >{WIND_THRESHOLD}kt in forecast. All clear.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
