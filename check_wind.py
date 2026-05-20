#!/usr/bin/env python3
"""
Jericho Beach Wind Forecast Alert
Calls bigwavedave.ca's extractwinddata.php API directly for today + next 2 days.
No browser needed! Fast, reliable, and simple.
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import httpx

SITE = 20  # Jericho Beach
DATA_URL = "https://bigwavedave.ca/sqlwind/extractwinddata.php"
WIND_THRESHOLD = 10
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
PACIFIC = timezone(timedelta(hours=-7))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Referer": "https://bigwavedave.ca/jerichobch.html?site=20",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}


async def fetch_day_data(client, day_str):
    """Fetch wind data for a specific day from the API."""
    params = {"site": SITE, "day": day_str}
    print(f"  Fetching day={day_str}", flush=True)
    try:
        resp = await client.get(DATA_URL, params=params, timeout=30)
        print(f"    Status: {resp.status_code}, Length: {len(resp.text)} chars", flush=True)
        if resp.status_code == 200:
            text = resp.text.strip()
            print(f"    Preview: {text[:500]}", flush=True)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    print(f"    Not JSON, returning raw text", flush=True)
                    return {"raw": text}
        else:
            print(f"    HTTP error: {resp.status_code}", flush=True)
            print(f"    Response preview: {resp.text[:300]}", flush=True)
    except Exception as e:
        print(f"    Request failed: {e}", flush=True)
    return None


def extract_model2_from_response(data):
    """Try to find Model2 wind values from API response."""
    values = []

    if isinstance(data, dict):
        if "raw" in data:
            # Try to parse numbers from raw text using regex
            # Look for model2 array pattern
            raw = data["raw"]
            m = re.search(r'model2\s*[=:]\s*\[([^\]]+)\]', raw, re.IGNORECASE)
            if m:
                nums = re.findall(r'-?\d+(?:\.\d+)?', m.group(1))
                values = [float(x) for x in nums]
            else:
                # Try to find any array of numbers
                arrays = re.findall(r'\[([0-9,.\s-]+)\]', raw)
                for arr in arrays:
                    nums = re.findall(r'-?\d+(?:\.\d+)?', arr)
                    if len(nums) > 3:
                        values = [float(x) for x in nums]
                        break
        else:
            # JSON dict - look for model2-related keys
            for key in data:
                key_lower = key.lower()
                if any(k in key_lower for k in ["model2", "model 2", "ec", "hrdps"]):
                    vals = data[key]
                    if isinstance(vals, list):
                        values.extend([float(v) for v in vals if isinstance(v, (int, float))])

    elif isinstance(data, list):
        # Could be array of records
        for item in data:
            if isinstance(item, dict):
                for key in item:
                    if "model2" in key.lower() or "ec" in key.lower():
                        v = item[key]
                        if isinstance(v, (int, float)):
                            values.append(float(v))
            elif isinstance(item, (int, float)):
                values.append(float(item))

    return values


async def send_notification(m2_max, m2_above_count, all_values, days_checked):
    """Send a push notification via ntfy.sh."""
    if not NTFY_TOPIC:
        print("WARNING: NTFY_TOPIC not set. Skipping notification.", flush=True)
        return

    lines = [
        "Jericho Beach Wind Alert!",
        "",
        f"Model2 peak: {m2_max:.0f}kt",
        f"{m2_above_count} readings >{WIND_THRESHOLD}kt",
        f"Days checked: {', '.join(days_checked)}",
        "",
        "https://bigwavedave.ca/jerichobch.html?site=20"
    ]
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

    now_pacific = datetime.now(PACIFIC)
    days = [(now_pacific + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)]
    print(f"Checking days: {days}", flush=True)

    all_model2_values = []

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        # First visit the main page to establish a session/cookies
        print("\nVisiting main page first (for cookies)...", flush=True)
        try:
            main_resp = await client.get("https://bigwavedave.ca/jerichobch.html?site=20", timeout=30)
            print(f"  Main page status: {main_resp.status_code}", flush=True)
            cookies = dict(main_resp.cookies)
            if cookies:
                print(f"  Cookies received: {list(cookies.keys())}", flush=True)
        except Exception as e:
            print(f"  Main page visit failed (non-fatal): {e}", flush=True)

        for day_str in days:
            print(f"\n--- {day_str} ---", flush=True)
            data = await fetch_day_data(client, day_str)
            if data is None:
                continue

            m2_vals = extract_model2_from_response(data)
            if m2_vals:
                print(f"  Model2 values: {m2_vals}", flush=True)
                all_model2_values.extend(m2_vals)
            else:
                print(f"  Could not extract Model2 values from response", flush=True)
                if isinstance(data, dict) and "raw" not in data:
                    print(f"  Response keys: {list(data.keys())}", flush=True)

    print(f"\n{'='*50}", flush=True)
    print(f"All Model2 values: {all_model2_values}", flush=True)

    if all_model2_values:
        m2_max = max(all_model2_values)
        m2_above = [v for v in all_model2_values if v > WIND_THRESHOLD]
        print(f"Max: {m2_max}, Above {WIND_THRESHOLD}kt: {len(m2_above)} readings", flush=True)

        if m2_above:
            print(f"ALERT TRIGGERED!", flush=True)
            await send_notification(m2_max, len(m2_above), all_model2_values, days)
        else:
            print(f"No wind >{WIND_THRESHOLD}kt. All clear.", flush=True)
    else:
        print("Could not extract Model2 values. Check API response format above.", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
