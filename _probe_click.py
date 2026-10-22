"""ตรวจสอบ click_option: เลือก index ที่กำหนด แล้วดูว่า radio ตัวไหนถูกเลือกจริง"""
import json, time
from pathlib import Path
from playwright.sync_api import sync_playwright
import quiz_solver as bot

URL = ("https://learn.thaimooc.ac.th/xblock/"
       "block-v1:TU+00012+0+type@vertical+block@6cf7c854af39408d8d383b8e74b730eb")
PROFILE = str(Path(__file__).parent / "browser_profile")

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(user_data_dir=PROFILE, headless=False)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=120000)
    time.sleep(5)

    profile = bot.PROFILES["thaimooc"]
    qs = bot.extract_questions(page, profile)
    if not qs:
        print("ไม่มีข้อให้ทดสอบ")
        ctx.close()
        raise SystemExit

    q = qs[-1]   # ข้อที่ยัง unanswered
    print("โจทย์:", q.text[:80])
    for i, o in enumerate(q.options):
        print(f"  [{i}] {o[:60]}  (value={q.values[i]})")

    # ทดสอบคลิก index สุดท้าย
    idx = len(q.options) - 1
    print(f"\nลองคลิก index {idx} -> {q.options[idx][:50]}")
    ok = bot.click_option(page, q, idx, profile.get("option_css"))
    time.sleep(0.8)

    checked = q.container.evaluate(
        """p => [...p.querySelectorAll('input[type=radio]')].filter(r=>r.checked).map(r => {
            const lab = p.querySelector('label[for="'+r.id+'"]');
            return r.value + ' | ' + (lab ? lab.innerText.replace(/\\s+/g,' ').trim().slice(0,50) : '?');
        })"""
    )
    print("click_option คืนค่า:", ok)
    print("checked จริง:", checked)
    print("คาดหวัง:", q.values[idx])

    # ตรวจว่ามี label ครอบ radio ไหม
    inner = q.container.evaluate(
        """p => [...p.querySelectorAll('input[type=radio]')].slice(0,2).map(r => ({
            value: r.value,
            parentTag: r.parentElement.tagName,
            parentCls: r.parentElement.className,
            insideLabel: !!r.closest('label'),
            nextSibling: r.nextElementSibling ? r.nextElementSibling.tagName + '.' + r.nextElementSibling.className : null,
        }))"""
    )
    print(json.dumps(inner, ensure_ascii=False, indent=1))
    ctx.close()
