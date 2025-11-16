import os
import time
import base64
import requests
import shutil
import stat
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from config import CHROMEDRIVER_PATH, TEMP_FOLDER


# -------------------------------------------------------------
# 🔧 Função segura para remover pasta no Windows (sem erro 5)
# -------------------------------------------------------------
def force_remove(path):
    if not os.path.exists(path):
        return

    def remove_readonly(func, path, _):
        os.chmod(path, stat.S_IWRITE)
        func(path)

    shutil.rmtree(path, onerror=remove_readonly)


def download_images(url, progress_callback=None, max_retries=3):

    # ---------------------------------------------------------
    # 🔥 Limpa a pasta TEMP_FOLDER com método seguro
    # ---------------------------------------------------------
    if os.path.exists(TEMP_FOLDER):
        force_remove(TEMP_FOLDER)

    os.makedirs(TEMP_FOLDER, exist_ok=True)

    tmp_files = []
    failed_candidates = []

    # ---------------------------------------------------------
    # 🔧 Configurações do Chrome (estável)
    # ---------------------------------------------------------
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--log-level=3")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)

    if not os.path.isfile(CHROMEDRIVER_PATH):
        raise FileNotFoundError(f"❌ CHROMEDRIVER_PATH inválido:\n{CHROMEDRIVER_PATH}")

    driver = webdriver.Chrome(
        service=Service(CHROMEDRIVER_PATH),
        options=chrome_options
    )

    # ---------------------------------------------------------
    # 🌐 Carrega a página
    # ---------------------------------------------------------
    driver.get(url)
    time.sleep(3)

    # ---------------------------------------------------------
    # 📜 Scroll infinito
    # ---------------------------------------------------------
    last_height = driver.execute_script("return document.body.scrollHeight")
    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.5)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height

    # ---------------------------------------------------------
    # 🎯 Coleta imagens
    # ---------------------------------------------------------
    imgs = driver.find_elements(By.TAG_NAME, "img")
    candidates = []

    for img in imgs:
        try:
            w = img.size.get("width", 0)
            h = img.size.get("height", 0)
            y = img.location.get("y", 0)

            # Pega src ou data-src
            src = (
                img.get_attribute("src")
                or img.get_attribute("data-src")
                or img.get_attribute("data-lazy-src")
                or ""
            )

            if w >= 200 and h >= 200:
                candidates.append({"el": img, "src": src, "y": y})

        except:
            pass

    candidates = sorted(candidates, key=lambda x: x["y"])

    # ---------------------------------------------------------
    # 🧪 Função JS para blob via fetch()
    # ---------------------------------------------------------
    fetch_blob_script = """
        var img = arguments[0];
        var callback = arguments[1];
        fetch(img.src)
            .then(r => r.blob())
            .then(b => {
                var reader = new FileReader();
                reader.onloadend = () =>
                    callback(reader.result.split(',')[1]);
                reader.readAsDataURL(b);
            })
            .catch(() => callback(null));
    """

    def save_bytes(data, path):
        with open(path, "wb") as f:
            f.write(data)

    # ---------------------------------------------------------
    # 📥 Função de download com retry
    # ---------------------------------------------------------
    def try_save(el, src, path):

        # 1) Tenta baixar direto via HTTP
        if src.startswith("http"):
            for attempt in range(max_retries):
                try:
                    r = requests.get(src, timeout=10, headers={
                        "User-Agent": "Mozilla/5.0"
                    })
                    if r.status_code == 200:
                        save_bytes(r.content, path)
                        return True
                except:
                    pass

        # 2) Blob via navegador
        try:
            b64 = driver.execute_async_script(fetch_blob_script, el)
            if b64:
                save_bytes(base64.b64decode(b64), path)
                return True
        except:
            pass

        # 3) Screenshot
        try:
            el.screenshot(path)
            return True
        except:
            pass

        return False

    # ---------------------------------------------------------
    # 📥 Baixa as imagens
    # ---------------------------------------------------------
    for i, c in enumerate(candidates, start=1):
        file_path = os.path.join(TEMP_FOLDER, f"{i:03}.png")

        ok = try_save(c["el"], c["src"], file_path)

        if ok:
            tmp_files.append(file_path)
        else:
            failed_candidates.append((i, c))

        if progress_callback:
            progress_callback(i, len(candidates), "Baixando imagens")

    driver.quit()
    return sorted(tmp_files)
