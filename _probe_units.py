"""ทดสอบจริง: ตอบ+ส่งทีละข้อ ในหน่วยบทที่ 2 (หลังแก้ click_option)"""
import json, time
from pathlib import Path
from playwright.sync_api import sync_playwright
import quiz_solver as bot

CID = "course-v1:TU+00012+0"
URL = ("https://learn.thaimooc.ac.th/xblock/"
       "block-v1:TU+00012+0+type@vertical+block@6cf7c854af39408d8d383b8e74b730eb")
PROFILE = str(Path(__file__).parent / "browser_profile")

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(user_data_dir=PROFILE, headless=False)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=120000)
    time.sleep(4)

    profile = bot.PROFILES["thaimooc"]
    res = bot.solve_page_one_by_one(page, profile)
    print("\nRESULT:", res)

    time.sleep(2)
    st = page.evaluate(
        """() => [...document.querySelectorAll('.problem')].map(p => ({
            status: (p.querySelector('.status')||{}).className || '',
            legend: ((p.querySelector('legend')||{}).innerText||'').slice(0,45),
            checked: [...p.querySelectorAll('input[type=radio]')].filter(r=>r.checked).map(r=>{
                const l = p.querySelector('label[for="'+r.id+'"]');
                return l ? l.innerText.replace(/\\s+/g,' ').trim().slice(0,45) : r.value;
            }),
        }))"""
    )
    print(json.dumps(st, ensure_ascii=False, indent=1))
    ctx.close()
