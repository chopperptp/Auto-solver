"""ทดสอบการวิเคราะห์โครงสร้างคอร์ส/ประวัติสอบ (ไม่ต้องต่อเน็ต)"""

import json
from pathlib import Path

import set_bot as bot

HERE = Path(__file__).parent


def load(name: str) -> dict:
    return json.loads((HERE / name).read_text(encoding="utf-8-sig"))


# 1) คอร์ส 770: 8 บทเรียนล้วน + final exam (ไม่มีคำถามแยกในบท)
course770 = load("testdata_course770.json")
plan770 = bot.course_plan(course770)
quizzes770 = bot.build_quiz_bank(course770)
assert len(plan770) == 7, len(plan770)
assert sum(len(p["topics"]) for p in plan770) == 8
assert not any(p["has_quiz"] for p in plan770), plan770
assert "exam" in {q["kind"] for q in quizzes770}
assert all(q["id"] for q in quizzes770)
bot.describe_course_plan(course770, plan770, quizzes770)
print("✅ 770: แยกได้ว่าเรียนล้วน 8 บท + final exam")

# 2) คอร์ส 299: 5 บทเรียน + pre/post/exam ระดับคอร์ส
course299 = load("testdata_course299.json")
plan299 = bot.course_plan(course299)
quizzes299 = bot.build_quiz_bank(course299)
assert sum(len(p["topics"]) for p in plan299) == 5
assert not any(p["has_quiz"] for p in plan299), plan299
kinds299 = {q["kind"] for q in quizzes299}
assert {"pre", "post", "exam"} <= kinds299, kinds299
print("✅ 299: แยกบทเรียนล้วนออกจาก pre/post/exam ได้")

# 3) หมวดที่มี quiz_id ตรงกับ final exam -> รู้ว่าเป็น "เรียน + ข้อสอบ"
synthetic = {
    "main_topics": [
        {
            "id": 1,
            "title": "Module 1",
            "quiz_id": 10,
            "sub_topics": [{"id": 11, "title": "บทที่ 1"}],
        },
        {
            "id": 2,
            "title": "Module 2",
            "quiz_id": None,
            "auto_quiz": None,
            "sub_topics": [{"id": 12, "title": "บทที่ 2"}],
        },
    ],
    "exam": {"id": 10, "title": "Final", "questions": []},
}
plan = bot.course_plan(synthetic)
assert plan[0]["has_quiz"] and plan[0]["known_quiz"]
assert plan[0]["quiz_kind"] == "exam"
assert not plan[1]["has_quiz"]
print("✅ หมวดที่มี quiz_id ถูกทำเครื่องหมายเป็นเรียน + ข้อสอบ")

# 4) quiz_id ที่ไม่มีเฉลย / auto_quiz ต้องไม่ถูกเข้าใจผิดว่าเรียนล้วน
unknown = {
    "main_topics": [
        {
            "id": 3,
            "title": "Unknown quiz",
            "quiz_id": 999,
            "sub_topics": [{"id": 31, "title": "บท"}],
        },
        {
            "id": 4,
            "title": "Auto quiz",
            "auto_quiz": 1,
            "sub_topics": [{"id": 41, "title": "บท"}],
        },
    ]
}
uplan = bot.course_plan(unknown)
assert uplan[0]["has_quiz"] and not uplan[0]["known_quiz"]
assert uplan[0]["quiz_kind"] == "quiz"
assert uplan[1]["has_quiz"] and uplan[1]["auto_quiz"]
print("✅ quiz ที่ไม่มีเฉลย/auto quiz ถูกแยกจากบทเรียนล้วน")

# 5) ประวัติสอบ: ผ่าน = ข้าม, ไม่ผ่าน = ต้องทำใหม่
passed = [{"cells": ["1", "22 ต.ค. 2569", "10/10", "ผ่าน"], "success": True}]
failed = [{"cells": ["1", "22 ต.ค. 2569", "3/10", "ไม่ผ่าน"], "success": False}]
assert bot.exam_history_passed(passed)
assert not bot.exam_history_passed(failed)
assert not bot.exam_history_passed([])
assert "10/10" in bot.exam_history_summary(passed)
assert "ผ่าน" in bot.exam_history_summary(passed)
print("✅ อ่านประวัติสอบและแยกผ่าน/ไม่ผ่านได้:", bot.exam_history_summary(passed))

print("\n✅ ผ่านทั้งหมด")
