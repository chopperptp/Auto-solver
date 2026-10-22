"""ทดสอบจับคู่เฉลย final exam (10 ข้อ) จากฟอร์มจริงที่บันทึกไว้"""

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

import set_bot as sb

course = json.load(open("testdata_course299.json", encoding="utf-8-sig"))
exam = [qz for qz in sb.build_quiz_bank(course) if qz["kind"] == "exam"][0]
assert exam["id"] == 317
assert len(exam["questions"]) >= 15, len(exam["questions"])

fixture = Path("testdata_exam_form_317.html").resolve()
assert fixture.exists()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(fixture.as_uri())
    page.wait_for_timeout(300)

    form = page.evaluate(sb.EXAM_FORM_JS)
    assert form is not None
    assert form["all"] == 10, form["all"]
    assert len(form["questions"]) == 10, len(form["questions"])

    plan, missing = sb.build_exam_plan(form, exam["questions"])
    assert not missing, missing
    assert len(plan) == 10, len(plan)
    print(f"จับคู่ได้ {len(plan)}/10 ข้อ")

    by_qid = {str(q["id"]): q for q in exam["questions"]}
    for item in plan:
        q = by_qid[item["qid"]]
        correct_ids = {str(o["id"]) for o in q["options"] if o["correct"] == 1}
        assert len(correct_ids) == 1, (item["qid"], correct_ids)
        assert item["aid"] in correct_ids, (item, correct_ids)
        print(f"  ข้อ {item['qid']}: {item['qtext'][:42]} -> {item['atext'][:42]}")

    results = page.evaluate(sb.APPLY_PLAN_JS, plan)
    assert all(r["ok"] for r in results), results
    verify = page.evaluate(sb.VERIFY_ANSWERS_JS)
    assert len(verify) == 10 and all(v["checked"] == 1 for v in verify), verify
    print("ติ๊กครบ 10 ข้อ ถูกทุกข้อ")

    browser.close()

print("✅ ผ่านทั้งหมด")
