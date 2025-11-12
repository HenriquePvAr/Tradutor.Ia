import os, time, base64, requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from config import CHROMEDRIVER_PATH, TEMP_FOLDER

def download_images(url, progress_callback=None, max_retries=3):
    tmp_files, failed_candidates = [], []
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    driver = webdriver.Chrome(service=Service(CHROMEDRIVER_PATH), options=chrome_options)

    driver.get(url)
    time.sleep(3)

    last_height = driver.execute_script("return document.body.scrollHeight")
    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height

    imgs = driver.find_elements(By.TAG_NAME, "img")
    candidates = []
    for img in imgs:
        try:
            w, h = img.size.get("width", 0), img.size.get("height", 0)
            loc = img.location
            src = img.get_attribute("src") or ""
            if w >= 200 and h >= 200:
                candidates.append({"el": img, "y": loc.get("y", 0), "src": src})
        except:
            pass
    candidates = sorted(candidates, key=lambda x: x["y"])

    fetch_blob_script = """
    var img = arguments[0];
    var callback = arguments[1];
    var url = img.src;
    fetch(url).then(r => r.blob()).then(function(b){
        var reader = new FileReader();
        reader.onloadend = function() { callback(reader.result.split(',')[1]); };
        reader.readAsDataURL(b);
    }).catch(function(e){ callback(null); });
    """

    def save_bytes(bts, path):
        with open(path, "wb") as f:
            f.write(bts)

    def try_save(el, src, path):
        saved = False
        if src.startswith("http"):
            try:
                r = requests.get(src, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
                if r.status_code == 200:
                    save_bytes(r.content, path)
                    saved = True
            except: pass
        if not saved:
            try:
                b64 = driver.execute_async_script(fetch_blob_script, el)
                if b64:
                    save_bytes(base64.b64decode(b64), path)
                    saved = True
            except: pass
        if not saved:
            try:
                el.screenshot(path)
                saved = True
            except: pass
        return saved

    for i, c in enumerate(candidates, start=1):
        path = os.path.join(TEMP_FOLDER, f"{i:03}.png")
        if try_save(c["el"], c["src"], path):
            tmp_files.append(path)
        else:
            failed_candidates.append((i, c))
        if progress_callback:
            progress_callback(i, len(candidates), "Baixando imagens")

    driver.quit()
    return sorted(tmp_files)
