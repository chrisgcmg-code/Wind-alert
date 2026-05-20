#!/usr/bin/env python3
"""
Jericho Beach Wind Forecast Alert
Adapted from working local Selenium script to Playwright for GitHub Actions.
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
MAX_WAIT = 20
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")


async def get_model2_values(page):
    """
    Extract Model2 values using multiple fallback methods
    (adapted from working Selenium script).
    """
    # Method 1: Highcharts series matching /model\s*2/i
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
                                if (Array.isArray(s.yData)) {
                                    out.push(...s.yData);
                                } else if (s.options && Array.isArray(s.options.data)) {
                                    for (const d of s.options.data) {
                                        if (Array.isArray(d)) out.push(d[1]);
                                        else if (typeof d === 'number') out.push(d);
                                        else if (d && typeof d.y === 'number') out.push(d.y);
                                    }
                                }
                            }
                        }
                    }
                    if (out.length > 0) return out;
                }
            } catch(e) {}

            // Method 2: window.model2 array
            try {
                if (typeof model2 !== 'undefined' && Array.isArray(model2)) return model2;
                if (typeof window !== 'undefined' && Array.isArray(window.model2)) return window.model2;
            } catch(e) {}

            return null;
        }
    """)
    if vals:
        return [float(x) for x in vals if isinstance(x, (int, float))]
    return []


async def get_all_model_data(page):
    """Extract timestamped data for all series from Highcharts."""
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


async def wait_for_model2_change(page, old_values, timeout=MAX_WAIT):
    """Poll until Model2 values appear and are different from old_values."""
    for i in range(timeout * 2):
        vals = await get_model2_values(page)
        if vals and vals != old_values:
            print(f"  Model2 values changed after {(i+1)*0.5:.1f}s", flush=True)
            return vals
        await page.wait_for_timeout(500)
    # Return whatever we have even if unchanged
    return await get_model2_values(page)


async def click_next_day(page):
    """
    Click the Next Day button with multiple fallbacks
    (adapted from working Selenium script).
    """
    # Method 1: Click by ID
    try:
        btn = page.locator("#NextButton")
        if await btn.is_visible(timeout=3000):
            await btn.scroll_into_view_if_needed()
            await page.wait_for_timeout(200)
            await btn.click()
            print("  Clicked #NextButton", flush=True)
            return True
    except Exception as e:
        print(f"  Direct click failed: {e}", flush=True)

    # Method 2: JS click on the element
    try:
        clicked = await page.evaluate("""
            () => {
                const btn = document.getElementById('NextButton');
                if (btn) { btn.click(); return true; }
                return false;
            }
        """)
        if clicked:
            print("  JS-clicked #NextButton", flush=True)
            return True
    except Exception as e:
        print(f"  JS click failed: {e}", flush=True)

    # Method 3: Call ChangeDate directly
    try:
        await page.evaluate("if (typeof ChangeDate === 'function') ChangeDate(1);")
        print("  Called ChangeDate(1)", flush=True)
        return True
    except Exception as e:
        print(f"  ChangeDate(1) failed: {e}", flush=True)

    return False


async def get_wind_data():
    """Load the BigWaveDave page and extract wind forecast data for today + 2 days."""
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
        all_model2_values = []

        # --- Load initial page ---
        print(f"Loading: {SITE_URL}", flush=True)
        try:
            await page.goto(SITE_URL, wait_until="commit", timeout=60000)
        except Exception as e:
            print(f"  Navigation error: {e}", flush=True)
            await browser.close()
            return [], []

        print("  Waiting for page to settle...", flush=True)
        await page.wait_for_timeout(10000)

        has_hc = await page.evaluate("() => typeof Highcharts !== 'undefined'")
        print(f"  Highcharts available: {has_hc}", flush=True)
        if not has_hc:
            await page.wait_for_timeout(15000)
            has_hc = await page.evaluate("() => typeof Highcharts !== 'undefined'")
            print(f"  Highcharts available (2nd check): {has_hc}", flush=True)
        if not has_hc:
            await browser.close()
            return [], []

        # Debug: print ChangeDate function source
        try:
            cd_source = await page.evaluate("() => typeof ChangeDate === 'function' ? ChangeDate.toString().substring(0, 200) : 'NOT FOUND'")
            print(f"  ChangeDate function: {cd_source}", flush=True)
        except Exception:
            pass

        # --- Extract today's data ---
        today_m2 = await get_model2_values(page)
        print(f"  Today Model2: {len(today_m2)} points, values: {today_m2}", flush=True)
        all_model2_values.extend(today_m2)

        today_data = await get_all_model_data(page)
        if today_data:
            for s in today_data:
                name = s["name"]
                if name not in all_series:
                    all_series[name] = {}
                for x, y in s["data"]:
                    if x is not None and y is not None:
                        all_series[name][x] = y
            print(f"  Loaded today: {len(today_data)} series", flush=True)

        # --- Navigate to next 2 days ---
        current_m2 = today_m2
        for day in range(1, 3):
            print(f"  --- Day +{day} ---", flush=True)
            clicked = await click_next_day(page)
            if not clicked:
                print(f"  WARNING: Could not click next button for day +{day}", flush=True)
                continue

            # Wait for Model2 values to actually change (key fix from local script)
            new_m2 = await wait_for_model2_change(page, current_m2, timeout=MAX_WAIT)

            if new_m2 and new_m2 != current_m2:
                print(f"  Day +{day} Model2: {len(new_m2)} points, values: {new_m2}", flush=True)
                all_model2_values.extend(new_m2)
                current_m2 = new_m2

                # Also extract full chart data
                day_data = await get_all_model_data(page)
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
                print(f"  WARNING: Model2 values did not change for day +{day}", flush=True)
                print(f"  Got: {new_m2}", flush=True)

        await browser.close()

    result = []
    for name, points in all_series.items():
        sorted_points = sorted(points.items())
        result.append({
            "name": name,
            "data": [[ts, val] for ts, val in sorted_points]
        })
    return result, all_model2_values


def check_for_alerts(series_list, all_model2_values):
    """Check if any Model2 value exceeds threshold."""
    alerts = []

    # Check from timestamped series data
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

        print(f"  {name}: {len(series['data'])} pts, values: {[v for _, v in series['data'] if v is not None]}", flush=True)

    # Also check raw model2 values (fallback, in case timestamps don't capture all days)
    if all_model2_values:
        m2_max = max(all_model2_values)
        m2_above = [v for v in all_model2_values if v > WIND_THRESHOLD]
        print(f"  All Model2 raw values ({len(all_model2_values)} total): max={m2_max}, above {WIND_THRESHOLD}kt: {len(m2_above)}", flush=True)
        if m2_above and not any(a["model"] == "Model2" for a in alerts):
            alerts.append({
                "model": "Model2 (raw)",
                "points": [{"time": datetime.now(timezone(timedelta(hours=-7))), "value": v} for v in m2_above],
                "peak": max(m2_above),
                "count": len(m2_above)
            })

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
    print(f"Alert if any Model2 forecast >{WIND_THRESHOLD} knots", flush=True)
    print("=" * 50, flush=True)

    data, all_m2 = await get_wind_data()

    if not data and not all_m2:
        print("ERROR: Could not extract any data from the page.", flush=True)
        sys.exit(1)

    print(f"Extracted {len(data)} series:", flush=True)
    for s in data:
        print(f"  {s['name']:20s} - {len(s['data'])} data points", flush=True)

    print("", flush=True)
    print("Checking for wind alerts...", flush=True)
    alerts = check_for_alerts(data, all_m2)

    if alerts:
        print(f"ALERT TRIGGERED - wind >{WIND_THRESHOLD}kt found!", flush=True)
        for a in alerts:
            print(f"  {a['model']}: {a['count']} readings, peak {a['peak']}kt", flush=True)
        await send_notification(alerts)
    else:
        print(f"No wind >{WIND_THRESHOLD}kt in forecast. All clear.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
