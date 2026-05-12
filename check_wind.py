#!/usr/bin/env python3
"""
Jericho Beach Wind Forecast Alert (Model2 / EC HRDPS)
Loads bigwavedave.ca/forecast.html#model2, selects Jericho Beach,
reads the EC (Model2) wind forecast table, and sends an alert
if any forecast value exceeds 10 knots.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
from playwright.async_api import async_playwright

SITE_URL = "https://bigwavedave.ca/forecast.html#model2"
WIND_THRESHOLD = 10
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")


async def get_wind_data():
    """Load forecast page, select Jericho Beach, extract Model2 data."""
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

        print(f"Loading: {SITE_URL}", flush=True)
        try:
            await page.goto(SITE_URL, wait_until="commit", timeout=60000)
        except Exception as e:
            print(f"  Navigation error: {e}", flush=True)
            await browser.close()
            return None

        print("  Waiting for page to load...", flush=True)
        await page.wait_for_timeout(10000)

        # Click Model2 tab if not already selected
        try:
            model2_tab = page.locator("text=Model2").first
            if await model2_tab.is_visible(timeout=5000):
                await model2_tab.click()
                print("  Clicked Model2 tab", flush=True)
                await page.wait_for_timeout(3000)
        except Exception as e:
            print(f"  Model2 tab click (non-fatal): {e}", flush=True)

        # Select Jericho Beach from dropdown
        try:
            # Try finding a select element with site options
            selected = await page.evaluate("""
                () => {
                    const selects = document.querySelectorAll('select');
                    for (const sel of selects) {
                        for (const opt of sel.options) {
                            if (opt.text.toLowerCase().includes('jericho')) {
                                sel.value = opt.value;
                                sel.dispatchEvent(new Event('change', { bubbles: true }));
                                return opt.text;
                            }
                        }
                    }
                    return null;
                }
            """)
            if selected:
                print(f"  Selected: {selected}", flush=True)
                await page.wait_for_timeout(5000)
            else:
                print("  WARNING: Could not find Jericho Beach in dropdown", flush=True)
                # Try clicking approach
                dropdown = page.locator("select").first
                if await dropdown.is_visible(timeout=3000):
                    options = await page.evaluate("""
                        () => {
                            const sel = document.querySelector('select');
                            if (!sel) return [];
                            return Array.from(sel.options).map(o => o.text);
                        }
                    """)
                    print(f"  Available sites: {options}", flush=True)
        except Exception as e:
            print(f"  Dropdown selection error: {e}", flush=True)

        # Wait for Highcharts to render
        print("  Waiting for Highcharts...", flush=True)
        chart_ready = False
        for i in range(30):
            ready = await page.evaluate("""
                () => {
                    try {
                        if (typeof Highcharts === 'undefined') return false;
                        const charts = Highcharts.charts.filter(c => c);
                        return charts.length > 0 && charts[0].series && charts[0].series.length > 0;
                    } catch(e) { return false; }
                }
            """)
            if ready:
                print(f"  Highcharts ready after {i+1}s", flush=True)
                chart_ready = True
                break
            await page.wait_for_timeout(1000)

        if not chart_ready:
            print("  ERROR: Highcharts did not load", flush=True)
            await browser.close()
            return None

        # Extract all chart data - get series names and values
        data = await page.evaluate("""
            () => {
                try {
                    const charts = Highcharts.charts.filter(c => c);
                    if (!charts.length) return null;
                    const result = [];
                    for (const chart of charts) {
                        const chartData = {
                            title: chart.title ? chart.title.textStr : 'Unknown',
                            series: []
                        };
                        for (const s of chart.series) {
                            chartData.series.push({
                                name: s.name,
                                data: s.xData.map((x, i) => ({
                                    time: x,
                                    value: s.yData[i]
                                }))
                            });
                        }
                        result.push(chartData);
                    }
                    return result;
                } catch(e) { return null; }
            }
        """)

        await browser.close()
        return data


def check_for_wind(charts_data):
    """Check if any EC (Model2) value exceeds the threshold."""
    alerts = []

    for chart in charts_data:
        title = chart.get("title", "Unknown")
        print(f"  Chart: {title}", flush=True)

        for series in chart.get("series", []):
            name = series["name"]
            # Look for EC column (Model2) - skip Dark, EC Gust, weather stations
            if name != "EC":
                continue

            values = series["data"]
            windy_times = []
            peak = 0

            for point in values:
                t = point.get("time")
                v = point.get("value")
                if v is not None and v > WIND_THRESHOLD:
                    pacific = timezone(timedelta(hours=-7))
                    pt_time = datetime.fromtimestamp(t / 1000, tz=pacific)
                    windy_times.append({"time": pt_time, "value": v})
                    peak = max(peak, v)

            if windy_times:
                alerts.append({
                    "chart_title": title,
                    "times": windy_times,
                    "peak": peak,
                    "count": len(windy_times)
                })
                print(f"    EC: {len(windy_times)} readings >{WIND_THRESHOLD}kt (peak {peak}kt)", flush=True)
            else:
                print(f"    EC: all readings <={WIND_THRESHOLD}kt", flush=True)

    return alerts


async def send_notification(alerts):
    """Send a push notification via ntfy.sh."""
    if not NTFY_TOPIC:
        print("WARNING: NTFY_TOPIC not set. Skipping notification.", flush=True)
        return

    lines = ["Jericho Beach Wind Alert! (Model2 EC HRDPS)", ""]
    for a in alerts:
        first = a["times"][0]
        last = a["times"][-1]
        lines.append(
            f"Peak {a['peak']:.0f}kt, "
            f"{a['count']} readings >{WIND_THRESHOLD}kt"
        )
        lines.append(
            f"From {first['time'].strftime('%a %-I%p')} "
            f"to {last['time'].strftime('%a %-I%p')}"
        )
        # Show the highest readings
        top5 = sorted(a["times"], key=lambda x: x["value"], reverse=True)[:5]
        for t in top5:
            lines.append(f"  {t['time'].strftime('%a %-I%p')}: {t['value']:.0f}kt")
    lines.append("")
    lines.append("https://bigwavedave.ca/forecast.html#model2")
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
    print("Jericho Beach Wind Alert (Model2)", flush=True)
    print(f"Alert if any EC forecast >{WIND_THRESHOLD} knots", flush=True)
    print("=" * 50, flush=True)

    data = await get_wind_data()

    if not data:
        print("ERROR: Could not extract chart data.", flush=True)
        sys.exit(1)

    print(f"Extracted {len(data)} chart(s):", flush=True)
    for chart in data:
        series_names = [s["name"] for s in chart.get("series", [])]
        print(f"  {chart.get('title', 'Unknown')}: {series_names}", flush=True)

    alerts = check_for_wind(data)

    if alerts:
        print(f"ALERT TRIGGERED - wind >{WIND_THRESHOLD}kt found!", flush=True)
        await send_notification(alerts)
    else:
        print(f"No wind >{WIND_THRESHOLD}kt in forecast. All clear.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
