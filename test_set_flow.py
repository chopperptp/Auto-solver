"""ทดสอบลูปตอบข้อสอบของ set_bot กับหน้า HTML จำลอง (ไม่ต้องล็อกอิน)"""

import json
import random
from pathlib import Path

from playwright.sync_api import sync_playwright

import set_bot as sb

course = json.load(open("testdata_course299.json", encoding="utf-8-sig"))
exam = [qz for qz in sb.build_quiz_bank(course) if qz["kind"] == "exam"][0]
bank = exam["questions"][:2]

random.seed(11)

parts = []
expected = []
for qi, q in enumerate(bank, 1):
    correct = [o["text"] for o in q["options"] if o["correct"] == 1][0]
    shown = [o["text"] for o in q["options"] if o["text"]]
    random.shuffle(shown)
    expected.append(correct)
    labels = []
    for oi, text in enumerate(shown):
        rid = f"q{qi}_{oi}"
        prefixed = f"{chr(ord('ก') + oi)}. {text}"
        labels.append(
            f'<input type="radio" id="{rid}" name="q{qi}" value="{oi}">'
            f'<label for="{rid}">{prefixed}</label>'
        )
    btn = "ถัดไป" if qi == 1 else "ส่งคำตอบ"
    parts.append(
        f'<div id="q{qi}" class="q" style="display:{"block" if qi == 1 else "none"}">'
        f"<p>{q['text']}</p><div>{''.join(labels)}</div>"
        f'<button type="button" id="btn{qi}">{btn}</button></div>'
    )

html = f"""<!DOCTYPE html>
<html lang="th"><head><meta charset="utf-8"><title>Fake exam</title>
<style>label{{display:block;margin:6px 0;cursor:pointer}}.q{{border:1px solid #ccc;padding:10px;margin:10px 0}}
#result{{display:none;font-size:20px}}</style></head><body>
<h3>แบบทดสอบจำลอง</h3>
{''.join(parts)}
<div id="result"></div>
<script>
document.getElementById('btn1').onclick = function () {{
  document.getElementById('q1').style.display = 'none';
  document.getElementById('q2').style.display = 'block';
}};
document.getElementById('btn2').onclick = function () {{
  const a1 = document.querySelector('input[name=q1]:checked');
  const a2 = document.querySelector('input[name=q2]:checked');
  document.getElementById('q2').style.display = 'none';
  const r = document.getElementById('result');
  r.textContent = 'ผลการสอบ: คะแนน ' + (a1 && a2 ? '10/10 ผ่าน' : '0/10 ไม่ผ่าน');
  r.style.display = 'block';
}};
</script></body></html>"""

path = Path("_fake_exam.html").resolve()
path.write_text(html, encoding="utf-8")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(path.as_uri())

    n1 = sb.solve_one_page(page, exam["questions"], use_ai=False)
    print("ตอบหน้า 1:", n1)
    picked = page.eval_on_selector("input[name=q1]:checked + label", "e => e.innerText")
    print("เลือก:", picked)
    assert n1 == 1 and sb.opt_norm(picked) == sb.opt_norm(expected[0])

    page.locator("#btn1").click()
    page.wait_for_timeout(300)
    n2 = sb.solve_one_page(page, exam["questions"], use_ai=False)
    print("ตอบหน้า 2:", n2)
    picked2 = page.eval_on_selector("input[name=q2]:checked + label", "e => e.innerText")
    print("เลือก:", picked2)
    assert n2 == 1 and sb.opt_norm(picked2) == sb.opt_norm(expected[1])

    page.locator("#btn2").click()
    page.wait_for_timeout(300)
    result = sb.read_result(page)
    print("ผลลัพธ์:", result)
    assert "10/10" in result

    browser.close()

print("✅ ผ่านทั้งหมด")
