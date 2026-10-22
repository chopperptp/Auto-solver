"""ทดสอบอ่านฟอร์มข้อสอบ SET จริง (จาก fixture ที่บันทึกไว้) + จับคู่เฉลย"""

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

import set_bot as sb

course = json.load(open("testdata_course299.json", encoding="utf-8-sig"))
pre = [qz for qz in sb.build_quiz_bank(course) if qz["kind"] == "pre"][0]
assert pre["id"] == 315 and len(pre["questions"]) == 5

fixture = Path("testdata_pre_form.html").resolve()
assert fixture.exists(), "missing fixture"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(fixture.as_uri())
    page.wait_for_timeout(300)

    form = page.evaluate(sb.EXAM_FORM_JS)
    assert form is not None, "ไม่พบ #exam-form"
    assert form["all"] == 5, form["all"]
    assert len(form["questions"]) == 5, len(form["questions"])
    for q in form["questions"]:
        assert len(q["options"]) == 5, (q["qid"], len(q["options"]))
        assert all(o["id"] for o in q["options"])
    print("อ่านฟอร์ม:", len(form["questions"]), "ข้อ, all =", form["all"])

    plan, missing = sb.build_exam_plan(form, pre["questions"])
    assert not missing, missing
    assert len(plan) == 5, len(plan)

    # เฉลยที่ถูกต้อง (จาก API) ต้องตรงกับ option id ที่แผนเลือก
    by_qid = {str(q["id"]): q for q in pre["questions"]}
    for p_ in plan:
        q = by_qid[p_["qid"]]
        correct_ids = {str(o["id"]) for o in q["options"] if o["correct"] == 1}
        assert p_["aid"] in correct_ids, (p_, correct_ids)
        print(f"  ข้อ {p_['qid']}: {p_['qtext'][:45]} -> {p_['atext'][:45]}")

    # ทดลองติ๊กจริงในหน้า (ยังไม่เริ่มสอบ) แล้วตรวจว่าถูกเลือกครบ
    results = page.evaluate(sb.APPLY_PLAN_JS, plan)
    assert all(r["ok"] for r in results), results
    verify = page.evaluate(sb.VERIFY_ANSWERS_JS)
    assert len(verify) == 5 and all(v["checked"] == 1 for v in verify), verify

    picked = page.eval_on_selector_all(
        "#exam-form input.answer-choice:checked",
        "els => els.map(e => e.value)",
    )
    assert sorted(picked) == sorted(p_["aid"] for p_ in plan), picked
    print("ติ๊กครบ 5 ข้อ, เลือกถูกทุกข้อ:", picked)

    # ข้อที่ไม่มีในเฉลยต้องถูกจับเป็น missing
    _, missing2 = sb.build_exam_plan(form, pre["questions"][:2])
    assert len(missing2) == 3, missing2
    print("ข้อที่ไม่มีในเฉลยถูกจับได้:", len(missing2))

    browser.close()

print("✅ ผ่านทั้งหมด")
