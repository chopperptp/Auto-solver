"""
quiz_solver.py — ตัวทำข้อสอบออนไลน์อัตโนมัติ ด้วย Gemini AI
รองรับ: Dek-D Quiz, Thai MOOC (Open edX) และเว็บแบบทดสอบทั่วไป (auto-detect)

ใช้:
    python quiz_solver.py                                   # อ่านลิงก์จาก quiz_links.txt
    python quiz_solver.py "https://www.dek-d.com/quiz/..."    # ระบุ URL เอง
    python quiz_solver.py <url> --dry-run                    # ทดสอบการดึงข้อมูล ไม่คลิกอะไรเลย
    python quiz_solver.py <url> --site openedx               # บังคับใช้โปรไฟล์เว็บ
    python quiz_solver.py <url> --no-submit                  # ไม่กดปุ่มส่งคำตอบ
    python quiz_solver.py <url> --learn                      # โหมดเรียนคอร์ส Open edX ให้จบก่อน

หมายเหตุ: สำหรับ Thai MOOC Academy (learn.thaimooc.ac.th + ThaiD)
         แนะนำใช้ `thaimooc_bot.py` ซึ่งจัดการล็อกอิน/เรียนหลายคอร์สให้
"""

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types
from playwright.sync_api import Locator, Page, sync_playwright

# ให้ stdout/stderr ใช้ UTF-8 เสมอ (กัน emoji + ภาษาไทยพังตอนรันใน Windows console ที่ default เป็น cp1252/cp874)
for _stream in (sys.stdout, sys.stderr):
    try:
        if "utf" not in (_stream.encoding or "").lower():
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ============================================================================
# 1. ตั้งค่า API Key / โมเดล
# ============================================================================
load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("❌ ไม่พบ GEMINI_API_KEY ในไฟล์ .env กรุณาตรวจสอบอีกครั้ง")

client = genai.Client(api_key=api_key)

# โมเดลที่ใช้ได้จริงกับ API key นี้ (เรียงตามลำดับความน่าเชื่อถือ)
MODELS_TO_TRY = [
    "gemini-3.7-flash",
    "gemini-3-flash-preview",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-flash-latest",
]

# ข้อผิดพลาดชั่วคราว (เครื่องราวแน่น / โควตาเต็มชั่วคราว / error ฝั่งเซิร์ฟเวอร์) -> ควรลองใหม่
TRANSIENT_CODES = ("429", "500", "502", "503", "504", "RESOURCE_EXHAUSTED", "UNAVAILABLE")


def is_transient_error(err: Exception) -> bool:
    """ตรวจว่า error เป็นแบบชั่วคราว (ควร retry) หรือแบบถาวร (เช่น 404 model ไม่มีอยู่)"""
    text = str(err).upper()
    if "404" in text or "NOT_FOUND" in text:
        return False
    return any(code in text for code in TRANSIENT_CODES)


# ============================================================================
# 2. โปรไฟล์เว็บ (แต่ละเว็บใช้ selector ต่างกัน)
# ============================================================================
DEFAULT_URL = "https://www.dek-d.com/quiz/supertest/27994/"

# ---- Thai MOOC Academy ----
THAIMOOC_LMS = "https://learn.thaimooc.ac.th"
THAIMOOC_PORTAL = "https://thaimooc.ac.th"
THAIMOOC_LOGIN = "https://learn.thaimooc.ac.th/login"

# หน้า SSO (Keycloak) — ใช้เมื่อต้องการเปิดหน้าเลือกวิธีล็อกอินโดยตรง
THAIMOOC_SSO = (
    "https://sso.thaimooc.ac.th/realms/thaimooc/protocol/openid-connect/auth"
    "?client_id=thaimooc-academy"
    "&redirect_uri=https://learn.thaimooc.ac.th/auth/complete/keycloak/"
    "&response_type=code"
)

PROFILES = {
    # ---- Dek-D.com ควิซ ----
    "dek-d": {
        "match": ("dek-d.com",),
        "question": ".question-box",
        "statement": ".question-title .title",
        "submit": (
            "button:has-text('ส่งคำตอบ'), button:has-text('ตรวจคะแนน'), "
            "input[value='ส่งคำตอบ'], .btn-submit, a:has-text('ส่งคำตอบ')"
        ),
        "option_css": ".choice-item, label.radiolabel",
    },
    # ---- Thai MOOC Academy (Open edX ที่ learn.thaimooc.ac.th + Keycloak SSO) ----
    "thaimooc": {
        "match": ("learn.thaimooc.ac.th", "thaimooc.ac.th", "sso.thaimooc.ac.th"),
        "question": ".problem",
        "statement": (
            ".wrapper-problem-response legend, .problem legend, "
            ".problem-statement, .response-fieldset-legend"
        ),
        "check": "button.check, button.submit-attempt, .problem button:has-text('ตรวจสอบ')",
        "submit": (
            "button.submit, button.submit-attempt, button:has-text('Submit'), "
            "button:has-text('ส่งคำตอบ'), button:has-text('ยืนยันคำตอบ'), "
            "button:has-text('ตรวจสอบ')"
        ),
        "next": (
            "button.next-btn, .problem-nav .next, button:has-text('Next'), "
            "button:has-text('ถัดไป'), a:has-text('Next')"
        ),
        "finish": (
            "button:has-text('Finish'), button:has-text('ส่งคำตอบ'), "
            "button:has-text('จบการทดสอบ'), button:has-text('เสร็จสิ้น')"
        ),
        "option_css": "label.response-label, .choices .choice, .problem .choice, .choice",
        "skip_correct": True,      # ข้ามข้อที่ตอบถูกแล้ว (ไม่เสีย attempts)
        "per_problem_submit": True,  # Open edX มีปุ่ม Submit แยกแต่ละข้อ
    },
    # ---- Open edX (edX ตัวอื่น ๆ / lms.thaimooc.org) ----
    "openedx": {
        "match": ("lms.thaimooc.org", "openedx", "edx.org"),
        "question": ".problem",
        "statement": (
            ".wrapper-problem-response legend, .problem legend, "
            ".problem-statement, .response-fieldset-legend"
        ),
        "check": "button.submit-attempt, .problem button:has-text('Check')",
        "submit": (
            "button.submit, button.submit-attempt, button:has-text('Submit'), "
            "button:has-text('ส่งคำตอบ'), button:has-text('ตรวจคะแนน'), "
            "button:has-text('ยืนยันคำตอบ')"
        ),
        "next": "button.next-btn, a.btn.btn-primary, button.btn-primary",
        "finish": "button:has-text('Finish'), a:has-text('Finish'), button:has-text('ส่งคำตอบ')",
        "option_css": "label.response-label, .choices .choice, .problem .choice",
        "skip_correct": True,
        "per_problem_submit": True,
    },
    # ---- ทั่วไป: ใช้การจัดกลุ่ม radio อัตโนมัติ ----
    "generic": {
        "match": (),
        "submit": (
            "button:has-text('ส่งคำตอบ'), button:has-text('ตรวจคะแนน'), "
            "button:has-text('Submit'), button[type=submit]"
        ),
    },
}


def detect_site(url: str) -> str:
    """เดาเว็บจาก URL เพื่อเลือก selector ที่ใช้"""
    low = url.lower()
    for name, prof in PROFILES.items():
        for token in prof.get("match", ()):
            if token in low:
                return name
    return "generic"


# ไฟล์ที่เก็บลิงก์แบบทดสอบ (1 บรรทัด = 1 ลิงก์, บรรทัดที่ขึ้นต้นด้วย # = คอมเมนต์)
QUIZ_LINKS_FILE = Path(__file__).parent / "quiz_links.txt"


def load_quiz_links(path: Optional[str] = None) -> list[str]:
    """อ่านรายการลิงก์จากไฟล์ (ข้ามบรรทัดว่างและบรรทัดคอมเมนต์)

    รองรับไฟล์ที่บันทึกแบบมี BOM (utf-8-sig) ซึ่ง Windows/Notepad มักเป็นค่าเริ่มต้น
    """
    file_path = Path(path) if path else QUIZ_LINKS_FILE

    if not file_path.exists():
        return []

    links: list[str] = []
    for raw in file_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.replace("\ufeff", "").strip()
        if line and not line.startswith("#"):
            links.append(line)

    return links


@dataclass
class Question:
    """ข้อสอบ 1 ข้อ + ตัวเลือก พร้อมข้อมูลสำหรับคลิกกลับเข้าเว็บ"""

    text: str
    options: list[str]
    name: str = ""                       # ชื่อกลุ่ม radio (ใช้ระบุตัวเลือกตอนคลิก)
    values: list[str] = field(default_factory=list)   # value ของ radio แต่ละตัวเลือก
    container: Optional[Locator] = None  # element ของบล็อกข้อสอบ (ถ้ามี)


# ============================================================================
# 3. ตัดความ / จับคู่คำตอบ
# ============================================================================
def normalize_text(text: str) -> str:
    """ทำความสะอาดข้อความ ตัดตัวเลขข้อ เว้นวรรค สัญลักษณ์ออก เพื่อเปรียบเทียบ"""
    if not text:
        return ""
    text = re.sub(r'[*_`\'"\n\r]', "", text)
    text = re.sub(r"^\s*[\dก-ฮa-zA-Z][\.\)\-\s]+\s*", "", text)
    text = re.sub(r"\s+", "", text)
    return text.lower().strip()


def match_answer_to_index(ai_answer: str, options: list[str]) -> Optional[int]:
    """จับคู่คำตอบ AI ให้กับตัวเลือก -> คืน index (None ถ้าไม่มีตัวไหนตรงกัน)"""
    norm_ai = normalize_text(ai_answer)
    if not norm_ai:
        return None

    norm_opts = [normalize_text(o) for o in options]

    # 1) ตรงกันเป๊ะ ๆ ก่อน (ปลอดภัยที่สุด)
    for i, norm_opt in enumerate(norm_opts):
        if norm_opt and norm_opt == norm_ai:
            return i

    # 2) AI ตอบเป็นเลข/อักษรของตัวเลือก เช่น "ข้อ 3", "3.", "C" -> แปลงเป็น index
    letter_match = re.match(
        r"^(?:ข้อ\s*)?([1-9a-eA-E])\s*[.):\-]?\s*$", ai_answer.strip()
    )
    if letter_match:
        token = letter_match.group(1).lower()
        idx = int(token) - 1 if token.isdigit() else ord(token) - ord("a")
        if 0 <= idx < len(options):
            return idx

    # 3) ค้นแบบ substring โดยเลือกตัวเลือกที่ "ความยาวใกล้เคียง" ที่สุด
    #    (กันกรณีตัวเลือกสั้นเป็นส่วนหนึ่งของตัวเลือกยาว เช่น 'อาณาจักรสุโขไท' / 'อาณาจักรสุโขไทย')
    best_idx, best_diff = None, None
    for i, norm_opt in enumerate(norm_opts):
        if not norm_opt or len(norm_ai) < 2:
            continue
        if norm_ai in norm_opt or norm_opt in norm_ai:
            diff = abs(len(norm_opt) - len(norm_ai))
            if best_diff is None or diff < best_diff:
                best_idx, best_diff = i, diff

    return best_idx


# ============================================================================
# 4. เรียก AI
# ============================================================================
# cache คำตอบ AI (กันเรียก API ซ้ำตอนเจอคำถามเดิมในหลายหน่วย)
_AI_CACHE: dict[str, str] = {}


def get_answer_from_ai(question: str, options: list[str]) -> str:
    """ส่งโจทย์และตัวเลือกให้ Gemini วิเคราะห์หาคำตอบ (ลองหลายโมเดล + retry เมื่อเจอ error ชั่วคราว)"""
    cache_key = (question or "").strip() + "||" + "|".join(options)
    if cache_key in _AI_CACHE:
        return _AI_CACHE[cache_key]

    prompt = f"""
    คุณเป็นผู้เชี่ยวชาญการทำข้อสอบ โปรดอ่านโจทย์และตัวเลือกต่อไปนี้ แล้วเลือกคำตอบที่ถูกต้องที่สุดเพียงข้อเดียว

    โจทย์: {question}
    ตัวเลือก: {", ".join(options)}

    ข้อบังคับ: ให้ตอบเฉพาะข้อความตัวเลือกที่ถูกต้องที่สุดเพียงอย่างเดียว ห้ามใส่คำอธิบาย ห้ามใส่เลขข้อ
    """

    config = types.GenerateContentConfig(
        temperature=0.0,
        max_output_tokens=2048,
    )

    last_error = None

    for model_name in MODELS_TO_TRY:
        for attempt in range(1, 4):  # retry 3 ครั้งต่อ 1 โมเดล
            try:
                response = client.models.generate_content(
                    model=model_name, contents=prompt, config=config
                )
                text = (response.text or "").strip()

                if text:
                    if attempt > 1:
                        print(f"♻️ ใช้โมเดล '{model_name}' สำเร็จหลังลองใหม่ {attempt} ครั้ง")
                    _AI_CACHE[cache_key] = text
                    return text

                last_error = RuntimeError(f"โมเดล '{model_name}' ตอบกลับมาเปล่า")
                break  # เปลี่ยนโมเดล

            except Exception as err:
                last_error = err
                if not is_transient_error(err):
                    print(f"⚠️ โมเดล '{model_name}' ใช้ไม่ได้: {str(err)[:120]}")
                    break  # error แบบถาวร -> เปลี่ยนโมเดล

                wait = 2 * attempt
                print(f"⏳ '{model_name}' ตอบกลับผิดพลาดชั่วคราว ({str(err)[:80]}) ลองใหม่ใน {wait} วินาที...")
                time.sleep(wait)

    print(f"❌ Gemini API Error Details: {last_error}")
    return ""


# ============================================================================
# 5. ดึงข้อสอบจากหน้าเว็บ
# ============================================================================
def _label_of(radio: Locator) -> Optional[Locator]:
    """หา element ป้ายกำกับของ radio input (ครอบคลุมหลายรูปแบบมาตรฐาน)"""
    rid = radio.get_attribute("id")
    if rid:
        lab = radio.page.locator(f"label[for='{rid}']")
        if lab.count():
            return lab.first

    for xpath in (
        "xpath=ancestor::label[1]",
        "xpath=following-sibling::label[1]",
        "xpath=../following-sibling::*[self::label or self::span][1]",
    ):
        lab = radio.locator(xpath)
        if lab.count():
            return lab.first

    return None


def _options_in(container: Locator) -> tuple[str, list[str], list[str]]:
    """ดึง (ชื่อกลุ่ม radio, ข้อความตัวเลือก, value ของ radio) จากบล็อกข้อสอบ"""
    radios = container.locator("input[type=radio]")
    names, texts, values = [], [], []

    for radio in radios.all():
        name = radio.get_attribute("name") or ""
        value = radio.get_attribute("value") or ""

        label = _label_of(radio)
        text = ""
        if label is not None:
            text = label.inner_text().strip()
        if not text:
            text = (radio.get_attribute("title") or "").strip()
        if not text:
            text = (radio.get_attribute("aria-label") or "").strip()

        if not text:
            continue

        names.append(name)
        texts.append(re.sub(r"\s+", " ", text).strip())
        values.append(value)

    group_name = names[0] if names else ""
    return group_name, texts, values


_NOISE_RE = re.compile(
    r"\b(Submit|Show answer|Hide answer|Show Answer|Review|Status|"
    r"correct|incorrect|unanswered|points?|point|graded|unlimited|attempts?|"
    r"ส่งคำตอบ|ตรวจสอบ|ดูเฉลย|แสดงเฉลย|ซ่อนเฉลย|สถานะ|ถูกต้อง|ไม่ถูกต้อง|คะแนน)\b",
    re.IGNORECASE,
)


def _statement_of(container: Locator, statement_css: Optional[str]) -> str:
    """ดึงข้อความโจทย์จากบล็อกข้อสอบ"""
    if statement_css:
        el = container.locator(statement_css)
        if el.count():
            txt = el.first.inner_text().strip()
            if txt:
                return re.sub(r"\s+", " ", txt)

    # สำรอง: ใช้ข้อความทั้งหมดในบล็อก แล้วตัดข้อความตัวเลือกออก
    _, opt_texts, _ = _options_in(container)
    text = container.inner_text()
    for opt in opt_texts:
        if opt:
            text = text.replace(opt, " ")
    text = _NOISE_RE.sub(" ", text)
    text = re.sub(r"\d+\s*/\s*\d+\s*points?", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^\d+\s*[.)]\s*", "", text)
    return text


def _is_problem_correct(block: Locator) -> bool:
    """ข้อนี้ตอบถูกแล้วหรือยัง (Open edX ใส่ class 'correct' ที่ .status)"""
    try:
        st = block.locator(".status").first
        if st.count():
            cls = (st.get_attribute("class") or "").split()
            if "correct" in cls:
                return True
        return False
    except Exception:
        return False


def _revealed_correct_index(block: Locator, options: list[str]) -> Optional[int]:
    """ถ้าหน้าเว็บเฉลยคำตอบที่ถูกไว้แล้ว (class choicegroup_correct) ให้ใช้ตัวนั้นเลย"""
    try:
        lbl = block.locator("label.choicegroup_correct, .choicegroup_correct").first
        if not lbl.count():
            return None
        txt = re.sub(r"\s+", " ", lbl.inner_text()).strip()
        if not txt:
            return None
        for i, opt in enumerate(options):
            if opt == txt:
                return i
    except Exception:
        pass
    return None


def extract_questions_profile(page: Page, profile: dict) -> list[Question]:
    """ดึงข้อสอบตาม selector ของเว็บที่กำหนดไว้"""
    questions: list[Question] = []

    css = profile.get("question")
    if not css:
        return questions

    skip_correct = bool(profile.get("skip_correct"))
    skipped = 0

    for block in page.locator(css).all():
        try:
            if not block.is_visible():
                continue

            if skip_correct and _is_problem_correct(block):
                skipped += 1
                continue

            group_name, opt_texts, values = _options_in(block)
            if not opt_texts:
                continue

            question_text = _statement_of(block, profile.get("statement"))

            questions.append(
                Question(
                    text=question_text,
                    options=opt_texts,
                    name=group_name,
                    values=values,
                    container=block,
                )
            )
        except Exception as e:
            print(f"⚠️ อ่านข้อสอบไม่สำเร็จ: {e}")

    if skipped:
        print(f"⏭️  ข้าม {skipped} ข้อที่ตอบถูกแล้ว")

    return questions


# JS: จัดกลุ่ม radio ตามชื่อ แล้วหา "ประโยคโจทย์" ของกลุ่มนั้น (ใช้กับเว็บที่ไม่รู้ selector)
_GENERIC_GROUP_JS = """
(name) => {
  const radios = Array.from(document.querySelectorAll('input[type=radio]'))
                      .filter(r => r.name === name);
  if (!radios.length) return null;

  const findLabel = (r) => {
    let el = null;
    if (r.id) el = document.querySelector('label[for="' + CSS.escape(r.id) + '"]');
    if (!el) el = r.closest('label');
    if (!el && r.parentElement) el = r.parentElement.querySelector('label');
    return el;
  };

  const texts = radios.map(r => {
    const el = findLabel(r);
    const t = (el ? el.innerText : (r.value || '')) || '';
    return t.replace(/\s+/g, ' ').trim();
  });

  // container = ancestor ที่ลึกที่สุดที่ครอบคลุม radio ทั้งกลุ่ม
  let node = radios[0].parentElement;
  const holdsAll = (el) => radios.every(r => el.contains(r));
  while (node && !holdsAll(node)) node = node.parentElement;

  const NOISE = /^(check|submit|continue|next|finish|save|ok|ยืนยัน|ตรวจคะแนน|ส่งคำตอบ|ถัดไป|ทำต่อ|บันทึก|เปรียบเทียบ|ปิด|ล้าง)$/i;

  let statement = '';
  let cur = node;
  for (let i = 0; i < 10 && cur; i++) {
    let t = (cur.innerText || '').replace(/\\s+/g, ' ');
    texts.forEach(ot => { if (ot) t = t.split(ot).join(' '); });
    t = t.split(/\\s+/).filter(w => w && !NOISE.test(w)).join(' ').trim();
    t = t.replace(/^\\d+\\s*[.)]\\s*/, '');
    if (t.length) { statement = t; break; }
    cur = cur.parentElement;
  }

  return { statement: statement, options: texts };
}
"""


def extract_questions_generic(page: Page) -> list[Question]:
    """ดึงข้อสอบแบบอัตโนมัติ: จัดกลุ่ม radio input ทั้งหน้า"""
    questions: list[Question] = []

    radios = page.locator("input[type=radio]")
    groups: dict[str, list[str]] = {}   # name -> list of value
    order: list[str] = []

    for radio in radios.all():
        try:
            name = radio.get_attribute("name") or ""
            value = radio.get_attribute("value") or ""
            if not name:
                continue
            if name not in groups:
                groups[name] = []
                order.append(name)
            groups[name].append(value)
        except Exception:
            continue

    for name in order:
        try:
            data = page.evaluate(_GENERIC_GROUP_JS, name)
            if not data:
                continue
            options = [o for o in data.get("options", []) if o]
            if len(options) < 2:
                continue
            questions.append(
                Question(
                    text=(data.get("statement") or "").strip(),
                    options=options,
                    name=name,
                    values=groups.get(name, []),
                    container=None,
                )
            )
        except Exception as e:
            print(f"⚠️ อ่านกลุ่มตัวเลือก '{name}' ไม่สำเร็จ: {e}")

    return questions


def extract_questions(page: Page, profile: dict) -> list[Question]:
    """ดึงข้อสอบ: ลองตาม selector ของเว็บก่อน ถ้าไม่มีเลยค่อยใช้วิธีอัตโนมัติ"""
    css = profile.get("question")
    if css:
        try:
            if page.locator(css).count():
                # หน้านี้มีบล็อกข้อสอบตาม selector แล้ว -> ใช้ผลจากโปรไฟล์เท่านั้น
                return extract_questions_profile(page, profile)
        except Exception:
            pass

    print("🔎 ไม่พบ selector เฉพาะเว็บ กำลังลองวิธีอัตโนมัติ (จัดกลุ่ม radio)...")
    return extract_questions_generic(page)


# ============================================================================
# 6. คลิกเลือกคำตอบ
# ============================================================================
def _is_radio_checked(radio) -> bool:
    try:
        return bool(radio.is_checked())
    except Exception:
        try:
            return bool(radio.evaluate("el => el.checked"))
        except Exception:
            return False


def click_option(page: Page, question: Question, index: int, option_css: Optional[str]) -> bool:
    """คลิกเลือกตัวเลือกที่ index แล้วยืนยันว่า radio ถูกเลือกจริง"""
    if index < 0 or index >= len(question.options):
        return False
    if not question.name or index >= len(question.values):
        return False

    name = question.name
    value = question.values[index]
    radio = page.locator(
        f"input[type=radio][name='{name}'][value='{value}']"
    ).first

    try:
        if not radio.count():
            return False
    except Exception:
        return False

    # ---- 1) คลิก label ที่ผูกกับ radio ตัวนี้ (แม่นที่สุด: edX วาง label ไว้ข้าง ๆ input) ----
    try:
        rid = radio.get_attribute("id") or ""
        if rid:
            lab = page.locator(f"label[for='{rid}']").first
            if lab.count():
                lab.scroll_into_view_if_needed(timeout=4000)
                lab.click(force=True, timeout=4000)
    except Exception:
        pass

    # ---- 2) ถ้ายังไม่ติด ให้สั่ง check() ที่ตัว radio ----
    if not _is_radio_checked(radio):
        try:
            radio.scroll_into_view_if_needed(timeout=4000)
            radio.check(force=True, timeout=4000)
        except Exception:
            pass

    # ---- 3) สุดท้าย: ตั้งค่าด้วย JS + ยิง event (ไม่ยิง click ซ้ำ เพราะจะสลับค่า) ----
    if not _is_radio_checked(radio):
        try:
            radio.evaluate(
                """el => {
                    el.checked = true;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }"""
            )
        except Exception:
            pass

    ok = _is_radio_checked(radio)
    if not ok:
        return False

    # ไฮไลต์ให้เห็นชัดว่าเลือกตัวไหน
    try:
        radio.evaluate(
            """el => {
                const box = el.closest('.field, .choice-item, .choice, label, li') || el.parentElement;
                if (box) {
                    box.style.border = '3px solid #28a745';
                    box.style.backgroundColor = '#d4edda';
                    box.style.boxShadow = '0 0 10px rgba(40,167,69,0.85)';
                }
            }"""
        )
    except Exception:
        pass

    return True


# ============================================================================
# 7. โหมด "เรียนก่อน" — ไล่เรียนคอร์สให้ครบ (Open edX / Thai MOOC)
# ============================================================================
def portal_code_to_course_id(code: str) -> Optional[str]:
    """แปลงรหัสคอร์สบน portal -> course_id ของ Open edX

    ตัวอย่าง: TAT00986 -> course-v1:TAT+00986+0
    """
    m = re.fullmatch(r"([A-Za-z]+)(\d{5})", (code or "").strip())
    if not m:
        return None
    return f"course-v1:{m.group(1).upper()}+{m.group(2)}+0"


def course_id_from_url(url: str) -> Optional[str]:
    """ดึง course_id ออกจาก URL"""
    url = url or ""

    # ลิงก์หน้า portal: https://thaimooc.ac.th/courses/TAT00986
    m = re.search(r"thaimooc\.ac\.th/courses/([A-Za-z]+\d{5})(?![A-Za-z0-9])", url)
    if m:
        cid = portal_code_to_course_id(m.group(1))
        if cid:
            return cid

    # ลิงก์บน learn.thaimooc.ac.th (Open edX): /courses/course-v1:O+12345+0/...
    m = re.search(r"/courses/([^/?#]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"course-v1:[^/?#]+", url)
    return m.group(0) if m else None


_JS_GET_JSON = """
async (args) => {
    const [api, want] = args;
    try {
        const r = await fetch(api, {credentials: 'include', headers: {'Accept': 'application/json'}});
        if (!r.ok) return {__error: r.status + ' ' + (await r.text()).slice(0, 300)};
        const j = await r.json();
        if (want === 'username') return {__value: j.username || ''};
        return j;
    } catch (e) { return {__error: String(e)}; }
}
"""


def _api_json(page: Page, api: str, want: str = "json"):
    """เรียก API ผ่านเบราว์เซอร์ (ลองใหม่ถ้าหน้าเพิ่งเปลี่ยนทาง)"""
    last = None
    for attempt in range(4):
        try:
            return page.evaluate(_JS_GET_JSON, [api, want])
        except Exception as e:
            msg = str(e)
            last = {"__error": msg.splitlines()[0]}
            # หน้าเว็บกำลังเปลี่ยนทาง (MFE redirect) — รอแล้วลองใหม่
            if "Execution context was destroyed" in msg or "navigation" in msg.lower():
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                except Exception:
                    pass
                time.sleep(1.5 + attempt)
                continue
            break
    return last or {"__error": "unknown"}


def fetch_course_units(page: Page, course_id: str) -> list[dict]:
    """ดึงหน่วยเรียนทั้งหมดของคอร์สจาก Open edX API (ใช้ cookie ที่ล็อกอินไว้)"""
    from urllib.parse import quote

    cid = quote(course_id, safe="")

    # หน้า MFE (/courses/.../) เปลี่ยนทางเองบ่อย ทำให้ evaluate ล้มเหลว
    # -> ย้ายไปหน้า about ที่เป็น HTML นิ่ง ๆ ก่อนเรียก API
    try:
        cur = page.url or ""
        if "/xblock/" not in cur and not cur.rstrip("/").endswith("/about"):
            page.goto(
                f"{THAIMOOC_LMS}/courses/{course_id}/about",
                wait_until="domcontentloaded",
                timeout=90000,
            )
            time.sleep(1.5)
    except Exception:
        pass

    # ต้องส่ง username ของตัวเอง (all_blocks=true ใช้ได้เฉพาะ staff)
    me = _api_json(page, "/api/user/v1/me", "username")
    username = me.get("__value") if isinstance(me, dict) else None

    api_base = "/api/courses/v1/blocks/?course_id=" + cid
    fields = "&requested_fields=children,type,display_name,completion"

    if username:
        api = api_base + f"&username={quote(username, safe='')}&depth=10" + fields
    else:
        api = api_base + "&all_blocks=true&depth=10" + fields

    data = _api_json(page, api)

    # ลองใหม่ถ้า depth 10 ไม่พอ/เซิร์ฟเวอร์ไม่รับ requested_fields
    if isinstance(data, dict) and data.get("__error"):
        if username:
            api2 = api_base + f"&username={quote(username, safe='')}&depth=20" + fields
        else:
            api2 = api_base + "&all_blocks=true&depth=20" + fields
        alt = _api_json(page, api2)
        if isinstance(alt, dict) and not alt.get("__error"):
            data = alt

    if not isinstance(data, dict):
        return []
    if data.get("__error"):
        msg = data["__error"]
        print(f"❌ ดึงโครงสร้างคอร์สไม่สำเร็จ: {msg}")
        if "403" in msg:
            print("   👉 บัญชีนี้ยังไม่ได้ลงทะเบียน (enroll) คอร์สนี้ หรือ session หลุด — ลองล็อกอินใหม่")
        return []

    blocks = data.get("blocks") or {}
    units: list[dict] = []

    def children_of(node) -> list[dict]:
        """ลูกของ block (API อาจคืนมาเป็น id หรือเป็น dict)"""
        if not isinstance(node, dict):
            return []
        kids = node.get("children")
        if not isinstance(kids, list):
            return []
        out = []
        for kid in kids:
            if isinstance(kid, str):
                kid = blocks.get(kid, {})
            if isinstance(kid, dict):
                out.append(kid)
        return out

    def is_done(node) -> bool:
        comp = node.get("completion")
        if isinstance(comp, dict):
            return bool(comp.get("complete"))
        if isinstance(comp, (bool, int)):
            return bool(comp)
        return False

    def unit_url(node) -> str:
        """URL ที่เรนเดอร์เนื้อหาหน่วยตรง ๆ (เสิร์ฟจากเซิร์ฟเวอร์ ไม่ต้องพึ่ง JS ของ MFE)"""
        return (
            node.get("student_view_url")
            or node.get("block_url")
            or node.get("lms_web_url")
            or node.get("url")
            or ""
        )

    def walk(node, trail: list[str]) -> None:
        for child in children_of(node):
            btype = child.get("type", "")
            title = child.get("display_name") or btype
            kids = children_of(child)

            if btype in ("course", "chapter", "sequential"):
                walk(child, trail + ([title] if btype != "course" else []))
                continue

            if btype == "vertical":
                # 1 หน้า courseware = 1 vertical (อาจมีวิดีโอ/โจทย์/เนื้อหาหลายอัน)
                types = [k.get("type", "") for k in kids]
                url = unit_url(child)
                if not url:
                    for k in kids:
                        url = unit_url(k)
                        if url:
                            break
            else:
                # ไม่มี vertical ครอบ (บางคอร์สวางโจทย์ใน sequential ตรง ๆ)
                types = [btype]
                url = unit_url(child)

            if not url:
                continue

            units.append(
                {
                    "name": title,
                    "type": "vertical",
                    "types": [t for t in types if t],
                    "url": url,
                    "done": is_done(child),
                    "path": " / ".join(trail),
                }
            )

    if data.get("root"):
        root = blocks.get(data["root"], data)
        walk(root, [])
    else:
        walk(data, [])

    return units


def ensure_enrolled(page: Page) -> bool:
    """กดปุ่ม 'Enroll Now' ถ้ายังไม่ได้ลงทะเบียนคอร์สนี้"""
    for sel in (
        "button:has-text('Enroll Now')",
        "a:has-text('Enroll Now')",
        "button:has-text('ลงทะเบียน')",
        "a:has-text('ลงทะเบียน')",
        "button.enroll-btn",
    ):
        btn = page.locator(sel).first
        try:
            if btn.count() and btn.is_visible():
                btn.click(force=True)
                time.sleep(3)
                print("   ✅ ลงทะเบียนคอร์สเรียบร้อย")
                return True
        except Exception:
            continue
    return False


def fast_forward_video(page: Page, max_wait: int = 90) -> bool:
    """เร่งวิดีโอให้ดูจนจบ (Open edX จะทำเครื่องหมายว่าดูจบแล้วเมื่อได้ยิน event 'ended')"""
    info = page.evaluate(
        """() => {
            const v = document.querySelector('video');
            if (!v) return null;
            v.muted = true;
            try { v.playbackRate = 16; } catch (e) {}
            const d = (isFinite(v.duration) && v.duration > 0) ? v.duration : 0;
            return {duration: d, ended: !!v.ended};
        }"""
    )
    if not info or not info.get("duration"):
        return False

    deadline = time.time() + max_wait
    while time.time() < deadline:
        state = page.evaluate(
            """() => {
                const v = document.querySelector('video');
                if (!v) return null;
                if (v.ended) return {ended: true};
                const d = (isFinite(v.duration) && v.duration > 0) ? v.duration : 0;
                try { v.currentTime = Math.min(d - 0.2, v.currentTime + 25); } catch (e) {}
                if (v.paused) { const p = v.play(); if (p && p.catch) p.catch(() => {}); }
                return {ended: false, t: v.currentTime, d: d};
            }"""
        )
        if state is None:
            break
        if state.get("ended"):
            return True
        time.sleep(1.0)

    return False


def scroll_to_bottom(page: Page) -> None:
    """เลื่อนหน้าลงล่างสุด (บางคอร์สต้องเลื่อนจนสุดถึงจะนับว่าอ่านแล้ว)"""
    try:
        page.evaluate(
            """() => {
                const step = window.innerHeight * 0.9;
                let y = 0;
                for (let i = 0; i < 25; i++) {
                    window.scrollBy(0, step);
                    y += step;
                    if (document.body.scrollHeight - (window.scrollY + window.innerHeight) < 5) break;
                }
                window.scrollTo(0, document.body.scrollHeight);
            }"""
        )
        time.sleep(0.8)
    except Exception:
        pass


def start_exam(page: Page, password: Optional[str] = None) -> bool:
    """กดปุ่มเริ่มสอบ + ใส่รหัสสอบ (ถ้ามี)"""
    started = False

    for sel in (
        "button:has-text('Start')",
        "a:has-text('Begin')",
        "button:has-text('Begin')",
        "button:has-text('เริ่มสอบ')",
        "button:has-text('เริ่มทำข้อสอบ')",
    ):
        btn = page.locator(sel).first
        try:
            if btn.count() and btn.is_visible():
                btn.click(force=True)
                time.sleep(2.5)
                started = True
                break
        except Exception:
            continue

    # ช่องใส่รหัสผ่านของข้อสอบ
    try:
        pw = page.locator("input[type=password], input[name='exam_password']").first
        if pw.count() and pw.is_visible():
            if password:
                pw.fill(password)
                for sel in ("button:has-text('Start')", "button[type=submit]", "button:has-text('เริ่ม')"):
                    go = page.locator(sel).first
                    if go.count() and go.is_visible():
                        go.click(force=True)
                        time.sleep(2.5)
                        started = True
                        break
            else:
                print("⚠️ ข้อสอบนี้ต้องใส่รหัส -> กรุณาใส่ในหน้าต่างเบราว์เซอร์ แล้วกด Enter")
                try:
                    input("   กด [Enter] เมื่อเริ่มสอบแล้ว: ")
                except EOFError:
                    pass
                started = True
    except Exception:
        pass

    return started


def mark_unit_complete(page: Page) -> bool:
    """กดปุ่ม 'ทำเครื่องหมายว่าเรียนจบ' ของหน่วยเรียน (Open edX completion toggle) ถ้ามี"""
    for sel in (
        "button:has-text('Mark as complete')",
        "button:has-text('ทำเครื่องหมายว่าเรียนจบ')",
        "button:has-text('เรียนจบแล้ว')",
        "button[aria-label*='complete']",
        "input[name='completion']",
        ".sequence-nav-button.complete",
    ):
        btn = page.locator(sel).first
        try:
            if btn.count() and btn.is_visible():
                btn.click(force=True)
                time.sleep(1.2)
                print("   ✅ กดทำเครื่องหมายว่าเรียนจบแล้ว")
                return True
        except Exception:
            continue
    return False


def learn_course(
    page: Page,
    course_id: str,
    profile: dict,
    dry_run: bool = False,
    exam_password: Optional[str] = None,
) -> int:
    """ไล่เรียนทุกหน่วยของคอร์ส: ดูวิดีโอให้จบ / อ่านเนื้อหา / ตอบ pre-test"""
    units = fetch_course_units(page, course_id)
    if not units:
        return 0

    print(f"\n📚 พบหน่วยเรียนทั้งหมด {len(units)} หน่วย — เริ่มเรียนอัตโนมัติ\n")

    done = 0

    for idx, unit in enumerate(units, 1):
        url = unit["url"]
        if url.startswith("/"):
            base = re.match(r"(https?://[^/]+)", page.url)
            url = (base.group(1) if base else THAIMOOC_LMS) + url

        print("-" * 46)
        print(f"📖 [{idx}/{len(units)}] {unit['name']}  (type: {','.join(unit.get('types') or [unit['type']])})")

        try:
            page.goto(url, wait_until="domcontentloaded")
            time.sleep(2.5)

            if dry_run:
                print("   (dry-run: ข้ามการเรียน)")
                continue

            # ---- วิดีโอ ----
            if "video" in (unit.get("types") or []) or page.locator("video").count():
                print("   🎬 เร่งวิดีโอ...")
                ok = fast_forward_video(page)
                print("   ✅ ดูวิดีโอจบ" if ok else "   ⚠️ ดูวิดีโอไม่จบ (อาจเป็นวิดีโอ YouTube)")

                # คำถามในวิดีโอ (in-video quiz)
                if page.locator(".in-video-quiz, .video-quiz").count():
                    print("   ❓ พบคำถามในวิดีโอ กำลังตอบ")
                    for q in extract_questions_generic(page):
                        ai = get_answer_from_ai(q.text, q.options)
                        oi = match_answer_to_index(ai, q.options)
                        if oi is not None:
                            click_option(page, q, oi, None)
                    cont = page.locator(".in-video-quiz button:has-text('Continue'), .in-video-quiz .btn-primary").first
                    if cont.count() and cont.is_visible():
                        cont.click(force=True)
                        time.sleep(1.5)

            # ---- ข้อสอบ / แบบฝึกหัดในหน่วยนี้ ----
            questions = extract_questions(page, profile)
            if questions:
                print(f"   📝 พบ {len(questions)} ข้อในหน่วยนี้")
                solve_questions(page, questions, profile, dry_run=False)

            # ---- แบบสอบ (exam) แบบทีละหน้า ----
            if "exam" in (unit.get("types") or []):
                start_exam(page, exam_password)
                for _ in range(80):
                    qs = extract_questions(page, profile)
                    if not qs:
                        break
                    solve_questions(page, qs, profile, dry_run=False)
                    if not click_continue(page, profile):
                        break

            # ---- เนื้อหาอ่าน/วิดีโออื่น ๆ ----
            scroll_to_bottom(page)
            time.sleep(0.6)

            # กดทำเครื่องหมายว่าเรียนจบ (ถ้าคอร์สใช้ระบบ completion toggle)
            mark_unit_complete(page)

            done += 1

        except Exception as e:
            print(f"   ⚠️ เกิดข้อผิดพลาด: {e}")

    print(f"\n✅ เรียนครบ {done}/{len(units)} หน่วย")
    return done


# ============================================================================
# 8. flow ของแต่ละเว็บ
# ============================================================================
def is_login_page(page: Page) -> bool:
    """ตรวจว่าหน้าปัจจุบันเป็นหน้าเข้าสู่ระบบหรือไม่ (รองรับทั้ง Open edX และ Keycloak)"""
    url = (page.url or "").lower()
    if any(k in url for k in (
        "login", "signin", "sign_in", "/auth", "account/login",
        "openid-connect", "idp.", "sso.thaimooc",
    )):
        return True
    try:
        # ปุ่ม ThaiD บนหน้า Keycloak ของ Thai MOOC = ยังไม่ล็อกอินแน่นอน
        if page.locator("#social-thaid, #social-line").count():
            return True
    except Exception:
        pass
    try:
        # ต้องเป็นช่องรหัสผ่านที่ "มองเห็นได้" เท่านั้น (Dek-D มีช่องรหัสผ่านซ่อนอยู่ในเมนู)
        return page.locator("input[type=password]:visible").count() > 0
    except Exception:
        return False


def click_thaid_login(page: Page, timeout: float = 20) -> bool:
    """กดปุ่ม 'เข้าสู่ระบบด้วย ThaiD' บนหน้า SSO ของ Thai MOOC

    คืน True = กดสำเร็จ (จากนี้ผู้ใช้ต้องยืนยันตัวตนกับ ThaiD เอง)
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for sel in ("#social-thaid", "a[id='social-thaid']", "a:has-text('ThaiD')"):
            btn = page.locator(sel).first
            try:
                if btn.count() and btn.is_visible():
                    btn.click(force=True)
                    time.sleep(2.5)
                    return True
            except Exception:
                continue
        time.sleep(0.5)
    return False


SUBMIT_WORDS = ("submit", "ส่งคำตอบ", "ตรวจคะแนน", "ยืนยัน", "ส่ง", "ok", "เสร็จ", "finish")


def confirm_dialog(page: Page, timeout: float = 4) -> bool:
    """กดยืนยันใน modal/กล่องข้อความที่เด้งขึ้นมาหลังกดส่งคำตอบ (เช่น Open edX exam)"""
    deadline = time.time() + timeout
    selectors = (
        ".modal.show .btn-primary",
        ".modal.show button:has-text('Submit')",
        ".modal.show button:has-text('Yes')",
        ".modal.show button:has-text('OK')",
        ".modal.show button:has-text('ยืนยัน')",
        ".wrapper-modal .action-primary",
        ".modal .btn-primary",
    )
    while time.time() < deadline:
        for sel in selectors:
            btn = page.locator(sel).first
            try:
                if btn.count() and btn.is_visible():
                    label = re.sub(r"\s+", " ", btn.inner_text()).strip()
                    btn.click(force=True)
                    print(f"✅ ยืนยันในกล่องข้อความ: '{label or 'OK'}'")
                    time.sleep(2)
                    return True
            except Exception:
                continue
        time.sleep(0.5)
    return False


def wait_for_problems(page: Page, profile: dict, timeout: float = 15) -> bool:
    """รอให้โจทย์/ตัวเลือกเรนเดอร์เสร็จ (Open edX โหลดเนื้อหาผ่าน JS)"""
    css = profile.get("question")
    if not css:
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.locator(css).count():
                time.sleep(1.0)   # เผื่อเวลาให้ตัวเลือก (radio) เรนเดอร์ครบ
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def dump_debug_page(
    page: Page,
    name: str = "debug_page",
    course_id: Optional[str] = None,
) -> Optional[str]:
    """บันทึก HTML ของหน้าปัจจุบันไว้ให้ตรวจ selector (ใช้เมื่อหาข้อสอบไม่เจอ)"""
    try:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", course_id or "").strip("_")
        fname = f"{name}_{safe}.html" if safe else f"{name}.html"
        path = Path(__file__).parent / fname
        path.write_text(page.content(), encoding="utf-8")
        print(f"   💾 บันทึกหน้าไว้ตรวจสอบแล้ว: {path.name}")
        return str(path)
    except Exception:
        return None


_JS_FIND_SUBMITTABLE = """
() => {
    const problems = [...document.querySelectorAll('.problem')];
    for (let i = 0; i < problems.length; i++) {
        const p = problems[i];
        const status = p.querySelector('.status');
        if (status && status.classList.contains('correct')) continue;  // ตอบถูกแล้ว
        const checked = p.querySelector(
            'input[type=radio]:checked, input[type=checkbox]:checked'
        );
        if (!checked) continue;
        const btn = [...p.querySelectorAll('button')].find(b => {
            const t = ((b.innerText || b.value || '')).trim().toLowerCase();
            const visible = !!(b.offsetWidth || b.offsetHeight);
            return visible && !b.disabled && (t === 'submit' || t === 'ส่งคำตอบ' || t === 'ตรวจสอบ');
        });
        if (btn) return i;
    }
    return -1;
}
"""


def _problem_by_group(page: Page, group: str):
    """หาบล็อกข้อสอบจากชื่อ radio group (ทนต่อการ re-render)"""
    return page.locator(f".problem:has(input[name='{group}'])").first


def _status_of(page: Page, group: str) -> str:
    try:
        block = _problem_by_group(page, group)
        if not block.count():
            return ""
        st = block.locator(".status").first
        if not st.count():
            return ""
        return (st.get_attribute("class") or "")
    except Exception:
        return ""


def _wait_submitted(page: Page, group: str, timeout: float = 25.0) -> str:
    """รอจนกว่าข้อนี้จะถูกตรวจ (correct / incorrect)"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        cls = _status_of(page, group)
        words = cls.split()
        if "correct" in words:
            return "correct"
        if "incorrect" in words:
            return "incorrect"
        time.sleep(0.6)
    return ""


def solve_page_one_by_one(page: Page, profile: dict, dry_run: bool = False) -> dict:
    """ตอบ + กด Submit ทีละข้อ (แบบ Open edX ที่แต่ละข้อมีปุ่มของตัวเอง)

    คืนค่า {"answered": n, "correct": n, "incorrect": n, "skipped": n}
    """
    done: set[str] = set()
    answered = correct = incorrect = 0

    for _ in range(200):
        questions = extract_questions(page, profile)   # ข้ามข้อที่ตอบถูกแล้ว
        q = next((x for x in questions if x.name not in done), None)
        if q is None:
            break

        done.add(q.name)
        print("-" * 40)
        print(f"📌 ข้อ: {q.text}")
        print(f"📌 ตัวเลือก: {q.options}")

        if dry_run:
            continue

        idx = _revealed_correct_index(q.container, q.options) if q.container is not None else None
        if idx is not None:
            print(f"🎯 หน้าเว็บเฉลยไว้แล้ว: '{q.options[idx]}'")
        else:
            ai_answer = get_answer_from_ai(q.text, q.options)
            print(f"💡 AI ตอบกลับมาว่า: '{ai_answer}'")
            idx = match_answer_to_index(ai_answer, q.options)

        if idx is None:
            print("⚠️ จับคู่ตัวเลือกไม่ได้ — ข้ามข้อนี้")
            continue

        try:
            q.container.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass

        if not click_option(page, q, idx, profile.get("option_css")):
            print("⚠️ คลิกตัวเลือกไม่สำเร็จ — ข้ามข้อนี้")
            continue

        answered += 1
        print(f"✅ เลือก: '{q.options[idx]}'")

        # ---- กด Submit ของข้อนี้ ----
        try:
            block = _problem_by_group(page, q.name)
            btn = block.locator(
                "button:has-text('Submit'), button:has-text('ส่งคำตอบ'), "
                "button:has-text('ตรวจสอบ'), button.submit"
            ).first
            btn.scroll_into_view_if_needed(timeout=5000)
            btn.click(force=True, timeout=5000)
            print("🚀 กด Submit")
        except Exception as e:
            print(f"⚠️ กด Submit ไม่สำเร็จ: {e}")
            continue

        result = _wait_submitted(page, q.name)
        if result == "correct":
            correct += 1
            print("🟢 ถูกต้อง")
        elif result == "incorrect":
            incorrect += 1
            print("🔴 ผิด")
        else:
            print("⏳ ไม่เห็นผลตรวจ (อาจต้องกดเองในเบราว์เซอร์)")

    return {"answered": answered, "correct": correct, "incorrect": incorrect}


def submit_all_problems(page: Page, profile: dict) -> int:
    """กด Submit ของทุกข้อที่เลือกคำตอบแล้ว (Open edX มีปุ่มแยกแต่ละข้อ)"""
    sent = 0
    for _ in range(80):
        try:
            idx = page.evaluate(_JS_FIND_SUBMITTABLE)
        except Exception:
            break
        if not isinstance(idx, int) or idx < 0:
            break

        try:
            block = page.locator(".problem").nth(idx)
            btn = block.locator(
                "button:has-text('Submit'), button:has-text('ส่งคำตอบ'), "
                "button:has-text('ตรวจสอบ'), button.submit"
            ).first
            btn.scroll_into_view_if_needed(timeout=5000)
            btn.click(force=True, timeout=5000)
            sent += 1
            time.sleep(2.0)
            confirm_dialog(page)
        except Exception as e:
            print(f"   ⚠️ กด Submit ข้อที่ {idx + 1} ไม่สำเร็จ: {e}")
            break

    if sent:
        print(f"🚀 กด Submit ไปทั้งหมด {sent} ข้อ")
    return sent


def click_submit(page: Page, profile: dict) -> bool:
    """กดปุ่มส่งคำตอบ/ตรวจคะแนน (เลือกปุ่มที่ข้อความตรงกับคำว่า 'ส่ง' ก่อน)"""
    css = profile.get("submit")
    if not css:
        return False

    candidates = []
    for btn in page.locator(css).all():
        try:
            if not btn.is_visible():
                continue
            label = re.sub(r"\s+", " ", btn.inner_text()).strip()
            candidates.append((btn, label))
        except Exception:
            continue

    if not candidates:
        return False

    preferred = [c for c in candidates
                 if any(w in c[1].lower() for w in SUBMIT_WORDS)]
    ordered = preferred + [c for c in candidates if c not in preferred]

    for btn, label in ordered:
        try:
            btn.scroll_into_view_if_needed()
            time.sleep(0.5)
            btn.click(force=True)
            print(f"🚀 กดปุ่ม '{label or 'ส่งคำตอบ'}' เรียบร้อยแล้ว")
            time.sleep(1.5)
            confirm_dialog(page)
            return True
        except Exception:
            continue

    return False


def click_continue(page: Page, profile: dict) -> bool:
    """กดปุ่ม Continue/Next ถ้ามี (ใช้กับเว็บแบบทีละหน้า เช่น Open edX)"""
    for key in ("next", "finish"):
        css = profile.get(key)
        if not css:
            continue
        for btn in page.locator(css).all():
            try:
                if not btn.is_visible():
                    continue
                label = re.sub(r"\s+", " ", btn.inner_text()).strip().lower()
                if key == "next" and not re.search(
                    r"continue|next|ถัดไป|ต่อไป|ไปต่อ|ปิด|จบ", label
                ):
                    continue
                btn.scroll_into_view_if_needed()
                btn.click(force=True)
                print(f"➡️ กดปุ่ม '{label}' เพื่อไปหน้าถัดไป")
                time.sleep(2.5)
                return True
            except Exception:
                continue
    return False


def solve_questions(page: Page, questions: list[Question], profile: dict, dry_run: bool) -> int:
    """ตอบข้อสอบทั้งหมดที่เจอบนหน้าปัจจุบัน -> คืนจำนวนข้อที่ตอบได้"""
    answered = 0

    for idx, q in enumerate(questions, 1):
        try:
            print("-" * 40)
            print(f"📌 ข้อที่ {idx}: {q.text}")
            print(f"📌 ตัวเลือก: {q.options}")

            if dry_run:
                continue

            if q.container is not None:
                q.container.scroll_into_view_if_needed()

            # ถ้าเว็บเฉลยไว้แล้ว (choicegroup_correct) ใช้คำตอบนั้นตรง ๆ ไม่ต้องถาม AI
            opt_index = None
            if q.container is not None:
                opt_index = _revealed_correct_index(q.container, q.options)
                if opt_index is not None:
                    print(f"🎯 หน้าเว็บเฉลยไว้แล้ว: '{q.options[opt_index]}'")

            if opt_index is None:
                ai_answer = get_answer_from_ai(q.text, q.options)
                print(f"💡 AI ตอบกลับมาว่า: '{ai_answer}'")
                opt_index = match_answer_to_index(ai_answer, q.options)

            if opt_index is None:
                print("⚠️ ไม่พบตัวเลือกที่ตรงกับคำตอบ AI (ข้ามการคลิกข้อนี้ เพื่อไม่ให้ตอบมั่ว)")
                continue

            if click_option(page, q, opt_index, profile.get("option_css")):
                answered += 1
                print(f"✅ คลิกเลือกคำตอบเรียบร้อย: '{q.options[opt_index]}'")
            else:
                print("⚠️ คลิกตัวเลือกไม่สำเร็จ")

            # บางเว็บ (Open edX) ต้องกด Check ทีละข้อ
            check_css = profile.get("check")
            if check_css:
                scope = q.container if q.container is not None else page
                btn = scope.locator(check_css).first
                if btn.count() and btn.is_visible():
                    try:
                        btn.click(force=True)
                        time.sleep(1.0)
                    except Exception:
                        pass

            time.sleep(0.8)

        except Exception as e:
            print(f"⚠️ เกิดข้อผิดพลาดในข้อที่ {idx}: {e}")

    return answered


def run_bot(
    urls: list[str],
    site: Optional[str],
    dry_run: bool,
    do_submit: bool,
    profile_dir: str,
    learn: bool = False,
    course_id: Optional[str] = None,
    exam_password: Optional[str] = None,
):
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            slow_mo=300,
            viewport={"width": 1440, "height": 900},
            args=["--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        login_done = False

        for url_index, url in enumerate(urls, 1):
            current_site = site or detect_site(url)
            profile = PROFILES.get(current_site, PROFILES["generic"])

            print("\n" + "=" * 62)
            print(f"🌐 ลิงก์ที่ {url_index}/{len(urls)}  [{current_site}]")
            print(f"   {url}")
            print("=" * 62)

            page.goto(url, wait_until="domcontentloaded")
            time.sleep(3)

            # ถ้าเจอหน้า login -> ให้ผู้ใช้ล็อกอินเอง (รองรับ OAuth เช่น LINE/Google)
            if is_login_page(page) and not login_done:
                print("\n🔐 เว็บนี้ต้องเข้าสู่ระบบก่อน")
                print("   กรุณาล็อกอินในหน้าต่างเบราว์เซอร์ให้เรียบร้อย (ครั้งเดียวก็พอ)")
                print("   ระบบจะจดจำ session ไว้ในโฟลเดอร์ browser_profile")
                try:
                    input("   กด [Enter] เมื่อล็อกอินเสร็จ: ")
                except EOFError:
                    pass
                page.goto(url, wait_until="domcontentloaded")
                time.sleep(3)
                login_done = True

            # ---------------- โหมด "เรียนก่อน" ----------------
            if learn:
                cid = course_id or course_id_from_url(page.url) or course_id_from_url(url)
                if not cid:
                    print("❌ หา course_id ไม่ได้ — ระบุด้วย --course-id หรือใช้ URL หน้า courseware/progress")
                else:
                    print(f"🎓 โหมดเรียนคอร์ส: {cid}")
                    learn_course(page, cid, profile, dry_run=dry_run, exam_password=exam_password)

            # ---------------- วนทำข้อสอบ ----------------
            page_no = 0
            while True:
                page_no += 1
                questions = extract_questions(page, profile)

                if not questions:
                    if page_no == 1 and not learn:
                        print("❌ ไม่พบข้อสอบในหน้านี้ (เช็คว่าเข้าถูกหน้าและล็อกอินแล้วหรือยัง)")
                        dump_debug_page(page, "debug_page")
                    break

                print(f"\n📊 พบข้อสอบทั้งหมด {len(questions)} ข้อในหน้านี้\n")
                answered = solve_questions(page, questions, profile, dry_run)

                if dry_run:
                    break

                print("\n" + "-" * 40)
                if do_submit and profile.get("submit"):
                    if not click_submit(page, profile):
                        print("⚠️ ไม่พบปุ่มส่งคำตอบ")

                # เว็บแบบทีละหน้า (Open edX) -> ไปหน้าถัดไป
                if not click_continue(page, profile):
                    break

                if page_no > 50:
                    print("⚠️ หยุดอัตโนมัติ: วนหน้าเยอะเกินไป")
                    break

        print("\n" + "-" * 40)
        print("📌 เบราว์เซอร์เปิดค้างไว้เพื่อดูผลคะแนนจริงบนหน้าเว็บ")
        try:
            input("👉 กด [Enter] ใน Terminal นี้เพื่อปิดเบราว์เซอร์: ")
        except EOFError:
            pass

        ctx.close()


def main():
    parser = argparse.ArgumentParser(
        description="Auto-solver แบบทดสอบออนไลน์ด้วย Gemini AI",
        epilog="ถ้าไม่ใส่ URL ระบบจะอ่านลิงก์จากไฟล์ quiz_links.txt",
    )
    parser.add_argument("url", nargs="?", default=None,
                        help="URL ของหน้าแบบทดสอบ (ถ้าไม่ใส่จะอ่านจาก quiz_links.txt)")
    parser.add_argument("--site", choices=list(PROFILES.keys()), default=None,
                        help="บังคับใช้โปรไฟล์ของเว็บ (default: เดาจาก URL)")
    parser.add_argument("--dry-run", action="store_true", help="ทดสอบดึงข้อมูลอย่างเดียว ไม่คลิกอะไร")
    parser.add_argument("--no-submit", action="store_true", help="ไม่กดปุ่มส่งคำตอบ")
    parser.add_argument("--profile-dir", default="browser_profile",
                        help="โฟลเดอร์เก็บ session (default: browser_profile)")
    parser.add_argument("--learn", action="store_true",
                        help="โหมดเรียนคอร์สให้ครบก่อน (ดูวิดีโอ/อ่านเนื้อหา/ตอบ pre-test) "
                             "แล้วค่อยทำแบบทดสอบ")
    parser.add_argument("--course-id", default=None,
                        help="ระบุ course_id ของ Open edX (default: เดาจาก URL)")
    parser.add_argument("--exam-password", default=None,
                        help="รหัสผ่านของแบบสอบ (ถ้าไม่ระบุจะให้พิมพ์เองในหน้าต่างเบราว์เซอร์)")
    parser.add_argument("--links-file", default=None,
                        help="ไฟล์ลิงก์ที่จะทำ (default: quiz_links.txt)")
    args = parser.parse_args()

    # --- รวบรวมลิงก์ที่จะทำ ---
    urls: list[str] = []
    if args.url:
        urls.append(args.url)
    else:
        urls.extend(load_quiz_links(args.links_file))

    if not urls:
        urls = [DEFAULT_URL]

    print(f"🎯 จะทำทั้งหมด {len(urls)} ลิงก์")
    for i, u in enumerate(urls, 1):
        print(f"   {i}. {u}")

    run_bot(
        urls=urls,
        site=args.site,
        dry_run=args.dry_run,
        do_submit=not args.no_submit,
        profile_dir=args.profile_dir,
        learn=args.learn,
        course_id=args.course_id,
        exam_password=args.exam_password,
    )


if __name__ == "__main__":
    main()
