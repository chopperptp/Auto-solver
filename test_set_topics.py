"""ทดสอบการอ่านสถานะบทเรียน/รหัส enroll2topic (ไม่ต้องต่อเน็ต)"""
from pathlib import Path

import set_bot as bot

HERE = Path(__file__).parent

# 1) หน้า player จริงมีรหัสอยู่ใน var enroll2topic_id (จุดที่เคยหาไม่เจอ)
player = (HERE / "testdata_topic_player.html").read_text(encoding="utf-8")
assert bot.find_enroll2topic_id(player) == "143478875", bot.find_enroll2topic_id(player)
print("✅ อ่าน enroll2topic id จากหน้า player ได้:", bot.find_enroll2topic_id(player))

# 2) แถวบทเรียน: 100% = ผ่านแล้ว, 0% = ยังไม่ดู แต่เปิดได้
rows = [
    {
        "tid": "7942",
        "title": "แนะนำหลักสูตร",
        "href": "/SETGroup/enroll/26739471/topics/7942",
        "cls": "mr-1 has-access",
        "html": '<div class="progress"><div class="progress-bar" style="width: 100%"></div></div>',
    },
    {
        "tid": "7945",
        "title": "ตัวอย่างฟีเจอร์ของ Marketing Automation",
        "href": "/SETGroup/enroll/26739471/topics/7945",
        "cls": "mr-1 has-access",
        "html": '<div class="progress"><div class="progress-bar" style="width: 0%"></div></div>',
    },
]
parsed = bot.topic_rows_from_payload(rows)
assert parsed["7942"]["progress"] == 100
assert parsed["7942"]["unlocked"] is True
assert parsed["7945"]["progress"] == 0
assert parsed["7945"]["unlocked"] is True
print("✅ แยกบทที่ผ่านแล้ว/ยังไม่ผ่านได้:", {k: v["progress"] for k, v in parsed.items()})

# 3) แถวซ้ำ (responsive/header) ต้องรวม progress สูงสุดและ unlocked แบบ OR
dup = bot.topic_rows_from_payload(
    [
        {"tid": "1", "cls": "", "html": "width: 100%"},
        {"tid": "1", "cls": "mr-1 has-access", "html": "width: 25%"},
    ]
)
assert dup["1"]["progress"] == 100 and dup["1"]["unlocked"] is True
print("✅ รวมแถวซ้ำได้ถูกต้อง")

# 4) เวลายาว -> วินาที
assert bot.parse_duration_seconds("00:08:48") == 528
assert bot.parse_duration_seconds("15:10") == 910
print("✅ แปลงเวลาเป็นวินาทีได้")

print("\n✅ ผ่านทั้งหมด")
