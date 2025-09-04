import os
import time
import re
import traceback
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, ElementClickInterceptedException
from webdriver_manager.chrome import ChromeDriverManager
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

# --------------- CONFIG ---------------
URL = "https://bigwavedave.ca/jerichobch.html?site=20"
THRESHOLD = 10.0  # knots
HEADLESS = True
MAX_WAIT = 40  # seconds
DEBUG_DIR = Path("debug")
DEBUG_DIR.mkdir(exist_ok=True)

# --------------- Utilities ---------------
def save_artifacts(driver, name: str):
    try:
        driver.save_screenshot(str(DEBUG_DIR / f"{name}.png"))
    except Exception:
        pass
    try:
        (DEBUG_DIR / f"{name}.html").write_text(driver.page_source, encoding="utf-8")
    except Exception:
        pass

def build_driver():
    opts = Options()
    if HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1400,1200")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)

def get_model2_values(driver):
    try:
        html = driver.page_source
        m = re.search(r"model2\s*=\s*\[([^\]]+)\]", html, flags=re.IGNORECASE | re.DOTALL)
        if m:
            nums = re.findall(r"-?\d+(?:\.\d+)?", m.group(1))
            return [float(x) for x in nums]
    except Exception:
        pass
    return []

def click_next_day(driver):
    try:
        wait = WebDriverWait(driver, MAX_WAIT)
        btn = wait.until(EC.element_to_be_clickable((By.ID, "NextButton")))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.5)
        try:
            btn.click()
            return True
        except ElementClickInterceptedException:
            driver.execute_script("arguments[0].click();", btn)
            return True
    except Exception as e:
        print("❌ Failed to click 'Next Day' button using Selenium:", e)
        save_artifacts(driver, "click_failed")
        return False

def send_email(subject: str, body: str):
    sendgrid_api_key = os.getenv("SENDGRID_API_KEY")
    alert_email = os.getenv("ALERT_EMAIL")
    if not sendgrid_api_key or not alert_email:
        print("❌ Missing SENDGRID_API_KEY or ALERT_EMAIL environment variables.")
        return

    message = Mail(
        from_email='alert@windforecast.com',
        to_emails=alert_email,
        subject=subject,
        html_content=f"<p>{body.replace(chr(10), '<br>')}</p>"
    )

    try:
        sg = SendGridAPIClient(sendgrid_api_key)
        response = sg.send(message)
        print(f"✅ Alert sent via SendGrid. Status code: {response.status_code}")
    except Exception as e:
        print("❌ SendGrid email failed:", e)

# --------------- Main Flow ---------------
def main():
    try:
        driver = build_driver()
    except WebDriverException as e:
        print("❌ Could not launch Chrome WebDriver. Is Chrome installed?")
        print(e)
        return

    try:
        driver.get(URL)
        WebDriverWait(driver, MAX_WAIT).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(2)
        save_artifacts(driver, "loaded_root")

        before_vals = get_model2_values(driver)

        if not click_next_day(driver):
            print("⚠️ Could not click 'Next Day' button.")
        time.sleep(5)

        after_vals = get_model2_values(driver)
        if not after_vals or after_vals == before_vals:
            print("⚠️ Could not find Model 2 data after attempting 'Next Day'.")
            save_artifacts(driver, "no_model2_after")
            return

        model2_max = max(after_vals)
        print(f"ℹ️ Model 2 points: {len(after_vals)}  |  Max: {model2_max:.2f} kn")

        if model2_max > THRESHOLD:
            print(f"🚨 ALERT: {model2_max:.2f} kn > {THRESHOLD:.2f} kn")
            body = (
                f"Model 2 next-day forecast exceeds {THRESHOLD} knots.\n"
                f"Max observed: {model2_max:.2f} kn.\n\nLink: {URL}"
            )
            send_email("Wind Alert: Model 2 exceeds threshold", body)
        else:
            print(f"✅ No alert. Max {model2_max:.2f} kn ≤
