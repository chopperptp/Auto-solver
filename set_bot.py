"""
set_bot.py — ทำแบบทดสอบอัตโนมัติบน SET e-Learning (elearning.set.or.th)

ระบบนี้ทำงานคล้าย thaimooc_bot.py:
  1) เปิดเบราว์เซอร์ด้วยโปรไฟล์เดิม (browser_profile) แล้วให้ผู้ใช้ล็อกอินเองครั้งเดียว
     (session ถูกจำไว้ ครั้งต่อไปไม่ต้องล็อกอินใหม่)
  2) ลงทะเบียนคอร์สให้อัตโนมัติ (ถ้ายังไม่ลง)
  3) ไล่ทำแบบทดสอบทุกชุดของคอร์ส (ก่อนเรียน / วัดความรู้ / หลังเรียน)
     - ดึง "เฉลย" จาก API สาธารณะของคอร์ส แล้วจับคู่กับตัวเลือกบนหน้าจอ
     - ข้อไหนจับคู่ไม่ได้ ให้ถาม Gemini ช่วยตอบ

ใช้:
    python set_bot.py                                  # อ่านลิงก์จาก set_links.txt
    python set_bot.py "https://elearning.set.or.th/SETGroup/courses/299/info?site=web"
    python set_bot.py <url> --list                     # ดูว่ามีแบบทดสอบอะไรบ้าง (ไม่ต้องล็อกอิน)
    python set_bot.py <url> --only exam                # ทำเฉพาะชุดที่ระบุ (pre,exam,post)
    python set_bot.py <url> --dump                     # เก็บ HTML ทุกหน้าไว้ debug
"""

import argparse
import difflib
import html as htmlmod
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import Page, sync_playwright

import quiz_solver as bot

try:
    for _stream in (sys.stdout, sys.stderr):
        if "utf" not in (_stream.encoding or "").lower():
            _stream.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).parent
LINKS_FILE = HERE / "set_links.txt"
PROFILE_DIR = HERE / "browser_profile"
DUMP_DIR = HERE / "_set_dumps"

BASE = "https://elearning.set.or.th"
GROUP = "SETGroup"

# ชื่อชุดข้อสอบใน API -> ชื่อย่อที่ใช้กับ --only
QUIZ_KINDS = {
    "pre_test": "pre",
    "exam": "exam",
    "post_test": "post",
    "survey": "survey",
}

START_LABELS = (
    "เริ่มทำแบบทดสอบ", "เริ่มทำข้อสอบ", "เริ่มสอบ", "เริ่มทำ",
    "ทำแบบทดสอบ", "เริ่ม", "Start",
)
NEXT_LABELS = ("ถัดไป", "ต่อไป", "ข้อถัดไป", "Next", "Continue")
SUBMIT_LABELS = (
    "ส่งคำตอบ", "ส่งข้อสอบ", "ยืนยันส่งคำตอบ", "จบการสอบ", "ส่งแบบทดสอบ",
    "Finish", "Submit", "ส่ง",
)
CONFIRM_LABELS = ("ตกลง", "ยืนยัน", "ยืนยันการส่ง", "ยืนยันส่งคำตอบ", "ใช่", "OK")


# ============================================================================
# 1. ลิงก์ / ข้อมูลคอร์ส
# ============================================================================
def normalize_link(link: str) -> str:
    link = (link or "").replace("\ufeff", "").strip()
    if not link:
        return ""
    if re.fullmatch(r"\d+", link):
        return f"{BASE}/{GROUP}/courses/{link}/info"
    if link.startswith("/"):
        return BASE + link
    if not link.startswith("http"):
        return f"{BASE}/{GROUP}/courses/{link}/info"
    return link


def course_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"/courses/(\d+)", url)
    if m:
        return m.group(1)
    m = re.fullmatch(r"\d+", (url or "").strip())
    return m.group(0) if m else None


def course_info_url(url: str) -> str:
    cid = course_id_from_url(url)
    return f"{BASE}/{GROUP}/courses/{cid}/info" if cid else url


def load_links(path: str, extra: list[str]) -> list[str]:
    links: list[str] = [normalize_link(x) for x in extra if x.strip()]
    p = Path(path)
    if p.exists():
        for raw in p.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("quiz:"):
                line = line.split(":", 1)[1].strip()
            line = normalize_link(line)
            if line:
                links.append(line)
    # กันซ้ำ
    seen, out = set(), []
    for link in links:
        key = course_id_from_url(link) or link
        if key in seen:
            continue
        seen.add(key)
        out.append(link)
    return out


def fetch_course_json(ctx, cid: str) -> Optional[dict]:
    url = f"{BASE}/api/site/groups/{GROUP}/courses/{cid}"
    for i in range(3):
        try:
            r = ctx.request.get(url, timeout=90000, headers={"Accept": "application/json"})
            if r.ok:
                return r.json()
            print(f"   ⚠️ ดึงข้อมูลคอร์สไม่สำเร็จ (HTTP {r.status})")
        except Exception as e:
            print(f"   ⚠️ ดึงข้อมูลคอร์สไม่สำเร็จ ({type(e).__name__})")
        time.sleep(2)
    return None


def plain_text(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    return htmlmod.unescape(s).strip()


def build_quiz_bank(course: dict) -> list[dict]:
    """แปลงข้อมูลคอร์ส -> รายการชุดข้อสอบ พร้อมเฉลย"""
    quizzes = []
    for key, short in QUIZ_KINDS.items():
        qz = course.get(key)
        if not qz or not qz.get("id"):
            continue
        questions = []
        for q in qz.get("questions") or []:
            answers = q.get("answer") or []
            questions.append({
                "id": q.get("id"),
                "text": plain_text(q.get("questions", "")),
                "options": [
                    {"text": plain_text(a.get("answer", "")), "correct": int(a.get("correct") or 0)}
                    for a in answers
                ],
            })
        quizzes.append({
            "kind": short,
            "api_key": key,
            "id": qz.get("id"),
            "title": plain_text(qz.get("title", "")) or short,
            "questions": questions,
            "time": qz.get("time"),
            "limit": qz.get("limit_questions"),
            "passing": qz.get("passing_score"),
        })
    return quizzes


# ============================================================================
# 2. ล็อกอิน / ลงทะเบียน
# ============================================================================
def is_logged_in(page: Page) -> bool:
    try:
        return page.locator(".btn-trigger-login").count() == 0
    except Exception:
        return False


def goto(page: Page, url: str, tries: int = 3, timeout: int = 90000) -> bool:
    for i in range(tries):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            page.wait_for_timeout(1000)
            return True
        except Exception as e:
            print(f"   ⚠️ เปิดหน้าไม่สำเร็จ ({i + 1}/{tries}): {type(e).__name__}")
            time.sleep(3)
    return False


def wait_for_login(ctx, page: Page, course_url: str) -> bool:
    """เปิดหน้าเว็บแล้วรอจนผู้ใช้ล็อกอิน (ล็อกอินเองในหน้าต่างเบราว์เซอร์)"""
    goto(page, course_url)
    if is_logged_in(page):
        print("✅ ล็อกอินอยู่แล้ว (ใช้ session เดิมจาก browser_profile)\n")
        return True

    print("🔐 ยังไม่ล็อกอิน — กำลังพาไปหน้าเข้าสู่ระบบ SET Member ...")
    try:
        btn = page.locator(".btn-trigger-login:visible")
        if btn.count() == 0:
            btn = page.locator(".btn-trigger-login")
        btn.first.click(timeout=20000)
    except Exception as e:
        print(f"   ⚠️ กดปุ่มเข้าสู่ระบบไม่สำเร็จ: {type(e).__name__}")

    print("=" * 62)
    print("👉 ล็อกอินด้วยอีเมล/รหัสผ่าน SET Member ในหน้าต่างเบราว์เซอร์")
    print("   • ล็อกอินเสร็จ ระบบจะพากลับมาหน้าคอร์สและทำต่อให้เอง")
    print("   • รอสูงสุด 20 นาที (ไม่ต้องกลับมากดอะไรใน Terminal)")
    print("   • session ถูกจำไว้ใน browser_profile — ครั้งเดียวก็พอ")
    print("=" * 62)

    probe = ctx.new_page()
    ok = False
    try:
        for i in range(240):  # 240 * 5s = 20 นาที
            time.sleep(5)
            if i % 6 == 0:
                print(f"   ... รอล็อกอิน ({i * 5}s)")
            try:
                probe.goto(course_url, wait_until="domcontentloaded", timeout=60000)
                probe.wait_for_timeout(500)
                if is_logged_in(probe):
                    ok = True
                    break
            except Exception:
                continue
    finally:
        try:
            probe.close()
        except Exception:
            pass

    if not ok:
        raise SystemExit("❌ ล็อกอินไม่สำเร็จภายในเวลาที่กำหนด — ลองรันใหม่")

    print("✅ ล็อกอินเรียบร้อย เริ่มทำงานต่อได้เลย\n")
    goto(page, course_url)
    return True


def enroll_course(page: Page, ctx, cid: str) -> Optional[int]:
    """ลงทะเบียนคอร์ส (ถ้ายังไม่ลง) -> คืน enroll id"""
    url = course_info_url(f"{BASE}/{GROUP}/courses/{cid}")
    goto(page, url)

    btn = page.locator(".btn-trigger-enroll:visible")
    if btn.count() == 0:
        btn = page.locator(".btn-trigger-enroll")
    if btn.count() == 0:
        btn = page.locator(".btn-enroll-summary:visible")

    if btn.count() == 0:
        # ไม่มีปุ่มลงทะเบียน = น่าจะลงทะเบียนแล้ว -> หา enroll id จากลิงก์ในหน้า
        eid = enroll_id_from_page(page)
        if eid:
            print(f"✅ ลงทะเบียนคอร์สนี้แล้ว (enroll id = {eid})")
            return eid

    if btn.count():
        print("📝 กำลังกดปุ่มลงทะเบียน ...")
        try:
            btn.first.click(timeout=20000)
        except Exception as e:
            print(f"   ⚠️ กดปุ่มไม่สำเร็จ: {type(e).__name__}")
        page.wait_for_timeout(2500)
        accept_checkboxes(page)
        click_confirm_modal(page)
        try:
            page.wait_for_url(re.compile(r"/enroll/\d+"), timeout=45000)
        except Exception:
            pass
        page.wait_for_timeout(2500)
        print(f"   หลังลงทะเบียน: {page.url}")
        eid = enroll_id_from_page(page)
        if eid:
            return eid

    # แผนสำรอง: เรียก API ตรง ๆ
    eid = enroll_via_api(page, cid)
    if eid:
        return eid

    print("❌ ลงทะเบียนไม่สำเร็จ (ลองกดเองในเบราว์เซอร์แล้วรันใหม่)")
    return None


def enroll_id_from_page(page: Page) -> Optional[int]:
    m = re.search(r"/enroll/(\d+)(?:/|$)", page.url)
    if m:
        return int(m.group(1))
    try:
        hrefs = page.eval_on_selector_all(
            "a[href*='/enroll/']", "els => els.map(e => e.getAttribute('href'))"
        )
        for h in hrefs:
            m = re.search(r"/enroll/(\d+)(?:/|$)", h or "")
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def collect_exam_links(page: Page, eid: int) -> dict[str, str]:
    """อ่านลิงก์แบบทดสอบจริงจากหน้าเรียน (กันกรณี URL เดาไม่ตรง)"""
    links: dict[str, str] = {}
    try:
        goto(page, f"{BASE}/{GROUP}/enroll/{eid}/summary")
        hrefs = page.eval_on_selector_all(
            "a[href*='/exam/']", "els => els.map(e => e.getAttribute('href'))"
        )
        for h in hrefs or []:
            m = re.search(r"/exam/(\d+)", h or "")
            if not m:
                continue
            url = h if h.startswith("http") else BASE + (h if h.startswith("/") else "/" + h)
            links.setdefault(m.group(1), url)
    except Exception:
        pass
    if links:
        print(f"   🔗 พบลิงก์แบบทดสอบในหน้าเรียน {len(links)} ลิงก์")
    return links


def enroll_via_api(page: Page, cid: str) -> Optional[int]:
    try:
        data = page.evaluate(
            """async (cid) => {
                const t = await (await fetch('/api/site/csrf/token', {headers:{'Accept':'application/json'}})).json();
                const r = await fetch('/api/site/enroll2learning', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                        'X-CSRF-TOKEN': t.csrf_token || '',
                        'X-Requested-With': 'XMLHttpRequest',
                        'Accept': 'application/json',
                    },
                    body: 'cid=' + encodeURIComponent(cid),
                });
                return {status: r.status, text: await r.text()};
            }""",
            cid,
        )
    except Exception as e:
        print(f"   ⚠️ เรียก API ลงทะเบียนไม่สำเร็จ: {type(e).__name__}: {e}")
        return None

    try:
        resp = json.loads(data["text"])
    except Exception:
        print(f"   ⚠️ API ลงทะเบียนตอบกลับแปลก ๆ: {str(data)[:200]}")
        return None

    if resp.get("eid"):
        print(f"✅ ลงทะเบียนผ่าน API สำเร็จ (enroll id = {resp['eid']})")
        return int(resp["eid"])
    print(f"   ⚠️ ลงทะเบียนไม่สำเร็จ: {str(resp)[:200]}")
    return None


# ============================================================================
# 3. ตัวช่วยกดปุ่ม
# ============================================================================
def dump_page(page: Page, name: str) -> None:
    try:
        DUMP_DIR.mkdir(exist_ok=True)
        (DUMP_DIR / f"{name}.html").write_text(page.content(), encoding="utf-8")
        (DUMP_DIR / f"{name}.txt").write_text(page.inner_text("body"), encoding="utf-8")
        page.screenshot(path=str(DUMP_DIR / f"{name}.png"), full_page=True)
    except Exception:
        pass


def click_first(page: Page, labels: tuple, timeout: int = 6000, require_enabled: bool = True) -> Optional[str]:
    """หาปุ่ม/ลิงก์ที่ข้อความตรงกับ labels แล้วกด -> คืนข้อความที่กด"""
    for label in labels:
        for sel in (
            f"button:has-text('{label}')",
            f"a:has-text('{label}')",
            f"input[type=submit][value*='{label}']",
            f"input[type=button][value*='{label}']",
        ):
            try:
                loc = page.locator(sel)
                if loc.count() == 0:
                    continue
                first = loc.first
                if not first.is_visible():
                    continue
                if require_enabled and not first.is_enabled():
                    continue
                first.scroll_into_view_if_needed(timeout=timeout)
                first.click(timeout=timeout)
                return label
            except Exception:
                continue
    return None


def click_confirm_modal(page: Page) -> Optional[str]:
    try:
        return click_first(page, CONFIRM_LABELS, timeout=4000)
    except Exception:
        return None


_CONSENT_RE = re.compile(r"ยอมรับ|ข้อตกลง|เงื่อนไข|อ่านแล้ว|รับทราบ|consent|agree|accept|terms|policy", re.I)


def accept_checkboxes(page: Page) -> None:
    """ติ๊กเฉพาะ checkbox ยอมรับข้อตกลง (ไม่ยุ่งกับ checkbox ที่เป็นตัวเลือกคำตอบ)"""
    try:
        boxes = page.locator("input[type=checkbox]:visible").all()
    except Exception:
        return

    for cb in boxes:
        try:
            if cb.is_checked():
                continue
            ident = " ".join(
                (cb.get_attribute(k) or "") for k in ("name", "id", "class", "aria-label")
            )
            matched = bool(_CONSENT_RE.search(ident))
            if not matched:
                try:
                    text = cb.evaluate(
                        "el => {"
                        "  const l = el.closest('label') || (el.id ? document.querySelector(`label[for='${el.id}']`) : null);"
                        "  const p = el.closest('div, p, li, form, .modal');"
                        "  return ((l && l.innerText) || '') + ' ' + ((p && p.innerText) || '').slice(0, 300);"
                        "}"
                    )
                    matched = bool(_CONSENT_RE.search(text or ""))
                except Exception:
                    matched = False
            if matched:
                cb.check(force=True, timeout=2500)
        except Exception:
            continue


# ============================================================================
# 4. จับคู่ข้อสอบ + เลือกคำตอบ
# ============================================================================
def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


_OPT_PREFIX = re.compile(r"^\s*(?:[ก-ฮ]|[0-9]{1,2})\s*[\.\)]\s*")


def opt_norm(text: str) -> str:
    """ทำความสะอาดข้อความตัวเลือก: ตัดป้าย 'ก. / 1.' แต่คงเลขโรมัน (I., II.) ไว้"""
    t = htmlmod.unescape(text or "")
    t = re.sub(r'[*_`\'"\n\r]', "", t)
    t = _OPT_PREFIX.sub("", t)
    return re.sub(r"\s+", "", t).lower().strip()


def raw_norm(text: str) -> str:
    t = htmlmod.unescape(text or "")
    t = re.sub(r'[*_`\'"\n\r]', "", t)
    return re.sub(r"\s+", "", t).lower().strip()


def find_bank_question(text: str, options: list[str], bank: list[dict]) -> Optional[dict]:
    """หาข้อในเฉลยที่ตรงกับข้อบนหน้าจอ (เทียบข้อความโจทย์ + ชุดตัวเลือก)"""
    qn = bot.normalize_text(text)
    if not qn:
        return None

    opts_norm = {opt_norm(o) for o in options}

    # 1) วัดความทับซ้อนของ "ชุดตัวเลือก" ก่อน (แม่นกว่าโจทย์ เพราะโจทย์อาจถูกตัดเพี้ยน)
    scored = []
    for item in bank:
        bank_opts = {opt_norm(o["text"]) for o in item["options"]}
        if not bank_opts:
            continue
        overlap = len(opts_norm & bank_opts) / len(bank_opts)
        bn = bot.normalize_text(item["text"])
        sim = _sim(qn, bn) if bn else 0.0
        if qn and bn and (qn in bn or bn in qn):
            sim = max(sim, 0.95)
        scored.append((overlap, sim, item))

    if not scored:
        return None

    # 2) เอาข้อที่ตัวเลือกทับซ้อนมากสุด แล้วใช้ความคล้ายของโจทย์ตัดสิน
    top_overlap = max(s[0] for s in scored)
    if top_overlap >= 0.6:
        pool = [s for s in scored if s[0] >= top_overlap - 0.001]
        pool.sort(key=lambda s: s[1], reverse=True)
        return pool[0][2]

    # 3) ตัวเลือกไม่ตรงเลย — ใช้ความคล้ายของโจทย์แทน
    scored.sort(key=lambda s: s[1], reverse=True)
    if scored[0][1] >= 0.6:
        return scored[0][2]
    return None


def correct_option_index(item: dict, shown_options: list[str]) -> Optional[int]:
    """หาว่าตัวเลือกที่ถูก (ตามเฉลย) ตรงกับตัวเลือกข้อไหนบนหน้าจอ"""
    correct = [o["text"] for o in item.get("options", []) if o.get("correct") == 1]
    if not correct:
        return None
    target = correct[0]

    target_variants = {raw_norm(target), opt_norm(target), bot.normalize_text(target)}
    target_variants = {t for t in target_variants if t}

    # 1) ตรงกันเป๊ะ (ลองหลายรูปแบบการ normalize)
    for i, opt in enumerate(shown_options):
        if raw_norm(opt) in target_variants or opt_norm(opt) in target_variants:
            return i

    # 2) คล้ายกันที่สุด
    best_i, best_s = None, 0.0
    for i, opt in enumerate(shown_options):
        for oval in (raw_norm(opt), opt_norm(opt)):
            for tval in target_variants:
                s = _sim(tval, oval)
                if s > best_s:
                    best_i, best_s = i, s
    if best_s >= 0.8:
        return best_i
    return None


def solve_one_page(page: Page, bank: list[dict], use_ai: bool = True, survey: bool = False) -> int:
    """ตอบทุกข้อที่เห็นในหน้าปัจจุบัน -> คืนจำนวนข้อที่ตอบได้"""
    questions = bot.extract_questions_generic(page)
    if not questions:
        return 0

    answered = 0
    for q in questions:
        item = find_bank_question(q.text, q.options, bank)
        idx = correct_option_index(item, q.options) if item else None

        if idx is None and survey and not use_ai:
            # แบบสอบถาม (ไม่มีข้อถูก/ผิด) -> เลือกตัวเลือกแรก (มักเป็น "มากที่สุด")
            idx = 0

        if idx is None and use_ai:
            print("   🤖 ข้อนี้จับคู่เฉลยไม่ได้ — ถาม Gemini ...")
            ai = bot.get_answer_from_ai(q.text, q.options)
            idx = bot.match_answer_to_index(ai, q.options) if ai else None
            if idx is None and ai:
                # ให้ AI ตอบเป็นตัวเลือกโดยตรงอีกครั้ง
                idx = correct_option_index({"options": [{"text": ai, "correct": 1}]}, q.options)

        if idx is None:
            print(f"   ⚠️ ตอบไม่ได้: {q.text[:80]}")
            continue

        if bot.click_option(page, q, idx, None):
            answered += 1
            print(f"   ✅ {q.text[:70]} -> {q.options[idx][:70]}")
        else:
            print(f"   ⚠️ คลิกตัวเลือกไม่สำเร็จ: {q.text[:70]}")

    return answered


def read_result(page: Page) -> str:
    try:
        body = page.inner_text("body")
    except Exception:
        return ""
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    hits = [ln for ln in lines if re.search(r"คะแนน|ผ่าน|ไม่ผ่าน|ผลการ|score|passed", ln, re.I)]
    return " | ".join(hits[:8])


# ============================================================================
# 5. ทำแบบทดสอบ 1 ชุด
# ============================================================================
def solve_quiz(page: Page, eid: int, quiz: dict, args, exam_links: Optional[dict] = None) -> bool:
    qid = quiz["id"]
    link = (exam_links or {}).get(str(qid)) or ""
    if link.rstrip("/").endswith("/score"):
        link = ""
    url = link or f"{BASE}/{GROUP}/enroll/{eid}/exam/{qid}"
    print(f"\n📝 {quiz['title']} (quiz id={qid}, {len(quiz['questions'])} ข้อ)")
    print(f"   🔗 {url}")

    if not goto(page, url):
        print("   ❌ เปิดหน้าแบบทดสอบไม่สำเร็จ")
        return False

    if "/exam/" not in page.url:
        print(f"   ⚠️ หน้าไม่ใช่แบบทดสอบ (อยู่ที่ {page.url}) — ลองเปิดจากลิงก์ที่พบในหน้าเรียน")
        fallback = f"{BASE}/{GROUP}/enroll/{eid}/exam/{qid}"
        if fallback != url:
            goto(page, fallback)
        if "/exam/" not in page.url:
            print("   ❌ เข้าหน้าแบบทดสอบไม่ได้ (อาจยังไม่ลงทะเบียน/ไม่มีสิทธิ์)")
            if args.dump:
                dump_page(page, f"quiz_{qid}_notfound")
            return False

    page.wait_for_timeout(2500)

    # หน้าเริ่มสอบ / คำชี้แจง (อาจต้องติ๊กยอมรับก่อน)
    accept_checkboxes(page)
    clicked = click_first(page, START_LABELS)
    if clicked:
        print(f"   ▶️ กด '{clicked}'")
        page.wait_for_timeout(2000)
        accept_checkboxes(page)
        click_confirm_modal(page)
        page.wait_for_timeout(2500)

    # ติ๊กยอมรับเงื่อนไข (ถ้ามี)
    accept_checkboxes(page)

    if args.dump:
        dump_page(page, f"quiz_{qid}_start")

    bank = quiz["questions"]
    is_survey = quiz["kind"] == "survey"
    answered_total = 0
    seen_pages = set()
    stuck = 0

    # รอให้ข้อสอบโหลด (บางหน้าดึงผ่าน AJAX)
    try:
        page.wait_for_selector("input[type=radio]", timeout=15000)
    except Exception:
        pass

    for step in range(1, 61):
        page.wait_for_timeout(1200)

        # ถ้ามีข้อสอบให้ตอบ
        n = solve_one_page(
            page,
            bank,
            use_ai=(not args.no_ai) and (not is_survey),
            survey=is_survey,
        )
        answered_total += n

        # สถานะหน้า: เก็บไว้กันวนซ้ำ
        try:
            sig = page.evaluate(
                "() => (document.body.innerText || '').replace(/\\s+/g,' ').slice(0, 400)"
            )
        except Exception:
            sig = ""
        seen_pages.add(sig)

        # กด "ถัดไป" (ถ้ามี)
        nxt = click_first(page, NEXT_LABELS)
        if nxt:
            print(f"   ➡️ {nxt}")
            page.wait_for_timeout(1500)
            try:
                page.wait_for_selector("input[type=radio]", timeout=8000)
            except Exception:
                # อาจเด้ง modal ยืนยันการส่งข้อสอบ
                if click_confirm_modal(page):
                    print("   📤 ยืนยันการส่งข้อสอบ")
                    page.wait_for_timeout(3500)
                    click_confirm_modal(page)
                    try:
                        page.wait_for_url(re.compile(r"/score"), timeout=15000)
                    except Exception:
                        pass
                    page.wait_for_timeout(1500)
                    if args.dump:
                        dump_page(page, f"quiz_{qid}_after_submit")
                    break
            try:
                new_sig = page.evaluate(
                    "() => (document.body.innerText || '').replace(/\\s+/g,' ').slice(0, 400)"
                )
            except Exception:
                new_sig = ""
            if n == 0 and new_sig == sig:
                stuck += 1
                if stuck >= 2:
                    print("   ⚠️ หน้าเดิมไม่เปลี่ยน — หยุดเพื่อกันวนซ้ำ")
                    break
            else:
                stuck = 0
            continue

        # กดส่งคำตอบ
        sub = click_first(page, SUBMIT_LABELS)
        if sub:
            print(f"   📤 กด '{sub}'")
            page.wait_for_timeout(1200)
            click_confirm_modal(page)
            page.wait_for_timeout(3500)
            click_confirm_modal(page)
            try:
                page.wait_for_url(re.compile(r"/score"), timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
            if args.dump:
                dump_page(page, f"quiz_{qid}_after_submit")
            break

        # ไม่มีปุ่มอะไรแล้ว — จบหรือหน้าผลลัพธ์
        if args.dump:
            dump_page(page, f"quiz_{qid}_step{step}")
        print("   ℹ️ ไม่พบปุ่มถัดไป/ส่งคำตอบ — หยุดลูป")
        break

    result = read_result(page)
    if result:
        print(f"   📊 ผลลัพธ์: {result}")
    if args.dump:
        dump_page(page, f"quiz_{qid}_final")
    print(f"   รวมตอบ {answered_total} ครั้ง")
    return True


# ============================================================================
# 6. ทำงานต่อคอร์ส
# ============================================================================
def process_course(page: Page, ctx, url: str, args) -> bool:
    cid = course_id_from_url(url)
    if not cid:
        print(f"❌ อ่านรหัสคอร์สจากลิงก์ไม่ได้: {url}")
        return False

    print(f"\n{'=' * 62}")
    print(f"🎓 คอร์ส {cid}: {url}")
    print("=" * 62)

    course = fetch_course_json(ctx, cid)
    if not course:
        print("❌ ดึงข้อมูลคอร์สไม่ได้ — ตรวจอินเทอร์เน็ตแล้วลองใหม่")
        return False

    print(f"   ชื่อคอร์ส: {plain_text(course.get('title', ''))}")
    quizzes = build_quiz_bank(course)
    if not quizzes:
        print("ℹ️ คอร์สนี้ไม่มีแบบทดสอบ")
        return True

    print("   แบบทดสอบที่พบ:")
    for q in quizzes:
        n_correct = sum(1 for x in q["questions"] for o in x["options"] if o["correct"])
        print(f"     • [{q['kind']}] {q['title']} — {len(q['questions'])} ข้อ, เฉลย {n_correct} ตัวเลือก")

    if args.only:
        wanted = {x.strip().lower() for x in args.only.split(",") if x.strip()}
        quizzes = [q for q in quizzes if q["kind"] in wanted]
        if not quizzes:
            print(f"⚠️ ไม่พบชุดที่ตรงกับ --only {args.only}")
            return True
    elif not args.survey:
        # ค่าเริ่มต้น: ทำเฉพาะแบบทดสอบ ไม่ทำแบบสอบถามความพึงพอใจ
        quizzes = [q for q in quizzes if q["kind"] != "survey"]

    eid = args.eid or enroll_course(page, ctx, cid)
    if not eid:
        print("   ℹ️ ถ้ารู้ enroll id แล้ว สามารถรันซ้ำด้วย --eid <ตัวเลข> ได้")
        return False

    exam_links = collect_exam_links(page, eid)

    ok = 0
    for quiz in quizzes:
        try:
            if solve_quiz(page, eid, quiz, args, exam_links):
                ok += 1
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"   ⚠️ ทำชุดนี้ไม่สำเร็จ: {type(e).__name__}: {e}")
            if args.dump:
                dump_page(page, f"quiz_{quiz['id']}_error")

    print(f"\n🏁 คอร์ส {cid}: ทำแบบทดสอบสำเร็จ {ok}/{len(quizzes)} ชุด")
    return ok == len(quizzes)


# ============================================================================
# 7. main
# ============================================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="ทำแบบทดสอบ SET e-Learning อัตโนมัติ")
    ap.add_argument("urls", nargs="*", help="ลิงก์คอร์ส (ไม่ใส่ = อ่านจาก set_links.txt)")
    ap.add_argument("--links-file", default=str(LINKS_FILE), help="ไฟล์รายการลิงก์คอร์ส")
    ap.add_argument("--profile-dir", default=str(PROFILE_DIR), help="โฟลเดอร์โปรไฟล์เบราว์เซอร์")
    ap.add_argument("--only", default="", help="ทำเฉพาะชุด: pre,exam,post (คั่นด้วย ,)")
    ap.add_argument("--no-ai", action="store_true", help="ไม่ใช้ Gemini เลย (ใช้เฉพาะเฉลยจาก API)")
    ap.add_argument("--survey", action="store_true", help="ทำแบบสอบถามความพึงพอใจด้วย (เลือกตัวเลือกแรก)")
    ap.add_argument("--list", action="store_true", help="แสดงรายการแบบทดสอบแล้วจบ (ไม่ต้องล็อกอิน)")
    ap.add_argument("--dump", action="store_true", help="เก็บ HTML ทุกหน้าไว้ใน _set_dumps")
    ap.add_argument("--eid", type=int, default=0, help="ระบุ enroll id เอง (ข้ามขั้นลงทะเบียน)")
    args = ap.parse_args()

    links = load_links(args.links_file, list(args.urls))
    if not links:
        print("❌ ไม่พบลิงก์คอร์ส")
        print(f"   ใส่ลิงก์ในไฟล์ {Path(args.links_file).name} หรือพิมพ์ลิงก์ตอนรัน")
        return

    print(f"🎯 จะทำทั้งหมด {len(links)} คอร์ส")
    for i, link in enumerate(links, 1):
        print(f"   {i}. {link}")

    if args.list:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=args.profile_dir, headless=True
            )
            for link in links:
                cid = course_id_from_url(link)
                course = fetch_course_json(ctx, cid) if cid else None
                if not course:
                    print(f"\n❌ {link} — ดึงข้อมูลไม่ได้")
                    continue
                print(f"\n🎓 {cid}: {plain_text(course.get('title', ''))}")
                for q in build_quiz_bank(course):
                    print(f"   • [{q['kind']}] {q['title']} — {len(q['questions'])} ข้อ")
            ctx.close()
        return

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=args.profile_dir,
            headless=False,
            slow_mo=150,
            viewport={"width": 1440, "height": 900},
            args=["--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        course_url = course_info_url(links[0])
        wait_for_login(ctx, page, course_url)

        ok = 0
        for i, link in enumerate(links, 1):
            print(f"\n>>> คอร์สที่ {i}/{len(links)}")
            try:
                if process_course(page, ctx, link, args):
                    ok += 1
            except KeyboardInterrupt:
                print("\n⛔ ผู้ใช้ยกเลิก")
                break
            except Exception as e:
                print(f"⚠️ คอร์สนี้ล้มเหลว: {type(e).__name__}: {e}")

        print("\n" + "=" * 62)
        print(f"🎉 เสร็จ {ok}/{len(links)} คอร์ส")
        print("=" * 62)

        try:
            input("👉 กด [Enter] เพื่อปิดเบราว์เซอร์: ")
        except EOFError:
            pass
        ctx.close()


if __name__ == "__main__":
    main()
