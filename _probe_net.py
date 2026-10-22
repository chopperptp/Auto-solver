import time
from pathlib import Path
from playwright.sync_api import sync_playwright

PROFILE = str(Path(__file__).parent / "browser_profile")
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(user_data_dir=PROFILE, headless=False)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    for url in ("https://thaimooc.ac.th/", "https://learn.thaimooc.ac.th/courses/course-v1:TU+00012+0/about"):
        t0 = time.time()
        try:
            r = page.goto(url, wait_until="domcontentloaded", timeout=90000)
            print(f"OK {url} status={r.status if r else None} {time.time()-t0:.1f}s title={page.title()[:60]}")
        except Exception as e:
            print(f"ERR {url} {time.time()-t0:.1f}s {type(e).__name__}: {str(e).splitlines()[0]}")
            try:
                print("   current url:", page.url)
            except Exception:
                pass
    ctx.close()
