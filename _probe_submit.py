"""ตรวจสอบการ submit ที่ไม่ขึ้นผล: ดูว่า radio ถูกเลือกจริงไหม + POST ไปไหม"""
import json, time
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = ("https://learn.thaimooc.ac.th/xblock/"
       "block-v1:TU+00012+0+type@vertical+block@6cf7c854af39408d8d383b8e74b730eb")
PROFILE = str(Path(__file__).parent / "browser_profile")

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(user_data_dir=PROFILE, headless=False)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    net = []
    page.on("request", lambda r: net.append(("REQ", r.method, r.url[:120])) if "handler" in r.url else None)
    page.on("response", lambda r: net.append(("RES", r.status, r.url[:120])) if "handler" in r.url else None)
    page.on("console", lambda m: net.append(("CONSOLE", m.type, m.text[:160])))

    page.goto(URL, wait_until="domcontentloaded", timeout=120000)
    time.sleep(5)

    state = page.evaluate(
        """() => [...document.querySelectorAll('.problem')].map((p,i) => ({
            i, status: (p.querySelector('.status')||{}).className || null,
        }))"""
    )
    print("STATE:", state)
    unanswered = [s for s in state if "unanswered" in (s["status"] or "")]
    if not unanswered:
        print("ไม่มีข้อค้าง")
        ctx.close()
        raise SystemExit

    i = unanswered[0]["i"]
    block = page.locator(".problem").nth(i)
    # เลือกตัวเลือกแรก
    radios = block.locator("input[type=radio]")
    n = radios.count()
    print(f"ข้อ {i}: มี {n} ตัวเลือก; label แรก =", block.locator("label").first.inner_text().strip()[:60])
    block.locator("label").first.click(force=True)
    time.sleep(1)
    print("checked หลังคลิก:", block.evaluate("p => [...p.querySelectorAll('input[type=radio]')].filter(r=>r.checked).map(r=>r.value)"))

    btn = block.locator("button.submit, button:has-text('Submit')").first
    print("ปุ่ม:", btn.inner_text().strip(), "disabled:", btn.is_disabled(), "visible:", btn.is_visible())
    btn.click(force=True)
    print("คลิก Submit แล้ว รอผล...")
    for k in range(15):
        time.sleep(2)
        st = block.evaluate("p => (p.querySelector('.status')||{}).className || ''")
        if "correct" in st.split() or "incorrect" in st.split():
            print(f"ผลที่ {k*2+2}s: {st}")
            break
    else:
        st = block.evaluate("p => (p.querySelector('.status')||{}).className || ''")
        note = block.evaluate("p => [...p.querySelectorAll('.notification, .message, .problem-progress')].map(x=>x.innerText.trim())")
        print("ไม่เห็นผล, status =", st, "notes =", note)

    print("\nNETWORK:")
    for item in net[-15:]:
        print("  ", item)
    ctx.close()
