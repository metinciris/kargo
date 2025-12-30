# yurtici_v4.py
# Yurtiçi Kargo takip (CLI): YOLDA / VARDI / TESLIM (+ NOT_FOUND)
#
# Bu sürüm, Selenium'da "data-step" ve "shipmentStatus" JS ile dolana kadar BEKLER.
# (Sende görülen step=0 / UNKNOWN sorunu genelde sayfayı çok erken okumaktan oluyor.)
#
# Kurulum:
#   pip install requests beautifulsoup4 playsound==1.2.2 pyttsx3
#   pip install selenium
#   (gerekirse) pip install webdriver-manager
#
# Kullanım:
#   python yurtici_v4.py 609911008046 15 --target VARDI --stop --tts --mp3 "C:\Users\user\Documents\ok.mp3"
#   python yurtici_v4.py   (argümansız çalıştırırsan sorarak ister)

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

try:
    from playsound import playsound
except Exception:
    playsound = None

try:
    import pyttsx3
except Exception:
    pyttsx3 = None


TRACK_URL = "https://www.yurticikargo.com/tr/online-servisler/gonderi-sorgula?code={code}"
BASE_URL = "https://www.yurticikargo.com/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)

DEFAULT_MP3 = r"C:\Users\user\Documents\ok.mp3"


def norm_tr(s: str) -> str:
    s = (s or "").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("İ", "I").replace("ı", "I")
    s = " ".join(s.split())
    return s.upper()


@dataclass(frozen=True)
class Snapshot:
    status: str
    step: Optional[int]
    not_found: bool = False

    def key(self) -> str:
        return f"{self.status}|{self.step if self.step is not None else ''}|NF{int(self.not_found)}"


def extract_snapshot_requests(html: str) -> Snapshot:
    soup = BeautifulSoup(html, "html.parser")

    status_el = soup.select_one("#shipmentStatus")
    status = status_el.get_text(" ", strip=True) if status_el else ""

    # step: #four-pack.info-graphic[data-step]
    step = None
    four = soup.select_one("#four-pack[data-step]")
    if four and four.has_attr("data-step"):
        try:
            step = int(str(four["data-step"]).strip())
        except Exception:
            step = None

    # not found modal/message
    txt = norm_tr(soup.get_text(" ", strip=True))
    not_found = ("KARGO BULUNAMADI" in txt) or ("GONDERI KODUYLA BIR KARGO BULUNAMADI" in txt) or ("BIR KARGO BULUNAMADI" in txt)

    return Snapshot(status=status, step=step, not_found=not_found)


def classify(snap: Snapshot) -> str:
    if snap.not_found:
        return "NOT_FOUND"

    st = norm_tr(snap.status)

    # 1) shipmentStatus doğrudan
    if "TESLIM EDILDI" in st or "TESLİM EDİLDİ" in st:
        return "TESLIM"
    if "VARIS BIRIMINDE" in st or "VARIŞ BİRİMİNDE" in st:
        return "VARDI"
    if any(k in st for k in ["TASIMA", "TAŞIMA", "TRANSFER", "SEVK", "YOLDA"]):
        return "YOLDA"

    # 2) step fallback: 2/3/4
    if snap.step is not None:
        if snap.step >= 4:
            return "TESLIM"
        if snap.step >= 3:
            return "VARDI"
        if snap.step >= 2:
            return "YOLDA"

    return "UNKNOWN"


def looks_unreadable(snap: Snapshot, html: str) -> bool:
    s = (snap.status or "").strip()
    if snap.not_found:
        return False
    if not s or set(s) <= {"*"}:
        return True
    if snap.step is None or snap.step == 0:
        # step gelmiyorsa ve sayfada four-pack yoksa şüpheli
        if "four-pack" not in html and "shipmentStatus" not in html:
            return True
    return False


class Speaker:
    def __init__(self):
        self.engine = None
        self.lock = threading.Lock()

    def say(self, text: str):
        if not text or pyttsx3 is None:
            return
        with self.lock:
            if self.engine is None:
                self.engine = pyttsx3.init()
            try:
                self.engine.say(text)
                self.engine.runAndWait()
            except Exception:
                pass


def play_mp3(path: str):
    if not path or playsound is None:
        return
    if not os.path.exists(path):
        return
    try:
        playsound(path)
    except Exception:
        pass


def fetch_requests(sess: requests.Session, code: str, timeout: int = 20) -> str:
    # Cookie al
    try:
        sess.get(BASE_URL, headers={"User-Agent": UA}, timeout=timeout)
    except Exception:
        pass

    url = TRACK_URL.format(code=code.strip())
    r = sess.get(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": BASE_URL,
        },
        timeout=timeout,
        allow_redirects=True,
    )
    r.raise_for_status()
    return r.text


def _make_driver():
    # Selenium Manager (varsayılan) -> olmazsa webdriver-manager fallback
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    # headless (Chrome 109+)
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument(f"--user-agent={UA}")
    opts.add_argument("--window-size=1200,900")

    try:
        return webdriver.Chrome(options=opts)
    except Exception:
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            from selenium.webdriver.chrome.service import Service
            service = Service(ChromeDriverManager().install())
            return webdriver.Chrome(service=service, options=opts)
        except Exception as e:
            raise RuntimeError(f"Chrome WebDriver başlatılamadı: {e}")


def fetch_selenium(code: str, driver, wait_s: int = 25) -> tuple[Snapshot, str]:
    """
    JS dolana kadar bekler:
      - #four-pack data-step 2/3/4
      - #shipmentStatus boş değil
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    url = TRACK_URL.format(code=code.strip())
    driver.get(url)

    # 1) sayfa hazır
    WebDriverWait(driver, wait_s).until(lambda d: d.execute_script("return document.readyState") == "complete")

    # 2) data-step 2/3/4 bekle (yolda/vardı/teslim)
    def step_ok(d):
        try:
            step = d.execute_script("return document.getElementById('four-pack')?.getAttribute('data-step') || ''")
            return str(step).strip() in ("2", "3", "4")
        except Exception:
            return False

    # 3) status metni bekle (yıldız/boş değil)
    def status_ok(d):
        try:
            s = d.execute_script("return document.getElementById('shipmentStatus')?.innerText || ''")
            s = (s or "").strip()
            if not s:
                return False
            if set(s) <= {"*"}:
                return False
            return True
        except Exception:
            return False

    # İkisini de beklemeyi dene; birisi gelmezse diğeriyle de ilerleyebiliriz.
    try:
        WebDriverWait(driver, wait_s).until(lambda d: step_ok(d) or status_ok(d))
    except Exception:
        pass

    # Çek
    status = driver.execute_script("return document.getElementById('shipmentStatus')?.innerText || ''") or ""
    step_raw = driver.execute_script("return document.getElementById('four-pack')?.getAttribute('data-step') || ''")
    try:
        step = int(str(step_raw).strip()) if str(step_raw).strip().isdigit() else None
    except Exception:
        step = None

    html = driver.execute_script("return document.documentElement.outerHTML") or ""

    # not found (modal) kontrolü
    txt = norm_tr(re.sub(r"<[^>]+>", " ", html))
    not_found = ("KARGO BULUNAMADI" in txt) or ("GONDERI KODUYLA BIR KARGO BULUNAMADI" in txt) or ("BIR KARGO BULUNAMADI" in txt)

    return Snapshot(status=status.strip(), step=step, not_found=not_found), html


def save_debug(html: str, tag: str, code: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"debug_{code}_{tag}_{ts}.html"
    with open(name, "w", encoding="utf-8") as f:
        f.write(html)
    return os.path.abspath(name)


def should_stop(current: str, target: str) -> bool:
    if target == "YOLDA":
        return False
    if target == "TESLIM":
        return current == "TESLIM"
    # VARDI hedefi: vardıda veya teslimde dur
    return current in ("VARDI", "TESLIM")


def log(line: str):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {line}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("code", nargs="?", help="Gönderi kodu")
    p.add_argument("interval", nargs="?", type=int, default=None, help="Saniye (örn 15)")
    p.add_argument("--mp3", default=DEFAULT_MP3, help="Durum değişince çalınacak mp3 yolu (boş bırakabilirsin)")
    p.add_argument("--tts", action="store_true", help="TTS konuş (yolda/vardı/teslim)")
    p.add_argument("--target", choices=["YOLDA", "VARDI", "TESLIM"], default="VARDI", help="Hedef durum")
    p.add_argument("--stop", action="store_true", help="Hedefe gelince durdur")
    p.add_argument("--mode", choices=["auto", "requests", "selenium"], default="auto", help="Çekme modu")
    p.add_argument("--debug", action="store_true", help="Okunamayan durumda HTML debug kaydet")
    return p.parse_args(argv)


def prompt_if_missing(ns: argparse.Namespace) -> argparse.Namespace:
    if ns.code:
        return ns

    print("Gönderi kodu gir (örn 60991100...): ", end="", flush=True)
    ns.code = input().strip()
    if not ns.code:
        print("Kod boş. Çıkılıyor.")
        sys.exit(1)

    print("Aralık (sn) [15]: ", end="", flush=True)
    iv = input().strip()
    ns.interval = int(iv) if iv else 15

    print("Hedef [VARDI/TESLIM/YOLDA] (default VARDI): ", end="", flush=True)
    tg = input().strip().upper()
    if tg in ("VARDI", "TESLIM", "YOLDA"):
        ns.target = tg

    print("Hedefe gelince durdur? [E/h] (default E): ", end="", flush=True)
    st = input().strip().lower()
    ns.stop = False if st == "h" else True

    print("MP3 yolu (enter = boş): ", end="", flush=True)
    mp = input().strip()
    ns.mp3 = mp  # boş olabilir

    print("TTS açılsın mı? [E/h] (default E): ", end="", flush=True)
    t = input().strip().lower()
    ns.tts = False if t == "h" else True

    ns.mode = "auto"
    ns.debug = True
    return ns


def main(argv: list[str]) -> int:
    ns = parse_args(argv)
    ns = prompt_if_missing(ns)

    interval = ns.interval if ns.interval is not None else 15
    interval = max(5, interval)

    speaker = Speaker() if ns.tts else None

    sess = requests.Session()
    driver = None

    log(f"Takip başladı: {ns.code} | aralık: {interval}s | hedef: {ns.target} | stop: {'E' if ns.stop else 'H'} | mode: {ns.mode}")

    last_key = None
    said_initial = False

    while True:
        try:
            snap = None
            html = ""

            # ---- requests ----
            if ns.mode in ("auto", "requests"):
                html = fetch_requests(sess, ns.code)
                snap = extract_snapshot_requests(html)

            # unreadable -> selenium
            if ns.mode == "selenium" or (ns.mode == "auto" and (snap is None or looks_unreadable(snap, html))):
                if ns.mode == "auto":
                    log("(auto) Selenium moduna geçildi (JS ile dolan sayfa tespit edildi).")
                if driver is None:
                    driver = _make_driver()

                try:
                    snap, html = fetch_selenium(ns.code, driver)
                except Exception as e:
                    # invalid session id vs -> driver reset
                    if "invalid session id" in str(e).lower():
                        try:
                            driver.quit()
                        except Exception:
                            pass
                        driver = _make_driver()
                        snap, html = fetch_selenium(ns.code, driver)
                    else:
                        raise

                if looks_unreadable(snap, html) and ns.debug:
                    p = save_debug(html, "selenium", ns.code)
                    log(f"UYARI: Selenium ile de status/step okunamadı. Debug: {p}")

            if snap is None:
                log("HATA: Snapshot üretilemedi.")
                time.sleep(interval)
                continue

            state = classify(snap)

            # NOT_FOUND -> durdur
            if state == "NOT_FOUND":
                log("❌ KARGO BULUNAMADI (NOT_FOUND). Durduruldu.")
                break

            # log
            if last_key is None:
                last_key = snap.key()
                log(f"İlk durum: {state} ({snap.status or '-'}) step={snap.step if snap.step is not None else '-'}")
                if speaker and not said_initial:
                    word = "teslim" if state == "TESLIM" else ("vardı" if state == "VARDI" else "yolda")
                    threading.Thread(target=speaker.say, args=(word,), daemon=True).start()
                    said_initial = True

                if ns.stop and should_stop(state, ns.target):
                    if ns.mp3:
                        threading.Thread(target=play_mp3, args=(ns.mp3,), daemon=True).start()
                    if speaker and state in ("VARDI", "TESLIM"):
                        threading.Thread(target=speaker.say, args=("teslim" if state=="TESLIM" else "vardı",), daemon=True).start()
                    log(f"Hedef zaten sağlandı ({ns.target}). Durduruldu.")
                    break
            else:
                if snap.key() != last_key:
                    last_key = snap.key()
                    log(f"Durum değişti: {state} ({snap.status or '-'}) step={snap.step if snap.step is not None else '-'}")

                    if ns.mp3:
                        threading.Thread(target=play_mp3, args=(ns.mp3,), daemon=True).start()

                    if speaker:
                        if state == "VARDI":
                            threading.Thread(target=speaker.say, args=("vardı",), daemon=True).start()
                        elif state == "TESLIM":
                            threading.Thread(target=speaker.say, args=("teslim",), daemon=True).start()

                    if ns.stop and should_stop(state, ns.target):
                        log(f"Hedefe ulaştı ({ns.target}). Durduruldu.")
                        break
                else:
                    log(f"Durum aynı: {state} ({snap.status or '-'}) step={snap.step if snap.step is not None else '-'}")

        except KeyboardInterrupt:
            log("Durduruldu (Ctrl+C).")
            break
        except Exception as e:
            log(f"HATA: {e}")

        time.sleep(interval)

    try:
        if driver is not None:
            driver.quit()
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
