"""ทดสอบการจับคู่เฉลยของ set_bot กับข้อมูลจริงของคอร์ส 299 (ไม่ต้องล็อกอิน)"""

import json
import random

import set_bot as sb

course = json.load(open("testdata_course299.json", encoding="utf-8-sig"))
bank = sb.build_quiz_bank(course)

random.seed(7)
tested = 0
failed = 0

for quiz in bank:
    if quiz["kind"] == "survey":
        continue
    for q in quiz["questions"]:
        correct = [o["text"] for o in q["options"] if o["correct"] == 1]
        assert len(correct) == 1, q

        shown = [o["text"] for o in q["options"] if o["text"]]
        random.shuffle(shown)
        # จำลอง prefix แบบข้อสอบไทย
        prefixed = [f"{chr(ord('ก') + i)}. {t}" for i, t in enumerate(shown)]

        item = sb.find_bank_question(q["text"], prefixed, quiz["questions"])
        idx = sb.correct_option_index(item, prefixed) if item else None
        got = shown[idx] if idx is not None else None
        tested += 1
        if got != correct[0]:
            failed += 1
            print(f"❌ {quiz['kind']} q{q['id']}: got={str(got)[:70]!r} want={correct[0][:70]!r}")
            print(f"   item={item['text'][:60] if item else None!r} idx={idx}")

print(f"\nทดสอบ {tested} ข้อ — ผิด {failed} ข้อ")
