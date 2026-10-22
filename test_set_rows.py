"""ทดสอบอ่านแถวบทเรียนจากหน้า /course จริง (โครงสร้างใหม่ของ SET)

หน้าเว็บรุ่นใหม่ไม่ได้ใช้ progress bar width อย่างเดียว แต่ใช้:
  - class "done" แปลว่าผ่าน 100%
  - title/data-original-title = "100%" หรือ "40%"
  - บทที่กำลังเปิดอยู่ href="javascript:void(0);" ต้องอ่าน topics_id
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

import set_bot as bot

fixture = Path("testdata_course_rows.html").resolve()
assert fixture.exists()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(fixture.as_uri())
    page.wait_for_timeout(100)
    payload = page.evaluate(bot.COURSE_TOPIC_ROWS_JS)
    rows = bot.topic_rows_from_payload(payload)
    browser.close()

assert set(rows) == {"1", "2", "3", "4"}, rows
assert rows["1"]["progress"] == 100, rows["1"]  # active + done
assert rows["2"]["progress"] == 100, rows["2"]  # done
assert rows["3"]["progress"] == 40, rows["3"]   # icon title 40%
assert rows["4"]["progress"] == 40, rows["4"]   # active, line height 40%
assert all(r["unlocked"] for r in rows.values()), rows
assert rows["1"]["title"].startswith("แนะนำหลักสูตร"), rows["1"]

print("✅ อ่านแถวบทเรียนโครงสร้างใหม่ได้ครบ:", {
    k: v["progress"] for k, v in rows.items()
})
print("✅ บทที่กำลังเปิดอยู่ (href=javascript:void(0)) อ่านจาก topics_id ได้")
print("\n✅ ผ่านทั้งหมด")
