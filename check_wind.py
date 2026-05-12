
check_wind_py = r'''#!/usr/bin/env python3
"""
Jericho Beach Wind Forecast Alert
Scrapes bigwavedave.ca Highcharts data for Jericho Beach (site=20),
checks if Model1 or Model2 forecast wind >10 knots for >2 consecutive hours
in the next 48 hours, and sends a push notification via ntfy.sh.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx
from playwright.async_api import async_playwright

# --- Configuration ---
SITE_URL = "https://bigwavedave.ca/jerichobch.html?site=20"
WIND_THRESHOLD = 10       # knots (alert if ABOVE this)
MIN_HOURS = 3             # 3+ consecutive hourly readings = more than 2 hours
FORECAST_WINDOW_HRS = 48  # look ahead 48 hours
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")


async def extract_chart_data(page):
    """Pull all series data out of the first Highcharts chart on the page."""
    return await page.evaluate("""
        () => {
            if (typeof Highcharts === 'undefined') return null;
            const charts = Highcharts.charts.filter(c => c);
            if (!charts.length) return null;
            const chart = charts[0];
            return chart.series.map(s => ({
                name: s.name,
                data: s.xData.map((x, i) => [x, s.yData[i]])
            }));
        }
    """)


async def get_wind_data():
    """Load the BigWaveDave page and extract Model1/Model2 forecast data."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        all_series = {}

        # Load today's page
        print(f"Loading: {SITE_URL}")
        await page.goto(SITE_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(5000)
        today_data = await extract_chart_data(page)

        if today_data:
            for s in today_data:
                name = s["name"]
                if name not in all_series:
                    all_series[name] = {}
                for x, y in s["data"]:
                    if x is not None and y is not None:
                        all_series[name][x] = y

        # Click "Next" to get tomorrow's data for full 48-hour coverage
        try:
            next_btn = page.locator("text=>>").first
            if await next_btn.is_visible():
                await next_btn.click()
                await page.wait_for_timeout(5000)
                tmrw_data = await extract_chart_data(page)
                if tmrw_data:
                    for s in tmrw_data:
                        name = s["name"]
                        if name not in all_series:
                            all_series[name] = {}
                        for x, y in s["data"]:
                            if x is not None and y is not None:
                                all_series[name][x] = y
        except Exception as e:
            print(f"Could not load next day (non-fatal): {e}")

        await browser.close()

    # Convert to sorted list format
    result = []
    for name, points in all_series.items():
        sorted_points = sorted(points.items())
        result.append({
            "name": name,
            "data": [[ts, val] for ts, val in sorted_points]
        })
    return result


def check_for_alerts(series_list):
    """
    Check each model series for periods where wind > WIND_THRESHOLD
    for more than 2 hours (i.e., MIN_HOURS consecutive hourly readings).
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=FORECAST_WINDOW_HRS)
    alerts = []

    for series in series_list:
        name = series["name"]
        # Only check Model1 and Model2 (skip observations, temp, pressure)
        if "model" not in name.lower() and "Model" not in name:
            continue

        data = series["data"]
        streak = 0
        streak_start = None
        peak_val = 0

        for ts_ms, val in data:
            if ts_ms is None or val is None:
                # End of streak
                if streak >= MIN_HOURS:
                    alerts.append({
                        "model": name,
                        "start": streak_start,
                        "hours": streak,
                        "peak": peak_val
                    })
                streak = 0
                streak_start = None
                peak_val = 0
                continue

            pt_time = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

            # Only look at the next 48 hours
            if pt_time < now or pt_time > cutoff:
                continue

            if val > WIND_THRESHOLD:
                if streak == 0:
                    streak_start = pt_time
                streak += 1
                peak_val = max(peak_val, val)
            else:
                if streak >= MIN_HOURS:
                    alerts.append({
                        "model": name,
                        "start": streak_start,
                        "hours": streak,
                        "peak": peak_val
                    })
                streak = 0
                streak_start = None
                peak_val = 0

        # Handle streak that runs to end of data
        if streak >= MIN_HOURS:
            alerts.append({
                "model": name,
                "start": streak_start,
                "hours": streak,
                "peak": peak_val
            })

    return alerts


async def send_notification(alerts):
    """Send a push notification via ntfy.sh."""
    if not NTFY_TOPIC:
        print("WARNING: NTFY_TOPIC not set. Skipping notification.")
        return

    pacific = timezone(timedelta(hours=-7))
    lines = ["🌬️ Jericho Beach Wind Alert!\n"]
    for a in alerts:
        start_local = a["start"].astimezone(pacific)
        lines.append(
            f"• {a['model']}: >{WIND_THRESHOLD}kt for ~{a['hours']}hrs "
            f"starting {start_local.strftime('%a %-I%p')} "
            f"(peak {a['peak']:.0f}kt)"
        )
    lines.append(f"\nhttps://bigwavedave.ca/jerichobch.html?site=20")
    message = "\n".join(lines)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            content=message.encode("utf-8"),
            headers={
                "Title": "Wind Alert - Jericho Beach",
                "Priority": "high",
                "Tags": "wind_face"
            },
        )
        print(f"Notification sent (status {resp.status_code})")

    print(f"\n--- Notification content ---\n{message}\n")


async def main():
    print("=" * 50)
    print("Jericho Beach Wind Alert Check")
    print(f"Threshold: >{WIND_THRESHOLD} knots for >{MIN_HOURS - 1} hours")
    print(f"Forecast window: next {FORECAST_WINDOW_HRS} hours")
    print("=" * 50)

    data = await get_wind_data()

    if not data:
        print("ERROR: Could not extract chart data from the page.")
        return

    print(f"\nExtracted {len(data)} series:")
    for s in data:
        print(f"  {s['name']:20s} — {len(s['data'])} data points")

    alerts = check_for_alerts(data)

    if alerts:
        print(f"\n🚨 ALERT TRIGGERED — {len(alerts)} windy period(s) found:")
        for a in alerts:
            print(f"   {a['model']}: {a['hours']}hrs from {a['start']} (peak {a['peak']}kt)")
        await send_notification(alerts)
    else:
        print("\n✅ No sustained wind above threshold. All clear.")


if __name__ == "__main__":
    asyncio.run(main())
'''

with open("check_wind.py", "w") as f:
    f.write(check_wind_py.strip())

print(check_wind_py.strip())

