"""ทดสอบ: กด Show answer -> อ่านเฉลย -> เลือกใหม่ -> submit (ข้อที่ตอบผิด)"""
import json, time
from pathlib import Path
from playwright.sync_api import sync_playwright
import quiz_solver as bot

CID = "course-v1:TU+00012+0"
# บทที่ 2 (มีข้อผิด/ค้างอยู่)
URL = ("https://learn.thaimooc.ac.th/xblock/"
       "block-v1:TU+00012+0+type@vertical+block@6cf7c854af39408d8d383b8e74b730eb")
PROFILE = str(Path(__file__).parent / "browser_profile")

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(user_data_dir=PROFILE, headless=False)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    submits = []
    page.on("response", lambda r: submits.append((r.status, r.url)) if "handler/submit" in r.url else None)

    page.goto(URL, wait_until="domcontentloaded", timeout=120000)
    time.sleep(5)

    state = page.evaluate(
        """() => [...document.querySelectorAll('.problem')].map((p,i) => ({
            i,
            status: (p.querySelector('.status')||{}).className || null,
            group: (p.querySelector('input[type=radio]')||{}).name || null,
            buttons: [...p.querySelectorAll('button')].filter(b=>b.offsetParent).map(b=>(b.innerText||'').trim()),
        }))"""
    )
    print("STATE:", json.dumps(state, ensure_ascii=False, indent=1))

    # เอาข้อแรกที่ไม่ใช่ correct
    idx = next((s["i"] for s in state if s["status"] and "correct" in s["status"] and "incorrect" not in s["status"]) if False else None, None)
    idx = next((s["i"] for s in state if not (s["status"] or "").split().__contains__("correct")), 0)
    print("ทดสอบข้อ index:", idx, "status:", state[idx]["status"])

    block = page.locator(".problem").nth(idx)

    # 1) กด Show answer ถ้ามี
    show = block.locator("button:has-text('Show answer'), button.show")
    if show.count():
        show.first.click(force=True)
        print("กด Show answer แล้ว")
        time.sleep(3)
    else:
        print("ไม่มีปุ่ม Show answer")

    # 2) อ่านคำตอบที่ถูก
    info = block.evaluate(
        """p => ({
            correctLabels: [...p.querySelectorAll('label.choicegroup_correct')].map(l => l.innerText.replace(/\\s+/g,' ').trim()),
            status: (p.querySelector('.status')||{}).className,
            buttons: [...p.querySelectorAll('button')].filter(b=>b.offsetParent).map(b=>(b.innerText||'').trim()),
            notices: [...p.querySelectorAll('.notification, .problem-progress, .feedback')].map(n=>n.innerText.trim()).slice(0,4),
        })"""
    )
    print("หลัง Show answer:", json.dumps(info, ensure_ascii=False, indent=1))

    # 3) เลือกคำตอบที่ถูกแล้ว submit
    if info["correctLabels"]:
        target = info["correctLabels"][0]
        clicked = block.evaluate(
            """(p, target) => {
                const labels = [...p.querySelectorAll('label')];
                const lab = labels.find(l => l.innerText.replace(/\\s+/g,' ').trim() === target);
                if (!lab) return false;
                lab.click();
                return true;
            }""",
            target,
        )
        print("คลิกเฉลย:", clicked, target[:60])
        time.sleep(1)
        btn = block.locator("button:has-text('Submit'), button.submit").first
        btn.click(force=True)
        for i in range(20):
            time.sleep(1.5)
            st = block.evaluate("p => (p.querySelector('.status')||{}).className || ''")
            if "correct" in st.split() or "incorrect" in st.split():
                print(f"ผลหลัง {i*1.5:.0f}s:", st)
                break
        else:
            print("ยังไม่เห็นผล")

    print("\nsubmit responses:", submits[-5:])
    ctx.close()
