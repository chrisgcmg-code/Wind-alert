#!/usr/bin/env python3
"""
Jericho Beach Wind Forecast Alert
Calls bigwavedave.ca's extractwinddata.php API directly for today + next 2 days.
No browser needed! Fast, reliable, and simple.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx

SITE = 20  # Jericho Beach
DATA_URL = "https://bigwavedave.ca/sqlwind/extractwinddata.php"
WIND_THRESHOLD = 10
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
PACIFIC = timezone(timedelta(hours=-7))


async def fetch_day_data(client, day_str):
    """Fetch wind data for a specific day from the API."""
    params = {"site": SITE, "day": day_str}
    print(f"  Fetching {DATA_URL}?site={SITE}&day={day_str}", flush=True)
    try:
        resp = await client.get(DATA_URL, params=params, timeout=30)
        print(f"    Status: {resp.status_code}, Content-Type: {resp.headers.get('content-type', 'unknown')}, Length: {len(resp.text)} chars", flush=True)
        if resp.status_code == 200:
            text = resp.text.strip()
            # Print first 500 chars for debugging
            print(f"    Response preview: {text[:500]}", flush=True)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    print(f"    Not JSON, trying to parse as other format...", flush=True)
                    return {"raw": text}
        else:
            print(f"    HTTP error: {resp.status_code}", flush=True)
    except Exception as e:
        print(f"    Request failed: {e}", flush=True)
    return None


async def main():
    print("=" * 50, flush=True)
    print("Jericho Beach Wind Alert Check", flush=True)
    print(f"Alert if any Model2 forecast >{WIND_THRESHOLD} knots", flush=True)
    print("=" * 50, flush=True)

    # Calculate today and next 2 days in Pacific time
    now_pacific = datetime.now(PACIFIC)
    days = []
    for i in range(3):
        d = now_pacific + timedelta(days=i)
        days.append(d.strftime("%Y-%m-%d"))

    print(f"Checking days: {days}", flush=True)

    all_model2_values = []

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/125.0.0.0"},
        follow_redirects=True
    ) as client:
        for day_str in days:
            print(f"\n--- {day_str} ---", flush=True)
            data = await fetch_day_data(client, day_str)
            if data is None:
                print(f"  No data returned for {day_str}", flush=True)
                continue

            # Try to extract Model2 values from the response
            if isinstance(data, dict):
                # Print all keys to understand the structure
                if "raw" not in data:
                    print(f"  Keys: {list(data.keys())}", flush=True)

                # Look for model2 data in various possible keys
                for key in data:
                    key_lower = key.lower()
                    if "model2" in key_lower or "model 2" in key_lower or "ec" in key_lower or "hrdps" in key_lower:
                        vals = data[key]
                        print(f"  Found '{key}': {vals}", flush=True)
                        if isinstance(vals, list):
                            numeric = [float(v) for v in vals if isinstance(v, (int, float))]
                            all_model2_values.extend(numeric)

                # If we didn't find specific model2 keys, print all data
                if not all_model2_values:
                    print(f"  Full response data:", flush=True)
                    if isinstance(data, dict) and "raw" in data:
                        print(f"    {data['raw'][:1000]}", flush=True)
                    else:
                        print(f"    {json.dumps(data, indent=2)[:1000]}", flush=True)

            elif isinstance(data, list):
                print(f"  Response is a list with {len(data)} items", flush=True)
                print(f"  First item: {data[0] if data else 'empty'}", flush=True)

    print(f"\n{'='*50}", flush=True)
    print(f"All Model2 values collected: {all_model2_values}", flush=True)

    if all_model2_values:
        m2_max = max(all_model2_values)
        m2_above = [v for v in all_model2_values if v > WIND_THRESHOLD]
        print(f"Max: {m2_max}, Above {WIND_THRESHOLD}kt: {len(m2_above)} readings", flush=True)

        if m2_above:
            print(f"ALERT TRIGGERED!", flush=True)
            if NTFY_TOPIC:
                message = f"Jericho Beach Wind Alert!\nModel2 peak: {m2_max:.0f}kt\n{len(m2_above)} readings >{WIND_THRESHOLD}kt\nhttps://bigwavedave.ca/jerichobch.html?site=20"
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"https://ntfy.sh/{NTFY_TOPIC}",
                        content=message.encode("utf-8"),
                        headers={"Title": "Wind Alert - Jericho Beach", "Priority": "high", "Tags": "wind_face"},
                    )
                    print(f"Notification sent (status {resp.status_code})", flush=True)
            else:
                print("WARNING: NTFY_TOPIC not set. Skipping notification.", flush=True)
        else:
            print(f"No wind >{WIND_THRESHOLD}kt. All clear.", flush=True)
    else:
        print("Could not extract Model2 values from API. Will need to check response format.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
