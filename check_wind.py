#!/usr/bin/env python3
"""
Jericho Beach Wind Forecast Alert
Scrapes bigwavedave.ca Highcharts data for Jericho Beach (site=20),
sends alert if any Model2 (EC HRDPS) forecast value exceeds 10 knots.
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
                } catch(e) { return false; }
            }
        """)
        if ready:
            print(f"  Highcharts ready after {i+1}s", flush=True)
            return True
        await page.wait_for_timeout(1000)
    print(f"  Highcharts not ready after {timeout}s", flush=True)
    return False


async def get_chart_date_range(page):
    """Get the current xAxis min timestamp to detect when chart data changes."""
    return await page.evaluate("""
        () => {
            try {
                const charts = Highcharts.charts.filter(c => c);
                if (!charts.length) return null;
                const chart = charts[0];
                return chart.xAxis[0].min;
            } catch(e) { return null; }
        }
    """)


async def wait_for_date_change(page, old_min, timeout=20):
    """Wait until the chart's xAxis min changes (indicating new day loaded)."""
    for i in range(timeout):
        new_min = await get_chart_date_range(page)
        if new_min is not None and new_min != old_min:
            print(f"  Chart date changed after {i+1}s (xAxis min: {old_min} -> {new_min})", flush=True)
            return True
        await page.wait_for_timeout(1000)
    print(f"  Chart date did not change after {timeout}s", flush=True)
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
            } catch(e) { return null; }
        }
    """)


async def get_wind_data():
    """Load the BigWaveDave page and extract wind forecast data."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled', '--disable-dev-shm-usage']
        )
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            viewport={'width': 1920, 'height': 1080},
            java_script_enabled=True,
        )
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => false });")

        all_series = {}

        print(f"Loading: {SITE_URL}", flush=True)
        try:
            await page.goto(SITE_URL, wait_until="commit", timeout=60000)
            print("  Page navigation started...", flush=True)
        except Exception as e:
            print(f"  Navigation error: {e}", flush=True)
            await browser.close()
            return []

        print("  Waiting for page to settle...", flush=True)
        await page.wait_for_timeout(10000)

        has_highcharts = await page.evaluate("() => typeof Highcharts !== 'undefined'")
        print(f"  Highcharts available: {has_highcharts}", flush=True)

        if not has_highcharts:
            print("  Waiting additional 15s for scripts...", flush=True)
            await page.wait_for_timeout(15000)
            has_highcharts = await page.evaluate("() => typeof Highcharts !== 'undefined'")
            print(f"  Highcharts available (2nd check): {has_highcharts}", flush=True)

        if not has_highcharts:
            title = await page.title()
            content_len = await page.evaluate("() => document.body.innerText.length")
            print(f"  Page title: {title}", flush=True)
            print(f"  Page content length: {content_len} chars", flush=True)
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
            model2_today = sum(1 for s in today_data if 'model' in s['name'].lower() for x, y in s['data'] if y is not None)
            print(f"  Loaded today: {len(today_data)} series ({model2_today} model data points)", flush=True)
        else:
            print("  WARNING: No data extracted for today", flush=True)

        # Click next day by calling ChangeDate(1) directly
        for day in range(1, 3):
            try:
                old_min = await get_chart_date_range(page)
                print(f"  Calling ChangeDate(1) for day +{day}...", flush=True)
                await page.evaluate("ChangeDate(1)")

                # Wait for the chart data to actually change
                date_changed = await wait_for_date_change(page, old_min)

                if date_changed:
                    # Give it a moment to fully render
                    await page.wait_for_timeout(2000)
                    day_data = await extract_chart_data(page)
                    if day_data:
                        new_points = 0
                        for s in day_data:
                            name = s["name"]
                            if name not in all_series:
                                all_series[name] = {}
                            for x, y in s["data"]:
                                if x is not None and y is not None:
                                    if x not in all_series[name]:
                                        new_points += 1
                                    all_series[name][x] = y
                        print(f"  Loaded day +{day}: {new_points} new data points", flush=True)
                    else:
                        print(f"  WARNING: No data extracted for day +{day}", flush=True)
                else:
                    print(f"  WARNING: Chart did not update for day +{day}", flush=True)
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
    """Check Model1/Model2 series for any value above threshold."""
    alerts = []

    for series in series_list:
        name = series["name"]
        if "model" not in name.lower():
            continue

        windy_points = []
        peak = 0

        for ts_ms, val in series["data"]:
            if ts_ms is None or val is None:
                continue
            if val > WIND_THRESHOLD:
                pacific = timezone(timedelta(hours=-7))
                pt_time = datetime.fromtimestamp(ts_ms / 1000, tz=pacific)
                windy_points.append({"time": pt_time, "value": val})
                peak = max(peak, val)

        if windy_points:
            alerts.append({
                "model": name,
                "points": windy_points,
                "peak": peak,
                "count": len(windy_points)
            })

        # Debug: print all values for model series
        print(f"  {name}: {len(series['data'])} points, values: {[v for _, v in series['data'] if v is not None]}", flush=True)

    return alerts


async def send_notification(alerts):
    """Send a push notification via ntfy.sh."""
    if not NTFY_TOPIC:
        print("WARNING: NTFY_TOPIC not set. Skipping notification.", flush=True)
        return

    lines = ["Jericho Beach Wind Alert!", ""]
    for a in alerts:
        lines.append(f"{a['model']}: {a['count']} readings >{WIND_THRESHOLD}kt (peak {a['peak']:.0f}kt)")
        top5 = sorted(a["points"], key=lambda x: x["value"], reverse=True)[:5]
        for t in top5:
            lines.append(f"  {t['time'].strftime('%a %-I%p')}: {t['value']:.0f}kt")
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

    print("--- Notification content ---", flush=True)
    print(message, flush=True)
    print("---", flush=True)


async def main():
    print("=" * 50, flush=True)
    print("Jericho Beach Wind Alert Check", flush=True)
    print(f"Alert if any Model forecast >{WIND_THRESHOLD} knots", flush=True)
    print("=" * 50, flush=True)

    data = await get_wind_data()

    if not data:
        print("ERROR: Could not extract chart data from the page.", flush=True)
        sys.exit(1)

    print(f"Extracted {len(data)} series:", flush=True)
    for s in data:
        print(f"  {s['name']:20s} - {len(s['data'])} data points", flush=True)

    print("", flush=True)
    print("Checking model series for wind alerts...", flush=True)
    alerts = check_for_alerts(data)

    if alerts:
        print(f"ALERT TRIGGERED - wind >{WIND_THRESHOLD}kt found!", flush=True)
        for a in alerts:
            print(f"  {a['model']}: {a['count']} readings, peak {a['peak']}kt", flush=True)
        await send_notification(alerts)
    else:
        print(f"No wind >{WIND_THRESHOLD}kt in forecast. All clear.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
