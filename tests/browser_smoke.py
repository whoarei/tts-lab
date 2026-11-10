"""Optional local UI test: pip install playwright; playwright install chromium.

Start run.py separately. Only uses the offline tone. If a configured real API
provider is selected by default, explicitly replace it with demo before submit.
Set CHROMIUM_PATH if using a system Chromium binary.
"""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    launch = {"headless": True}
    if os.getenv("CHROMIUM_PATH"):
        launch["executable_path"] = os.environ["CHROMIUM_PATH"]
    browser = p.chromium.launch(**launch)
    page = browser.new_page(viewport={"width":1440,"height":1100})
    errors=[]
    page.on("pageerror",lambda e: errors.append(str(e)))
    page.goto("http://127.0.0.1:8765")
    page.wait_for_selector(".variant")
    while page.locator(".variant").count() < 2:
        page.click("#add-variant")
    for selector in page.locator(".variant .provider").all():
        selector.select_option("demo")
    page.screenshot(path="/tmp/tts-lab-desktop.png",full_page=True)
    page.click("#generate")
    page.wait_for_function("document.querySelectorAll('audio').length === 2")
    assert page.locator(".result-card").count()==2
    assert page.locator(".result-card").filter(has_text="测试音").count()==2
    page.locator("audio").first.evaluate("(a)=>a.play()")
    page.wait_for_timeout(600)
    assert page.locator("audio").first.evaluate("(a)=>a.currentTime")>0
    page.uncheck("#blind")
    assert page.locator(".download").count()==2
    with page.expect_download() as info:
        page.click("#download-batch")
    info.value.save_as("/tmp/tts-browser-batch.zip")
    page.click("#refresh-history")
    page.wait_for_selector(".history-row")
    assert page.locator(".history-row").count()>=1
    page.click("#close-history")
    page.click("#add-variant")
    assert page.locator(".variant").count()==3
    page.locator(".variant .icon-button").last.click()
    assert page.locator(".variant").count()==2
    page.fill("#text","")
    page.click("#generate")
    assert "输入" in page.locator("#notice").inner_text()
    page.screenshot(path="/tmp/tts-lab-desktop-results.png",full_page=True)
    page.set_viewport_size({"width":390,"height":844})
    page.goto("http://127.0.0.1:8765")
    page.wait_for_selector(".variant")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path="/tmp/tts-lab-mobile.png",full_page=True)
    assert not errors, errors
    print("UI smoke passed: desktop/mobile, demo audio playback, blind reveal, ZIP, history, add/remove, empty input; no JS errors.")
    browser.close()
