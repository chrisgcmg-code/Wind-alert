#!/usr/bin/env python3
"""
Jericho Beach Wind Forecast Alert
Scrapes bigwavedave.ca Highcharts data for Jericho Beach (site=20),
checks if Model1 or Model2 forecast wind >10 knots for >2 consecutive hours
in the next 48 hours, and sends a push notification via ntfy.sh.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
from playwright.async_api import async_playwright

SITE_URL = "https://bigwavedave.ca/jerichobch.html?site=20"
WIND_THRESHOLD = 10
MIN_HOURS = 3
FORECAST_WINDOW_HRS = 48
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")


async def wait_for_highcharts(page, timeout=45):
    """Poll until Highcharts has rendered at least one chart with data."""
    for i in range(timeout):
        ready = await page.evaluate("""
            () => {
                try {
                    if (typeof Highcharts === 'undefined') return false;
                    const charts = Highcharts.charts.filter(c => c);
                    if (!charts.length) return false;
                    const chart = charts[0];
                    return chart.series && chart.series.length > 0 && chart.series[0].xData && chart.series[0].xData.length > 0;
                } catch(e) {
                    return false;
                }
            }
        """)
        if ready:
            print(f"  Highcharts ready after {i+1}s", flush=True)
            return True
        await page.wait_for_timeout(1000)
    print(f"  Highcharts not ready after {timeout}s", flush=True)
    return False


async def extract_chart_data(page):
    """Pull all series data out of the first Highcharts chart on the page."""
    return await page.evaluate("""
        () => {
            try {
                if (typeof Highcharts === 'undefined') return null;
                const charts = Highcharts.charts.filter(c => c);
                if (!charts.length) return null;
                const chart = charts[0];
                return chart.series.map(s => ({
                    name: s.name,
                    data: s.xData.map((x, i) => [x, s.yData[i]])
                }));
            } catch(e) {
                return null;
            }
        }
    """)


async def get_wind_data():
    """Load the BigWaveDave page and extract Model1/Model2 forecast data."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
            ]
        )
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            viewport={'width': 1920, 'height': 1080},
            java_script_enabled=True,
        )
        page = await context.new_page()

        # Hide webdriver detection
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
        """)

        all_series = {}

        print(f"Loading: {SITE_URL}", flush=True)
        try:
            await page.goto(SITE_URL, wait_until="commit", timeout=60000)
            print("  Page navigation started...", flush=True)
        except Exception as e:
            print(f"  Navigation error: {e}", flush=True)
            await browser.close()
            return []

        # Wait for the page to fully load
        print("  Waiting for page to settle...", flush=True)
        await page.wait_for_timeout(10000)

        # Check what scripts are loaded
        has_highcharts = await page.evaluate("() => typeof Highcharts !== 'undefined'")
        print(f"  Highcharts available: {has_highcharts}", flush=True)

        if not has_highcharts:
            # Try waiting more
            print("  Waiting additional 15s for scripts...", flush=True)
            await page.wait_for_timeout(15000)
            has_highcharts = await page.evaluate("() => typeof Highcharts !== 'undefined'")
            print(f"  Highcharts available (2nd check): {has_highcharts}", flush=True)

        if not has_highcharts:
            # Debug: check page title and content length
            title = await page.title()
            content_len = await page.evaluate("() => document.body.innerText.length")
            url = page.url
            print(f"  Page title: {title}", flush=True)
            print(f"  Page content length: {content_len} chars", flush=True)
            print(f"  Current URL: {url}", flush=True)
            await browser.close()
            return []

        if not await wait_for_highcharts(page):
            await browser.close()
            return []

        today_data = await extract_chart_data(page)

        if today_data:
            for s in today_data:
                name = s["name"]
                if name not in all_series:
                    all_series[name] = {}
                for x, y in s["data"]:
                    if x is not None and y is not None:
                        all_series[name][x] = y
            print(f"  Loaded today: {len(today_data)} series", flush=True)
        else:
            print("  WARNING: No data extracted for today", flush=True)

        # Click ">>" twice to get tomorrow and day-after-tomorrow
        for day in range(1, 3):
            try:
                next_btn = page.locator("text=>>").first
                if await next_btn.is_visible(timeout=5000):
                    await next_btn.click()
                    await page.wait_for_timeout(3000)
                    if await wait_for_highcharts(page):
                        day_data = await extract_chart_data(page)
                        if day_data:
                            for s in day_data:
                                name = s["name"]
                                if name not in all_series:
                                    all_series[name] = {}
                                for x, y in s["data"]:
                                    if x is not None and y is not None:
                                        all_series[name][x] = y
                        print(f"  Loaded day +{day}", flush=True)
                    else:
                        print(f"  WARNING: Chart not ready for day +{day}", flush=True)
                else:
                    print(f"  WARNING: Next button not visible for day +{day}", flush=True)
            except Exception as e:
                print(f"  Could not load day +{day}: {e}", flush=True)

        await browser.close()

    result = []
    for name, points in all_series.items():
        sorted_points = sorted(points.items())
        result.append({
            "name": name,
            "data": [[ts, val] for ts, val in sorted_points]
        })
    return result


def check_for_alerts(series_list):
    """Check each model series for sustained wind above threshold."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=FORECAST_WINDOW_HRS)
    alerts = []

    for series in series_list:
        name = series["name"]
        name_lower = name.lower()
        if "model" not in name_lower:
            continue

        data = series["data"]
        streak = 0
        streak_start = None
        peak_val = 0

        for ts_ms, val in data:
            if ts_ms is None or val is None:
                if streak >= MIN_HOURS:
                    alerts.append({"model": name, "start": streak_start, "hours": streak, "peak": peak_val})
                streak = 0
                streak_start = None
                peak_val = 0
                continue

            pt_time = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

            if pt_time < now or pt_time > cutoff:
                continue

            if val > WIND_THRESHOLD:
                if streak == 0:
                    streak_start = pt_time
                streak += 1
                peak_val = max(peak_val, val)
            else:
                if streak >= MIN_HOURS:
                    alerts.append({"model": name, "start": streak_start, "hours": streak, "peak": peak_val})
                streak = 0
                streak_start = None
                peak_val = 0

        if streak >= MIN_HOURS:
            alerts.append({"model": name, "start": streak_start, "hours": streak, "peak": peak_val})

    return alerts


async def send_notification(alerts):
    """Send a push notification via ntfy.sh."""
    if not NTFY_TOPIC:
        print("WARNING: NTFY_TOPIC not set. Skipping notification.", flush=True)
        return

    pacific = timezone(timedelta(hours=-7))
    lines = ["Jericho Beach Wind Alert!"]
    for a in alerts:
        start_local = a["start"].astimezone(pacific)
        lines.append(
            f"{a['model']}: >{WIND_THRESHOLD}kt for ~{a['hours']}hrs "
            f"starting {start_local.strftime('%a %-I%p')} "
            f"(peak {a['peak']:.0f}kt)"
        )
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

    print(f"--- Notification content ---", flush=True)
    print(message, flush=True)
    print(f"---", flush=True)


async def main():
    print("=" * 50, flush=True)
    print("Jericho Beach Wind Alert Check", flush=True)
    print(f"Threshold: >{WIND_THRESHOLD} knots for >{MIN_HOURS - 1} hours", flush=True)
    print(f"Forecast window: next {FORECAST_WINDOW_HRS} hours", flush=True)
    print("=" * 50, flush=True)

    data = await get_wind_data()

    if not data:
        print("ERROR: Could not extract chart data from the page.", flush=True)
        sys.exit(1)

    print(f"Extracted {len(data)} series:", flush=True)
    for s in data:
        print(f"  {s['name']:20s} - {len(s['data'])} data points", flush=True)

    alerts = check_for_alerts(data)

    if alerts:
        print(f"ALERT TRIGGERED - {len(alerts)} windy period(s) found:", flush=True)
        for a in alerts:
            print(f"   {a['model']}: {a['hours']}hrs from {a['start']} (peak {a['peak']}kt)", flush=True)
        await send_notification(alerts)
    else:
        print("No sustained wind above threshold. All clear.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
