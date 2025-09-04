import time, re, sys, traceback
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, ElementClickInterceptedException, WebDriverException
)
from webdriver_manager.chrome import ChromeDriverManager
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
import os

# --------------- CONFIG ---------------
URL        = "https://bigwavedave.ca/jerichobch.html?site=20"
THRESHOLD  = 10.0               # knots
HEADLESS   = True               # set True once it's working
MAX_WAIT   = 40                 # increased from 20 to 40 seconds

EMAIL_TO   = os.getenv("ALERT_EMAIL")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")

DEBUG_DIR = Path("debug"); DEBUG_DIR.mkdir(exist_ok=True)

# --------------- Utilities ---------------
def send_email(subject: str, body: str):
    message = Mail(
        from_email='alert@windforecast.com',
        to_emails=EMAIL_TO,
        subject=subject,
        html_content=f"<p>{body.replace(chr(10), '<br>')}</p>"
    )
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        print(f"✅ Alert email sent via SendGrid. Status code: {response.status_code}")
    except Exception as e:
        print("❌ Email send failed:", e)

def save_artifacts(driver, name: str):
    try: driver.save_screenshot(str(DEBUG_DIR / f"{name}.png"))
    except: pass
    try:
        (DEBUG_DIR / f"{name}.html").write_text(driver.page_source, encoding="utf-8")
    except: pass

def build_driver():
    opts = Options()
    if HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1400,1200")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)

# ---------------- Data extraction ----------------
def get_model2_values(driver):
    try:
        vals = driver.execute_script("""
            if (window.Highcharts && Highcharts.charts) {
              const out = [];
              for (const c of Highcharts.charts) {
                if (!c || !c.series) continue;
                for (const s of c.series) {
                  const nm = (s && s.name) ? String(s.name) : '';
                  if (/model\\s*2/i.test(nm)) {
                    if (Array.isArray(s.yData)) out.push(...s.yData);
                    else if (Array.isArray(s.options?.data)) {
                      for (const d of s.options.data) {
                        if (Array.isArray(d)) out.push(d[1]);
                        else if (typeof d === 'number') out.push(d);
                        else if (d && typeof d.y === 'number') out.push(d.y);
                      }
                    }
                  }
                }
              }
              return out;
            }
            return null;
        """)
        if vals and any(isinstance(x,(int,float)) for x in vals):
            return [float(x) for x in vals if isinstance(x,(int,float))]
    except Exception:
        pass

    try:
        arr = driver.execute_script("""
            if (typeof model2 !== 'undefined' && Array.isArray(model2)) return model2;
            if (typeof window !== 'undefined' && Array.isArray(window.model2)) return window.model2;
            return null;
        """)
        if arr and isinstance(arr, list):
            return [float(x) for x in arr if isinstance(x,(int,float))]
    except Exception:
        pass

    try:
        html = driver.page_source
        m = re.search(r"model2\s*=\s*\[([^\]]+)\]", html, flags=re.IGNORECASE | re.DOTALL)
        if m:
            nums = re.findall(r"-?\d+(?:\.\d+)?", m.group(1))
            return [float(x) for x in nums]
    except Exception:
        pass

    return []

def wait_for_model2_change(driver, old_values, timeout=MAX_WAIT):
    end = time.time() + timeout
    while time.time() < end:
        vals = get_model2_values(driver)
        if vals and vals != old_values:
            return vals
        time.sleep(0.5)
    return get_model2_values(driver)

# ---------------- Button click ----------------
def click_next_day(driver):
    wait = WebDriverWait(driver, MAX_WAIT)
    try:
        btn = wait.until(EC.element_to_be_clickable((By.ID, "NextButton")))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.2)
        try:
            btn.click()
            return True
        except ElementClickInterceptedException:
            driver.execute_script("arguments[0].click();", btn)
            return True
    except Exception:
        pass

    candidates = [
        (By.CSS_SELECTOR, "button#NextButton"),
        (By.CSS_SELECTOR, "button.button.smallbutton[title='Next day']"),
        (By.XPATH, "//button[@id='NextButton' or @title='Next day']"),
    ]
    for by, sel in candidates:
        try:
            el = wait.until(EC.element_to_be_clickable((by, sel)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(0.2)
            try:
                el.click()
                return True
            except ElementClickInterceptedException:
                driver.execute_script("arguments[0].click();", el)
                return True
        except Exception:
            continue

    try:
        driver.execute_script("if (typeof ChangeDate === 'function') ChangeDate(1);")
        return True
    except Exception:
        return False

# ---------------- Main flow ----------------
def main():
    try:
        driver = build_driver()
    except WebDriverException as e:
        print("❌ Could not launch Chrome WebDriver. Is Chrome installed?")
        print(e); sys.exit(1)

    try:
        driver.get(URL)
        WebDriverWait(driver, MAX_WAIT).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(1.5)
        save_artifacts(driver, "loaded_root")

        before_vals = get_model2_values(driver)

        if not click_next_day(driver):
            print("⚠️ Could not click 'Next Day' via id/selectors; tried ChangeDate(1) as fallback.")
        time.sleep(5)  # Added sleep to allow chart to load

        after_vals = wait_for_model2_change(driver, before_vals, timeout=MAX_WAIT)
        if not after_vals:
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
            try:
                send_email("Wind Alert: Model 2 exceeds threshold", body)
            except Exception as e:
                print("❌ Email send failed:", e)
        else:
            print(f"✅ No alert. Max {model2_max:.2f} kn ≤ {THRESHOLD:.2f} kn")

    except Exception as e:
        print("❌ Unhandled error:", e)
        traceback.print_exc()
        save_artifacts(driver, "exception")
    finally:
        driver.quit()

if __name__ == "__main__":
    main()


