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
import urllib.request
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
STATE_FILE = HERE / "_set_state.json"

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


BANK_CACHE_DIR = HERE / "_set_banks"


def _api_headers(cid: str) -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
        "Referer": f"{BASE}/{GROUP}/courses/{cid}/info",
        "X-Requested-With": "XMLHttpRequest",
    }


def fetch_course_json(ctx, cid: str, retries: int = 4) -> Optional[dict]:
    """ดึงข้อมูลคอร์สจาก API สาธารณะ (ไม่ต้องล็อกอิน)

    ใช้ urllib แทน ctx.request เพราะ API มีระบบกันบอท (Incapsula)
    ที่มักท้าทาย request จากเบราว์เซอร์แบบ headless
    """
    url = f"{BASE}/api/site/groups/{GROUP}/courses/{cid}"
    headers = _api_headers(cid)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
            text = body.lstrip("\ufeff \t\r\n")
            if text.startswith("{") or text.startswith("["):
                return json.loads(text)
            head = text[:300].lower()
            if "<html" in head or "_incapsula_resource" in head:
                print("   ⚠️ API ถูกระบบกันบอทชั่วคราว — รอแล้วลองใหม่")
                time.sleep(3 + attempt * 2)
                continue
            print("   ⚠️ API ตอบรูปแบบไม่รู้จัก")
        except Exception as e:
            print(f"   ⚠️ ดึงข้อมูลคอร์สไม่สำเร็จ ({type(e).__name__})")
        time.sleep(2 + attempt)
    return None


def merge_quiz_questions(dst: dict, src: dict) -> bool:
    """รวมข้อสอบจาก src เข้า dst (กันซ้ำด้วย id) -> True ถ้ามีข้อใหม่"""
    added = False
    for key in QUIZ_KINDS:
        dq = dst.get(key) or {}
        sq = src.get(key) or {}
        if not dq or not sq:
            continue
        if not isinstance(dq.get("questions"), list):
            dq["questions"] = []
        seen = {str(q.get("id")) for q in dq["questions"]}
        for q in sq.get("questions") or []:
            qid = str(q.get("id"))
            if qid and qid not in seen:
                dq["questions"].append(q)
                seen.add(qid)
                added = True
    return added


def bank_cache_path(cid: str) -> Path:
    return BANK_CACHE_DIR / f"{cid}.json"


def load_bank_cache(course: dict, cid: str) -> bool:
    """รวมคลังข้อสอบที่เคยเก็บไว้ใน _set_banks/<cid>.json"""
    p = bank_cache_path(cid)
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    if str(data.get("course_id")) != str(cid):
        return False
    return merge_quiz_questions(course, data.get("quizzes") or {})


def save_bank_cache(course: dict, cid: str) -> None:
    quizzes = {}
    for key in QUIZ_KINDS:
        qs = (course.get(key) or {}).get("questions") or []
        if qs:
            quizzes[key] = {"questions": qs}
    if not quizzes:
        return
    try:
        BANK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        bank_cache_path(cid).write_text(
            json.dumps(
                {"course_id": str(cid), "quizzes": quizzes},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"   ⚠️ บันทึกแคชข้อสอบไม่สำเร็จ ({type(e).__name__})")


def fetch_course_json_full(
    ctx, cid: str, quiet: int = 10, max_calls: int = 20
) -> Optional[dict]:
    """ดึงข้อมูลคอร์สซ้ำๆ เพื่อรวมคลังข้อสอบที่เว็บสุ่มมาให้ทีละชุด

    quiz ที่ random_questions=1 (เช่น final exam) จะคืนข้อสอบมาแค่ 10 ข้อจากคลังจริง
    การเรียกซ้ำหลายรอบแล้ว merge ด้วย id จะได้เฉลยครบทุกข้อ
    คลังที่รวมแล้วจะถูกแคชไว้ใน _set_banks/<cid>.json กัน API ล่ม/กันบอท
    """
    course = fetch_course_json(ctx, cid)
    if not course:
        return None

    cached = load_bank_cache(course, cid)

    random_keys = [k for k in QUIZ_KINDS if (course.get(k) or {}).get("random_questions")]
    if not random_keys:
        if cached:
            print("   📚 ใช้คลังข้อสอบที่แคชไว้")
        return course

    calls = 1
    stable = 0
    fails = 0
    total = sum(len((course.get(k) or {}).get("questions") or []) for k in random_keys)
    while quiet > stable and fails < 3 and calls < max_calls:
        time.sleep(0.7)
        nxt = fetch_course_json(ctx, cid, retries=1)
        calls += 1
        if not nxt:
            fails += 1
            continue
        fails = 0
        if merge_quiz_questions(course, nxt):
            stable = 0
        else:
            stable += 1

    new_total = sum(len((course.get(k) or {}).get("questions") or []) for k in random_keys)
    if new_total > total:
        print(f"   📚 รวมคลังข้อสอบจาก API {calls} ครั้ง: {total} -> {new_total} ข้อ")
    if cached:
        print(f"   📚 รวมคลังข้อสอบที่แคชไว้ด้วย (ทั้งหมด {new_total} ข้อ)")
    save_bank_cache(course, cid)
    return course



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
                    {
                        "id": a.get("id"),
                        "text": plain_text(a.get("answer", "")),
                        "correct": int(a.get("correct") or 0),
                    }
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


def course_plan(course: dict) -> list[dict]:
    """วิเคราะห์ว่าแต่ละหมวดเป็น "เรียนล้วน" หรือ "เรียน + มีแบบทดสอบ"

    SET เก็บ quiz ที่ผูกกับหมวดไว้ใน main_topics[].quiz_id (บางคอร์สมี auto_quiz)
    ถ้า quiz_id ตรงกับ pre/post/exam/survey ที่มีเฉลยอยู่แล้ว จะรู้ชนิดได้ทันที
    """
    quiz_kind_by_id = {}
    for key, short in QUIZ_KINDS.items():
        qz = course.get(key)
        if isinstance(qz, dict) and qz.get("id"):
            quiz_kind_by_id[str(qz["id"])] = short

    plan = []
    for main in course.get("main_topics") or []:
        topics = [s for s in (main.get("sub_topics") or []) if s.get("id")]
        if not topics:
            continue
        quiz_id = str(main.get("quiz_id") or "")
        auto_quiz = bool(main.get("auto_quiz"))
        plan.append({
            "id": str(main.get("id") or ""),
            "title": plain_text(main.get("title", "")),
            "topics": topics,
            "quiz_id": quiz_id,
            "quiz_kind": quiz_kind_by_id.get(quiz_id, "quiz" if quiz_id else ""),
            "auto_quiz": auto_quiz,
            "has_quiz": bool(quiz_id or auto_quiz),
            "known_quiz": bool(quiz_id and quiz_id in quiz_kind_by_id),
        })
    return plan


def describe_course_plan(course: dict, plan: list[dict], quizzes: list[dict]) -> None:
    """พิมพ์แผนให้เห็นชัดว่าอะไรเรียนล้วน อะไรมีข้อสอบ และจะทำชุดไหน"""
    topic_count = sum(len(p["topics"]) for p in plan)
    with_quiz = [p for p in plan if p["has_quiz"]]
    learn_only = [p for p in plan if not p["has_quiz"]]

    print(f"\n   📋 แผนการเรียน: {len(plan)} หมวด / {topic_count} บท")
    if not topic_count:
        print("      • ไม่มีบทเรียนในคอร์สนี้")

    if topic_count and not with_quiz:
        print("      • บทเรียน: เรียนอย่างเดียว (ไม่มีคำถามแยกในบท)")
    else:
        if learn_only:
            print(f"      • เรียนอย่างเดียว: {len(learn_only)} หมวด")
        for p in with_quiz:
            if p["known_quiz"]:
                print(f"      • เรียน + ข้อสอบ: {p['title']} -> [{p['quiz_kind']}]")
            elif p["auto_quiz"]:
                print(
                    f"      • เรียน + ข้อสอบอัตโนมัติ: {p['title']} "
                    "(จะทำเมื่อเว็บปลดล็อกและมีเฉลย)"
                )
            else:
                print(
                    f"      • เรียน + ข้อสอบ id={p['quiz_id']}: {p['title']} "
                    "(ยังไม่มีเฉลย — จะไม่เริ่มเพื่อกันเสียสิทธิ์)"
                )

    if not quizzes:
        print("      • แบบทดสอบ: ไม่มี — เรียนจบเป็นอันเสร็จ")
        return

    labels = ", ".join(
        f"{q['kind']} {len(q['questions'])} ข้อ" for q in quizzes
    )
    print(f"      • แบบทดสอบ: {labels}")
    if topic_count and not with_quiz:
        print("      • รูปแบบ: เรียนเนื้อหาให้ครบ แล้วทำแบบทดสอบของคอร์สตามลำดับ")


# ============================================================================
# 2. ล็อกอิน / ลงทะเบียน
# ============================================================================
def is_logged_in(page: Page) -> bool:
    """ล็อกอินแล้วหรือยัง (ระวังหน้า login ของ member.set.or.th ที่ไม่มีปุ่ม login)"""
    try:
        url = page.url or ""
        if "member.set.or.th" in url or "/login" in url.split("?")[0]:
            return False
        if "elearning.set.or.th" not in url:
            return False
        if page.locator("#btn-logout").count() > 0:
            return True
        if page.locator(".btn-trigger-login").count() == 0:
            # บางหน้า (เช่น SSO) ไม่มีปุ่มนี้แต่ยังไม่ได้ล็อกอิน
            try:
                title = page.title() or ""
            except Exception:
                title = ""
            return "ลงชื่อเข้าใช้งาน" not in title and "login" not in url.lower()
        return False
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


def settle(page: Page, url: str, timeout: int = 45000) -> bool:
    """เปิดหน้าและรอจนผ่าน SSO redirect กลับมาที่ elearning (คืน False ถ้าไปติดหน้า login)"""
    if not goto(page, url):
        return False
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        u = page.url or ""
        if "member.set.or.th" in u:
            if "/login" in u:
                return False
            page.wait_for_timeout(700)
            continue
        if "elearning.set.or.th" in u:
            page.wait_for_timeout(800)
            return True
        page.wait_for_timeout(400)
    return False


def restore_session(ctx) -> None:
    """คืนคุกกี้ที่บันทึกไว้ (cookie session ของ SET มักไม่ถูกเก็บลง browser_profile)"""
    if not STATE_FILE.exists():
        return
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        cookies = state.get("cookies") or []
        if cookies:
            ctx.add_cookies(cookies)
    except Exception:
        pass


def save_session(ctx) -> None:
    try:
        state = ctx.storage_state()
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def wait_for_login(ctx, page: Page, course_url: str) -> bool:
    """เปิดหน้าเว็บแล้วรอจนผู้ใช้ล็อกอิน (ล็อกอินเองในหน้าต่างเบราว์เซอร์)"""
    goto(page, course_url)
    if is_logged_in(page):
        print("✅ ล็อกอินอยู่แล้ว (ใช้ session เดิมจาก browser_profile)\n")
        save_session(ctx)
        return True

    print("🔐 ยังไม่ล็อกอิน — กำลังพาไปหน้าเข้าสู่ระบบ SET Member ...")
    if "member.set.or.th" in (page.url or ""):
        print("   ℹ️ หน้านี้คือหน้า login ของ SET Member — ล็อกอินในหน้าต่างนี้ได้เลย")
    else:
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

    ok = False
    for i in range(240):  # 240 * 5s = 20 นาที
        time.sleep(5)
        if i % 6 == 0:
            print(f"   ... รอล็อกอิน ({i * 5}s)")
        try:
            # ตรวจหน้าล็อกอินเดิมโดยไม่เปิดแท็บใหม่ทับ (ผู้ใช้จะเห็นฟอร์มล็อกอินตลอด)
            if is_logged_in(page):
                ok = True
                break
        except Exception:
            continue

    if not ok:
        raise SystemExit("❌ ล็อกอินไม่สำเร็จภายในเวลาที่กำหนด — ลองรันใหม่")

    print("✅ ล็อกอินเรียบร้อย เริ่มทำงานต่อได้เลย\n")
    goto(page, course_url)
    save_session(ctx)
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
    hits = [
        ln
        for ln in lines
        if re.search(r"คะแนน|ผ่าน|ไม่ผ่าน|ผลการ|ทำแล้ว|score|passed", ln, re.I)
    ]
    return " | ".join(hits[:8])


# ============================================================================
# 5. ทำแบบทดสอบ 1 ชุด (ฟอร์มข้อสอบของ SET ถูก render มาพร้อมหน้า)
# ============================================================================
# URL หน้า "คำชี้แจง/เริ่มสอบ" ของแต่ละชุด (หน้าสุดท้ายจะเปลี่ยนเป็น /exam/<attempt>/score)
QUIZ_PATHS = {
    "pre": ("pre-test",),
    "post": ("post-test",),
    "exam": ("exam",),
    "survey": ("survey",),
}

QUIZ_ORDER = {"pre": 0, "post": 1, "exam": 2, "survey": 3}

EXAM_FORM_JS = r"""
() => {
  const form = document.querySelector('#exam-form');
  if (!form) return null;
  const questions = [...form.querySelectorAll('fieldset')].map((fs) => {
    const block = fs.querySelector('.questions-block');
    const h = fs.querySelector('h4');
    const inputs = [...fs.querySelectorAll('input.answer-choice')];
    return {
      qid: (block && block.getAttribute('data-questions-id')) || fs.getAttribute('data-id') || '',
      text: h ? (h.innerText || '').trim() : '',
      options: inputs.map((i) => {
        let lab = i.closest('label');
        if (!lab && i.id) lab = form.querySelector("label[for='" + i.id + "']");
        return {
          id: String(i.value),
          type: i.type,
          text: lab ? (lab.innerText || '').trim() : '',
          checked: i.checked,
        };
      }),
    };
  });
  return {
    all: parseInt(form.getAttribute('data-all-questions') || '0', 10),
    questions,
  };
}
"""

APPLY_PLAN_JS = r"""
(plan) => {
  const res = [];
  for (const p of plan) {
    const el = document.querySelector("#exam-form input[name='" + p.qid + "'][value='" + p.aid + "']");
    if (!el) { res.push({qid: p.qid, ok: false, why: 'ไม่พบ input'}); continue; }
    if (!el.checked) el.click();
    if (!el.checked) {
      el.checked = true;
      el.dispatchEvent(new Event('change', {bubbles: true}));
      el.dispatchEvent(new Event('click', {bubbles: true}));
    }
    res.push({qid: p.qid, ok: !!el.checked, why: el.checked ? '' : 'ไม่ได้เลือก'});
  }
  try { checkSubmitButton(); } catch (e) {}
  return res;
}
"""

VERIFY_ANSWERS_JS = r"""
() => {
  const form = document.querySelector('#exam-form');
  if (!form) return [];
  const out = [];
  for (const fs of form.querySelectorAll('fieldset')) {
    const first = fs.querySelector('input.answer-choice');
    if (!first) continue;
    out.push({
      qid: first.name,
      checked: fs.querySelectorAll("input[name='" + first.name + "']:checked").length,
    });
  }
  return out;
}
"""

EXAM_HISTORY_JS = r"""
() => {
  const rows = [...document.querySelectorAll('.table-history tbody tr')];
  return rows.map((tr) => {
    const cells = [...tr.querySelectorAll('td')].map(
      (td) => (td.innerText || '').trim()
    );
    return {
      cells,
      text: (tr.innerText || '').trim(),
      success: !!tr.querySelector('td.text-success'),
    };
  }).filter((r) => r.text);
}
"""


def exam_history_passed(rows: list[dict]) -> bool:
    """ประวัติการสอบมีแถวที่ "ผ่าน" แล้วหรือยัง (แยกจาก "ไม่ผ่าน")"""
    for row in rows or []:
        cells = [str(c).strip() for c in (row.get("cells") or [])]
        status = cells[-1] if cells else str(row.get("text") or "").strip()
        if row.get("success"):
            return True
        if "ผ่าน" in status and "ไม่ผ่าน" not in status:
            return True
    return False


def exam_history_summary(rows: list[dict]) -> str:
    """สรุปประวัติแบบสั้น ๆ เช่น 2 ครั้ง, ล่าสุด 10/10 (ผ่าน)"""
    if not rows:
        return "ยังไม่มีประวัติ"
    latest = rows[0]
    cells = [str(c).strip() for c in (latest.get("cells") or [])]
    if len(cells) >= 4:
        return f"{len(rows)} ครั้ง, ล่าสุด {cells[-2]} ({cells[-1]})"
    if cells:
        return f"{len(rows)} ครั้ง, ล่าสุด {cells[-1]}"
    return f"{len(rows)} ครั้ง"


def read_exam_history(page: Page, timeout_ms: int = 5000) -> list[dict]:
    """รอให้ตารางประวัติสอบโหลดผ่าน AJAX แล้วคืนแถวทั้งหมด"""
    try:
        if page.locator(".table-history").count() == 0:
            return []
    except Exception:
        return []

    deadline = time.time() + max(1, timeout_ms) / 1000
    rows: list[dict] = []
    while time.time() < deadline:
        try:
            rows = page.evaluate(EXAM_HISTORY_JS) or []
        except Exception:
            rows = []
        if rows:
            return rows
        page.wait_for_timeout(400)
    return rows


TOPIC_STATUS_PATTERNS = (
    re.compile(r"enroll2topic_status['\"]?\s*[,:]\s*\{?\s*['\"]?id['\"]?\s*[:=]\s*['\"]?(\d+)", re.I),
    re.compile(r"enroll2topic_status[^\n]{0,200}?['\"]?id['\"]?\s*[:=]\s*['\"]?(\d+)", re.I),
    re.compile(r"enroll2topic_id['\"]?\s*[:=]\s*['\"]?(\d+)", re.I),
    re.compile(r"data-enroll2topic(?:-id)?=['\"](\d+)", re.I),
    re.compile(r"enroll2topic['\"]?\s*[:=]\s*\{[^}]{0,300}?['\"]?id['\"]?\s*[:=]\s*['\"]?(\d+)", re.I),
    re.compile(r"enroll2topic[^\n]{0,120}?['\"]?id['\"]?\s*[:=]\s*['\"]?(\d+)", re.I),
)


def find_enroll2topic_id(html: str) -> Optional[str]:
    for pat in TOPIC_STATUS_PATTERNS:
        m = pat.search(html or "")
        if m:
            return m.group(1)
    return None

TOPIC_COMPLETE_JS = r"""
async ({id, duration}) => {
  const post = async (url, body) => {
    const r = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': 'application/json',
      },
      body: new URLSearchParams(body).toString(),
    });
    const text = await r.text();
    let json = null;
    try { json = JSON.parse(text); } catch (e) {}
    return {status: r.status, json};
  };
  return {
    duration: await post('/api/site/enroll2topic_duration', {id, duration}),
    status: await post('/api/site/enroll2topic_status', {id, status: 1}),
  };
}
"""


COURSE_TOPIC_ROWS_JS = r"""
() => {
  const out = [];
  const seen = new Set();

  const progressOf = (block) => {
    if (!block) return 0;
    if (block.classList.contains('done')) return 100;
    const icon = block.querySelector('.timeline-icon');
    for (const el of [icon, block]) {
      if (!el) continue;
      const t = (el.getAttribute('data-original-title') || el.getAttribute('title') || '').trim();
      const m = t.match(/(\d+(?:\.\d+)?)\s*%/);
      if (m) return Math.round(parseFloat(m[1]));
    }
    // ใช้เฉพาะ progress bar ใต้ icon ไม่ใช่เส้นแนวตั้งของ timeline
    const bar = block.querySelector('.timeline-icon .timeline-line-progress');
    const h = (bar && bar.getAttribute('style')) || '';
    const m = h.match(/height\s*:\s*(\d+(?:\.\d+)?)%/);
    if (m) return Math.round(parseFloat(m[1]));
    return 0;
  };

  const currentTid = String(window.topics_id || '');
  for (const a of document.querySelectorAll('a.timeline-sub-topic, a[href*="/topics/"]')) {
    const href = a.getAttribute('href') || '';
    const m = href.match(/\/topics\/(\d+)/);
    const block = a.closest('.timeline-sub-topic-block');
    // บทที่กำลังเปิดอยู่ใช้ href="javascript:void(0);" จึงต้องอ่านจาก topics_id
    let tid = m ? m[1] : '';
    if (!tid && block && block.classList.contains('active') && currentTid) {
      tid = currentTid;
    }
    if (!tid) continue;
    if (seen.has(tid)) continue;
    seen.add(tid);
    const row = a.closest('.d-flex.align-items-start') || a.closest('tr') || a.parentElement;
    out.push({
      tid,
      title: (a.innerText || '').trim(),
      href,
      cls: [a.className || '', block ? block.className : ''].join(' ').trim(),
      progress: progressOf(block),
      has_row: !!row,
      html: row ? row.outerHTML.slice(0, 16000) : '',
    });
  }
  return out;
}
"""


STAGE_TOPIC_JS = r"""
async ({eid, tid}) => {
  const r = await fetch('/api/site/enroll2topic_stage', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
      'X-Requested-With': 'XMLHttpRequest',
      'Accept': 'application/json',
    },
    body: new URLSearchParams({enroll: eid, topics: tid}).toString(),
  });
  const text = await r.text();
  let json = null;
  try { json = JSON.parse(text); } catch (e) {}
  return {status: r.status, json};
}
"""


TOPIC_WATCH_JS = r"""
async () => {
  const v = document.querySelector('video');
  if (!v) return {ok: false, reason: 'no-video'};
  const meta = await new Promise((resolve) => {
    if (isFinite(v.duration) && v.duration > 0) return resolve(true);
    const done = () => resolve(isFinite(v.duration) && v.duration > 0);
    v.addEventListener('loadedmetadata', done, {once: true});
    setTimeout(() => resolve(isFinite(v.duration) && v.duration > 0), 8000);
  });
  if (!meta) return {ok: false, reason: 'no-metadata'};
  const duration = v.duration;
  try { v.muted = true; } catch (e) {}
  try { v.playbackRate = 16; } catch (e) {}
  const inp = document.querySelector('#duration');
  if (inp) inp.value = String(Math.floor(duration));
  try { await v.play(); } catch (e) {}
  await new Promise((r) => setTimeout(r, 700));
  const target = Math.max(0, duration - 1);
  try { v.currentTime = target; } catch (e) {}
  try {
    if (window.playerInstanceVe && playerInstanceVe.player) {
      const p = playerInstanceVe.player;
      if (p.setPlaybackRate) p.setPlaybackRate(16);
      if (p.seek) p.seek(target);
      if (p.play) p.play();
    }
  } catch (e) {}
  return {ok: true, duration, current: v.currentTime, rate: v.playbackRate};
}
"""


def parse_duration_seconds(text: str) -> int:
    """'00:23:55' -> 1435"""
    try:
        parts = [int(x) for x in (text or "").strip().split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return parts[-3] * 3600 + parts[-2] * 60 + parts[-1]
    except Exception:
        return 0


def flat_topics(course: dict) -> list[dict]:
    out: list[dict] = []
    for main in course.get("main_topics") or []:
        for sub in main.get("sub_topics") or []:
            if sub.get("id"):
                out.append(sub)
    return out


def topic_rows_from_payload(rows: list[dict]) -> dict[str, dict]:
    """แปลงข้อมูลแถวจาก COURSE_TOPIC_ROWS_JS -> {tid: progress/unlocked}"""
    out: dict[str, dict] = {}
    for r in rows or []:
        tid = str(r.get("tid") or "")
        if not tid:
            continue
        cls = r.get("cls") or ""
        if "progress" in r:
            # หน้าเว็บรุ่นใหม่: JS อ่าน class done / title % ให้แล้ว
            progress = int(r.get("progress") or 0)
            if "done" in cls.split():
                progress = 100
        else:
            # ข้อมูลรุ่นเก่า: อ่านจาก progress bar width ใน HTML
            widths = [
                int(float(x))
                for x in re.findall(r"width\s*:\s*(\d+(?:\.\d+)?)%", r.get("html") or "")
            ]
            progress = max(widths) if widths else 0
        item = {
            "tid": tid,
            "title": (r.get("title") or "").strip(),
            "href": r.get("href") or "",
            # ไม่เห็น has-access แต่มีลิงก์/สถานะ done = เปิดเรียนได้แล้ว
            "unlocked": (
                "has-access" in cls or "done" in cls.split() or bool(r.get("href"))
            ),
            "progress": progress,
        }
        old = out.get(tid)
        if old is None:
            out[tid] = item
            continue
        # รวมข้อมูลจากแถวซ้ำ: เปิดได้ถ้ามีอันไหนเปิดได้, ใช้ progress สูงสุด
        old["unlocked"] = old["unlocked"] or item["unlocked"]
        old["progress"] = max(old["progress"], item["progress"])
        if not old["title"] and item["title"]:
            old["title"] = item["title"]
        if not old["href"] and item["href"]:
            old["href"] = item["href"]
    return out


def parse_course_topic_rows(page: Page, eid: int) -> dict[str, dict]:
    """อ่านแถวบทเรียนจากหน้าคอร์ส: progress=100 คือผ่านแล้ว, unlocked=has-access"""
    url = f"{BASE}/{GROUP}/enroll/{eid}/course"
    try:
        if not settle(page, url):
            return {}
        page.wait_for_timeout(1200)
        rows = page.evaluate(COURSE_TOPIC_ROWS_JS) or []
    except Exception:
        return {}
    return topic_rows_from_payload(rows)


def stage_topic(page: Page, eid: int, tid: str) -> bool:
    """เลื่อนตำแหน่งเรียนไปบทถัดไป (ทำให้บทถัดไปถูกปลดล็อก)"""
    try:
        res = page.evaluate(STAGE_TOPIC_JS, {"eid": eid, "tid": str(tid)})
    except Exception as e:
        print(f"      ⚠️ เรียก stage ไม่สำเร็จ: {type(e).__name__}")
        return False
    st = (res or {}).get("status")
    if st and int(st) < 400:
        return True
    print(f"      ⚠️ stage ตอบ HTTP {st}")
    return False


def is_topic_page(page: Page, tid: str) -> bool:
    """อยู่หน้าบทเรียนจริง ไม่ใช่หน้าคอร์สที่เด้งกลับมา"""
    try:
        u = page.url or ""
        if f"/topics/{tid}" not in u:
            return False
        title = page.title() or ""
        if "รายละเอียดหลักสูตร" in title:
            return False
        return True
    except Exception:
        return False


def open_topic(page: Page, eid: int, tid: str) -> bool:
    """เปิดบทเรียน; ถ้าเด้งกลับหน้าคอร์ส ให้กดลิงก์เข้าเรียนจากหน้าคอร์ส"""
    url = f"{BASE}/{GROUP}/enroll/{eid}/topics/{tid}"
    for attempt in range(2):
        if settle(page, url):
            page.wait_for_timeout(1400)
            if is_topic_page(page, tid):
                return True
            if "member.set.or.th" in (page.url or ""):
                return False

        # เด้งกลับ -> เปิดหน้าคอร์สแล้วกด "เข้าเรียน" ของบทนี้
        try:
            settle(page, f"{BASE}/{GROUP}/enroll/{eid}/course")
            page.wait_for_timeout(900)
            link = page.locator(f'a[href*="/topics/{tid}"]')
            if link.count() > 0:
                link.first.click(timeout=8000)
                page.wait_for_timeout(2600)
                if is_topic_page(page, tid):
                    return True
        except Exception:
            pass

        # เผื่อรอบแรกโดน redirect ชั่วคราว
        page.wait_for_timeout(800)

    return is_topic_page(page, tid)


def _topic_nav_action(page: Page, tid: str) -> str:
    """ดูว่าเว็บพาไปหน้าไหนหลังบันทึกผลบทเรียน"""
    u = page.url or ""
    if "/exam/" in u:
        return "quiz"
    if "/summary" in u:
        return "done"
    if "/topics/" in u and f"/topics/{tid}" not in u:
        return "next"
    if "/course" in u and "/topics/" not in u:
        return "next"
    return ""


def _topic_status_action(page: Page, eid: int, data: dict) -> str:
    """แปลง chk จาก API เป็นการกระทำของบอท"""
    chk = data.get("chk") if isinstance(data, dict) else None
    next_id = ""
    try:
        next_id = str((data.get("topics") or {}).get("next", {}).get("id") or "")
    except Exception:
        next_id = ""

    # player ของเว็บจะพาไปทำ quiz ทันทีถ้า auto_quiz
    if isinstance(data.get("message"), dict) and data["message"].get("auto_quiz"):
        return "quiz"

    if chk in ("next", "success_next", "success", "survey"):
        if next_id:
            stage_topic(page, eid, next_id)
        return "next"
    if chk in ("quiz", "examination", "post-test"):
        return "quiz"
    if chk == "return":
        return "next"
    return ""


def watch_and_complete_topic(page: Page, eid: int, topic: dict, e2t: str) -> str:
    """เล่นวิดีโอไปท้ายบท -> รอ dialog ของเว็บ -> กดยืนยัน -> stage บทถัดไป"""
    title = plain_text(topic.get("title", ""))[:52]
    seconds = parse_duration_seconds(topic.get("duration"))
    print(f"      ▶️ [{title}] เปิดวิดีโอและเลื่อนไปท้ายบท ...")
    try:
        info = page.evaluate(TOPIC_WATCH_JS) or {}
    except Exception as e:
        info = {"ok": False, "reason": type(e).__name__}

    if info.get("ok"):
        dur = float(info.get("duration") or 0)
        if seconds <= 0 and dur > 0:
            seconds = int(dur)
        print(f"      🎬 วิดีโอพร้อม ({dur:.0f}s, เร่ง {info.get('rate')}x)")
    else:
        print(f"      ℹ️ เล่นวิดีโอไม่ได้ ({info.get('reason')}) — ใช้ API ทำเครื่องหมายเรียนจบ")

    # ปล่อยให้ player ของเว็บยิง enroll2topic_status แล้วขึ้น dialog ยืนยัน
    dialog = False
    for _ in range(30):  # ~12 วินาที
        try:
            if page.locator(
                ".jconfirm .btn-confirm:visible, .jconfirm-buttons .btn-confirm:visible, "
                ".jconfirm button.btn-confirm:visible"
            ).count() > 0:
                dialog = True
                break
            nav = _topic_nav_action(page, str(topic.get("id") or ""))
            if nav:
                return nav
        except Exception:
            pass
        page.wait_for_timeout(400)

    if dialog:
        try:
            dtitle = page.locator(".jconfirm .jconfirm-title").first.inner_text(timeout=1000).strip()
            btn = page.locator(".jconfirm .btn-confirm").first.inner_text(timeout=1000).strip()
            if dtitle or btn:
                print(f"      💬 {dtitle[:60]} → กด '{btn[:30]}'")
        except Exception:
            pass
        try:
            page.locator(".jconfirm .btn-confirm").first.click(timeout=5000)
        except Exception as e:
            print(f"      ⚠️ กดปุ่มยืนยันไม่สำเร็จ: {type(e).__name__}")

        for _ in range(30):  # ~12 วินาที รอเว็บพาไปหน้าถัดไป
            nav = _topic_nav_action(page, str(topic.get("id") or ""))
            if nav:
                return nav
            page.wait_for_timeout(400)

    # สำรอง: เรียก API เดียวกับที่ player เรียก แล้วจัดการ chk/stage เอง
    if not e2t:
        try:
            e2t = find_enroll2topic_id(page.content())
        except Exception:
            e2t = None
    if not e2t:
        return "failed"

    if seconds <= 0:
        try:
            seconds = int(
                page.evaluate(
                    "() => { const v = document.querySelector('video'); "
                    "return v && isFinite(v.duration) ? v.duration : 0; }"
                )
                or 0
            )
        except Exception:
            seconds = 0

    try:
        res = page.evaluate(TOPIC_COMPLETE_JS, {"id": e2t, "duration": seconds})
    except Exception as e:
        print(f"      ⚠️ เรียก API ทำเครื่องหมายไม่สำเร็จ: {type(e).__name__}")
        return "failed"

    st = (res or {}).get("status") or {}
    data = st.get("json") or {}
    chk = data.get("chk") if isinstance(data, dict) else None
    print(f"      🔁 API: HTTP {st.get('status')} / {chk or '-'}")
    action = _topic_status_action(page, eid, data)
    if action:
        return action
    if st.get("status") == 200:
        return "next"
    return "failed"


def learn_one_topic(page: Page, eid: int, topic: dict, args=None, attempts: int = 3) -> str:
    """เรียนบทเดียว -> 'done' (100%), 'quiz' (เว็บปลดล็อกข้อสอบ), 'failed'"""
    tid = str(topic["id"])
    title = plain_text(topic.get("title", ""))[:52]

    rows = parse_course_topic_rows(page, eid)
    row = rows.get(tid)
    if row and row.get("progress", 0) >= 100:
        print(f"   ⏭️ {title} — ผ่านแล้ว (ข้าม)")
        return "done"

    if not row or not row.get("unlocked"):
        print(f"   🔓 ปลดล็อก {title} ...")
        stage_topic(page, eid, tid)
        page.wait_for_timeout(1200)
        rows = parse_course_topic_rows(page, eid)
        row = rows.get(tid)

    for attempt in range(1, attempts + 1):
        if not open_topic(page, eid, tid):
            print(
                f"      ↪️ เปิดบทเรียนไม่ได้ (ครั้งที่ {attempt}/{attempts}) "
                "— ลองกดเข้าเรียนจากหน้าคอร์ส/ปลดล็อกอีกครั้ง"
            )
            stage_topic(page, eid, tid)
            page.wait_for_timeout(1200)
            continue

        # รอ player โหลด แล้วหา enroll2topic id
        e2t = None
        for _ in range(3):
            page.wait_for_timeout(1500)
            try:
                e2t = find_enroll2topic_id(page.content())
            except Exception:
                e2t = None
            if e2t:
                break
        if not e2t:
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2000)
                e2t = find_enroll2topic_id(page.content())
            except Exception:
                pass
        if not e2t:
            print(f"      ❌ ยังหา enroll2topic id ไม่เจอ (url: {page.url})")
            if args is not None and getattr(args, "dump", False):
                dump_page(page, f"topic_{tid}_no_id")
            stage_topic(page, eid, tid)
            page.wait_for_timeout(1200)
            continue

        action = watch_and_complete_topic(page, eid, topic, e2t)
        if action == "quiz":
            return "quiz"
        if action == "done":
            return "done"

        # ตรวจจากหน้าคอร์สว่าบทนี้ขึ้น 100% หรือยัง
        page.wait_for_timeout(800)
        rows = parse_course_topic_rows(page, eid)
        row = rows.get(tid)
        if row and row.get("progress", 0) >= 100:
            print(f"   ✅ {title} — 100%")
            return "done"

        print(
            f"      🔄 ยังไม่ผ่าน "
            f"(progress={row.get('progress') if row else '?'}) — ลองใหม่"
        )
        if attempt < attempts:
            stage_topic(page, eid, tid)
            page.wait_for_timeout(1000)

    return "failed"


def complete_topics(page: Page, course: dict, eid: int, args=None) -> bool:
    """เรียนทีละบทจนครบ: ข้ามบทที่ 100% แล้ว, บทที่ยังไม่ผ่านเล่นวิดีโอ/ทำซ้ำจนผ่าน"""
    topics = flat_topics(course)
    if not topics:
        return True

    total = len(topics)
    print(f"\n🎬 เรียนให้จบทีละบท {total} บท (ข้ามบทที่ผ่านแล้ว)")
    quiz_unlocked = False

    # ถ้าผ่านครบแล้ว ไม่ต้องเปิดบทเรียนซ้ำเลย (ประหยัดเวลา)
    rows = parse_course_topic_rows(page, eid)
    passed = [
        str(t["id"]) for t in topics
        if rows.get(str(t["id"]), {}).get("progress", 0) >= 100
    ]
    if len(passed) >= total:
        print(f"   ✅ บทเรียนผ่านครบ {total}/{total} แล้ว — ไม่ต้องเรียนซ้ำ")
        return True
    if passed:
        print(f"   ⏭️ ผ่านแล้ว {len(passed)}/{total} บท — จะทำเฉพาะบทที่ยังไม่ผ่าน")

    # รอบแรก: ไล่ทีละบท
    for i, topic in enumerate(topics, 1):
        print(f"\n   [{i}/{total}] {plain_text(topic.get('title', ''))[:52]}")
        action = learn_one_topic(page, eid, topic, args, attempts=3)
        if action == "quiz":
            quiz_unlocked = True
            print("   🔓 แบบทดสอบถูกปลดล็อกแล้ว — จะเรียนบทที่เหลือให้ครบก่อน")
        if i < total:
            stage_topic(page, eid, str(topics[i]["id"]))
            page.wait_for_timeout(500)
        if action == "failed":
            print("   ⚠️ บทนี้ยังไม่ผ่าน — จะลองซ้ำในรอบสอง")

    # รอบสอง: เฉพาะบทที่ยังไม่ 100% (ทำจนกว่าจะผ่าน)
    rows = parse_course_topic_rows(page, eid)
    missing = [
        t for t in topics
        if rows.get(str(t["id"]), {}).get("progress", 0) < 100
    ]
    if missing:
        print(f"\n🔁 ตรวจพบ {len(missing)} บทที่ยังไม่ 100% — ลองซ้ำอีกรอบ")
        for topic in missing:
            action = learn_one_topic(page, eid, topic, args, attempts=2)
            if action == "quiz":
                quiz_unlocked = True

    # ตรวจรอบสุดท้าย
    rows = parse_course_topic_rows(page, eid)
    all_tids = [str(t["id"]) for t in topics]
    verified = [tid for tid in all_tids if rows.get(tid, {}).get("progress", 0) >= 100]
    print(f"\n   📊 ตรวจสอบ: ผ่านแล้ว {len(verified)}/{total} บท")
    if len(verified) >= total:
        return True
    if quiz_unlocked:
        print("   ℹ️ บางบทอาจไม่ขึ้น 100% แต่เว็บปลดล็อกแบบทดสอบแล้ว — ทำข้อสอบต่อได้")
        return True
    return False


def quiz_landing_url(eid: int, kind: str, slug: str) -> str:
    return f"{BASE}/{GROUP}/enroll/{eid}/exam/{slug}"


def find_quiz_page(page: Page, eid: int, quiz: dict) -> Optional[str]:
    """เปิดหน้าเริ่มสอบของชุดนี้ -> คืน URL เมื่อเจอ quizID ตรงกับข้อมูลคอร์ส"""
    qid = str(quiz["id"])
    for slug in QUIZ_PATHS.get(quiz["kind"], (quiz["kind"],)):
        url = quiz_landing_url(eid, quiz["kind"], slug)
        if not settle(page, url):
            if "member.set.or.th" in (page.url or ""):
                return None
            continue
        page.wait_for_timeout(800)
        if "member.set.or.th" in (page.url or ""):
            return None
        try:
            page_qid = page.evaluate(
                "() => (typeof quizID !== 'undefined') ? String(quizID) : null"
            )
        except Exception:
            page_qid = None
        if page_qid == qid:
            return url
    return None


def build_exam_plan(form: dict, bank: list[dict], survey: bool = False) -> tuple[list[dict], list]:
    """จับคู่ข้อสอบบนหน้าจอกับเฉลย -> (แผนตอบ, ข้อที่จับคู่ไม่ได้)"""
    plan: list[dict] = []
    missing: list = []
    for q in form.get("questions") or []:
        opts = q.get("options") or []
        if not opts:
            missing.append((q, "ไม่พบตัวเลือก"))
            continue

        # 1) หาข้อในเฉลยจาก question id ก่อน (id ตรงกันเสมอใน SET)
        item = None
        for b in bank:
            if str(b.get("id")) == str(q.get("qid")):
                item = b
                break
        # 2) ไม่เจอ -> เทียบข้อความ
        if item is None:
            item = find_bank_question(q.get("text", ""), [o["text"] for o in opts], bank)
        if item is None and not survey:
            missing.append((q, "ไม่พบข้อนี้ในเฉลย"))
            continue

        aid = None
        if not survey and item is not None:
            # 2.1) ใช้ id ตัวเลือกจาก API (แม่นสุด รองรับการสลับตัวเลือก)
            correct_ids = {
                str(o.get("id")) for o in item.get("options", []) if o.get("correct") == 1
            }
            for o in opts:
                if str(o["id"]) in correct_ids:
                    aid = str(o["id"])
                    break
            # 2.2) เทียบข้อความ
            if aid is None:
                idx = correct_option_index(item, [o["text"] for o in opts])
                if idx is not None:
                    aid = str(opts[idx]["id"])
        if survey and aid is None:
            aid = str(opts[0]["id"])

        if aid is None:
            missing.append((q, "จับคู่ตัวเลือกที่ถูกไม่ได้"))
            continue
        if not any(str(o["id"]) == aid for o in opts):
            missing.append((q, "ตัวเลือกที่จับคู่ไม่มีในหน้า"))
            continue

        plan.append({
            "qid": q["qid"],
            "aid": aid,
            "qtext": (q.get("text") or "").replace("\n", " ")[:78],
            "atext": next((o["text"] for o in opts if str(o["id"]) == aid), "")[:78],
        })
    return plan, missing


def start_exam(page: Page) -> Optional[str]:
    """กดปุ่มเริ่มสอบ -> คืนเลขที่สอบ (enroll2quiz) หรือ None ถ้าเริ่มไม่สำเร็จ"""
    btn = page.locator("#btnExam")
    if btn.count() == 0:
        print("   ⚠️ ไม่พบปุ่ม 'เริ่มแบบทดสอบ'")
        return None

    data = {}
    try:
        with page.expect_response(
            lambda r: "/api/site/enroll2quiz" in r.url, timeout=45000
        ) as ri:
            btn.first.click(timeout=20000)
        try:
            data = ri.value.json()
        except Exception:
            data = {}
    except Exception as e:
        print(f"   ⚠️ กดเริ่มสอบแล้วไม่เห็นคำตอบจาก API: {type(e).__name__}")
        data = {}

    if data.get("is_error"):
        print(f"   ❌ เริ่มสอบไม่สำเร็จ: {data.get('message') or data}")
        return None

    try:
        page.wait_for_selector(".testing-block:not(.d-none)", timeout=25000)
    except Exception:
        print("   ⚠️ ไม่เห็นฟอร์มข้อสอบหลังกดเริ่ม (อาจมีป๊อปอัป/เงื่อนไข)")
        return None
    page.wait_for_timeout(800)

    attempt = str(data.get("enroll2quiz") or "")
    if not attempt:
        try:
            attempt = page.eval_on_selector("#enroll2quiz", "el => el.value") or ""
        except Exception:
            attempt = ""
    return attempt or "?"


def confirm_submit_modal(page: Page) -> Optional[str]:
    """กดยืนยันในกล่อง jquery-confirm (จำกัดเฉพาะใน .jconfirm)"""
    try:
        page.wait_for_selector(".jconfirm", timeout=12000)
    except Exception:
        pass
    for label in ("ยืนยันการส่งคำตอบ", "ยืนยัน", "ตกลง", "OK"):
        try:
            loc = page.locator(f".jconfirm button:has-text('{label}')")
            if loc.count() == 0:
                continue
            loc.first.click(timeout=8000)
            return label
        except Exception:
            continue
    return None


def submit_exam(page: Page) -> bool:
    """ไปข้อสุดท้าย -> กดส่งคำตอบ -> ยืนยัน -> รอหน้า /score"""
    try:
        page.evaluate(
            """() => {
                const fs = [...document.querySelectorAll('#exam-form fieldset')];
                if (!fs.length) return;
                const last = fs[fs.length - 1];
                try { fieldset(last.getAttribute('data-id')); } catch (e) { last.style.display = 'block'; }
                try { checkSubmitButton(); } catch (e) {}
            }"""
        )
        page.wait_for_timeout(900)
    except Exception:
        pass

    btn = page.locator(".submit-an-answer:visible")
    if btn.count() == 0:
        btn = page.locator(".submit-an-answer")
    if btn.count() == 0:
        print("   ❌ ไม่พบปุ่มส่งคำตอบ")
        return False

    try:
        btn.first.click(timeout=15000)
    except Exception as e:
        print(f"   ❌ กดส่งคำตอบไม่สำเร็จ: {type(e).__name__}")
        return False

    clicked = confirm_submit_modal(page)
    if clicked:
        print(f"   📤 ยืนยันส่งคำตอบ ('{clicked}')")
    else:
        print("   ⚠️ ไม่พบปุ่มยืนยันในกล่องส่งคำตอบ")

    try:
        page.wait_for_url(re.compile(r"/exam/\d+/score"), timeout=45000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    return "/score" in (page.url or "")


def solve_quiz(page: Page, eid: int, quiz: dict, args, exam_links: Optional[dict] = None) -> bool:
    qid = quiz["id"]
    print(f"\n📝 {quiz['title']} (quiz id={qid}, {len(quiz['questions'])} ข้อ)")

    url = find_quiz_page(page, eid, quiz)
    if not url:
        print("   ❌ เข้าหน้าแบบทดสอบชุดนี้ไม่ได้")
        print("      (อาจยังไม่ล็อกอิน หรือยังไม่ปลดล็อกเพราะต้องทำข้อก่อนหน้าให้เสร็จ)")
        if args.dump:
            dump_page(page, f"quiz_{qid}_notfound")
        return False
    print(f"   🔗 {page.url}")

    if args.dump:
        dump_page(page, f"quiz_{qid}_landing")

    # ตรวจประวัติการสอบก่อน: ผ่านแล้ว = ข้ามทันที (ไม่เสียเวลาทำซ้ำ)
    # และต้องไม่เริ่มใหม่ถ้าเคยสอบแต่ยังไม่ผ่านโดยไม่มีสิทธิ์สอบเหลือ
    history = read_exam_history(page)
    if exam_history_passed(history):
        print(f"   ✅ ชุดนี้ผ่านแล้ว ({exam_history_summary(history)}) — ข้าม ไม่ทำซ้ำ")
        return True
    if history:
        print(f"   📜 เคยสอบแล้วแต่ยังไม่ผ่าน ({exam_history_summary(history)})")

    if page.locator("#btnExam").count() == 0:
        if history:
            print("   ⚠️ ไม่พบปุ่มเริ่มสอบ — อาจใช้สิทธิ์สอบครบแล้ว")
        else:
            print("   ⚠️ ไม่พบปุ่มเริ่มสอบ (อาจยังไม่ปลดล็อก หรือสิทธิ์การสอบครบแล้ว)")
        return False

    form = page.evaluate(EXAM_FORM_JS)
    if not form or not form.get("questions"):
        print("   ⚠️ ไม่พบฟอร์มข้อสอบในหน้านี้ (ไม่เริ่มทำเพื่อกันเสียสิทธิ์)")
        if args.dump:
            dump_page(page, f"quiz_{qid}_noform")
        return False

    is_survey = quiz["kind"] == "survey"
    print(f"   📄 พบข้อสอบ {len(form['questions'])} ข้อ")
    plan, missing = build_exam_plan(form, quiz["questions"], survey=is_survey)
    print(f"   🧩 จับคู่เฉลยได้ {len(plan)}/{len(form['questions'])} ข้อ")

    expected = int(form.get("all") or len(form["questions"]))
    if missing or len(plan) != expected:
        for q, why in missing:
            print(f"      ❌ {why}: {(q.get('text') or '')[:70]}")
        print("   ⛔ ยังไม่เริ่มทำ เพราะจับคู่เฉลยไม่ครบ (กันเสียสิทธิ์สอบที่มีจำนวนครั้งจำกัด)")
        return False

    for p in plan:
        print(f"      • {p['qtext']} -> {p['atext']}")

    # เริ่มทำข้อสอบ (เริ่มตอนนี้เท่านั้น เพื่อไม่ให้เสียเวลาสอบ)
    print("   ▶️ เริ่มทำแบบทดสอบ ...")
    attempt = start_exam(page)
    if not attempt:
        if args.dump:
            dump_page(page, f"quiz_{qid}_start_failed")
        return False
    print(f"   🔢 เลขที่สอบ: {attempt}")

    # ตอบทุกข้อ (ฟอร์มซ่อนอยู่ แต่คลิกด้วย JS ได้)
    results = page.evaluate(APPLY_PLAN_JS, plan)
    answered = sum(1 for r in results if r.get("ok"))
    failed = [r for r in results if not r.get("ok")]
    print(f"   ✅ ตอบแล้ว {answered}/{len(plan)} ข้อ")
    if failed:
        for r in failed[:5]:
            print(f"      ⚠️ ข้อ {r['qid']}: {r.get('why')}")

    # ตรวจซ้ำว่าทุกข้อถูกเลือกจริง
    verify = page.evaluate(VERIFY_ANSWERS_JS)
    not_ready = [v for v in verify if v.get("checked", 0) < 1]
    if not_ready:
        page.evaluate(APPLY_PLAN_JS, plan)
        page.wait_for_timeout(500)
        verify = page.evaluate(VERIFY_ANSWERS_JS)
        not_ready = [v for v in verify if v.get("checked", 0) < 1]
    if not_ready:
        print(f"   ❌ ยังมีข้อที่ไม่ได้เลือก {len(not_ready)} ข้อ — หยุดก่อนส่ง")
        if args.dump:
            dump_page(page, f"quiz_{qid}_incomplete")
        return False

    if args.dump:
        dump_page(page, f"quiz_{qid}_answered")

    ok = submit_exam(page)
    if ok:
        result = read_result(page)
        print(f"   🎉 ส่งคำตอบสำเร็จ — {page.url}")
        if result:
            print(f"   📊 ผลลัพธ์: {result}")
        if args.dump:
            dump_page(page, f"quiz_{qid}_score")
        return True

    print(f"   ❌ ส่งคำตอบไม่สำเร็จ (อยู่ที่ {page.url})")
    if args.dump:
        dump_page(page, f"quiz_{qid}_submit_failed")
    return False


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

    course = fetch_course_json_full(ctx, cid)
    if not course:
        print("❌ ดึงข้อมูลคอร์สไม่ได้ — ตรวจอินเทอร์เน็ตแล้วลองใหม่")
        return False

    print(f"   ชื่อคอร์ส: {plain_text(course.get('title', ''))}")
    all_quizzes = build_quiz_bank(course)
    plan = course_plan(course)
    describe_course_plan(course, plan, all_quizzes)

    quizzes = list(all_quizzes)
    if args.only:
        wanted = {x.strip().lower() for x in args.only.split(",") if x.strip()}
        quizzes = [q for q in quizzes if q["kind"] in wanted]
        if not quizzes:
            print(f"⚠️ ไม่พบชุดที่ตรงกับ --only {args.only} — จะเรียนบทเรียนอย่างเดียว")
    elif not args.survey:
        # ค่าเริ่มต้น: ทำเฉพาะแบบทดสอบ ไม่ทำแบบสอบถามความพึงพอใจ
        quizzes = [q for q in quizzes if q["kind"] != "survey"]

    if quizzes:
        print("\n   แบบทดสอบที่จะทำ:")
        for q in quizzes:
            n_correct = sum(1 for x in q["questions"] for o in x["options"] if o["correct"])
            print(
                f"     • [{q['kind']}] {q['title']} — "
                f"{len(q['questions'])} ข้อ, เฉลย {n_correct} ตัวเลือก"
            )

    eid = args.eid or enroll_course(page, ctx, cid)
    if not eid:
        print("   ℹ️ ถ้ารู้ enroll id แล้ว สามารถรันซ้ำด้วย --eid <ตัวเลข> ได้")
        return False

    # เรียนบทเรียนให้ครบก่อน เพื่อปลดล็อก post-test/exam
    topics_ok = True
    if args.no_topics:
        print("\n⏭️ ข้ามการเรียนบทเรียน (--no-topics)")
    else:
        topics_ok = complete_topics(page, course, eid, args)
        if not topics_ok:
            print("   ⚠️ บทเรียนยังไม่ครบ 100% — จะลองทำแบบทดสอบต่อ (ถ้าปลดล็อกแล้ว)")

    # คอร์สเรียนล้วน (หรือ --only ไม่ตรงกับชุดไหน) = เรียนจบแล้วถือว่าเสร็จ
    if not quizzes:
        print("\nℹ️ คอร์สนี้มีแต่บทเรียน (ไม่มีแบบทดสอบที่ต้องทำ) — เรียนจบแล้ว")
        return topics_ok

    # ต้องทำตามลำดับ pre -> post -> exam
    quizzes.sort(key=lambda q: QUIZ_ORDER.get(q["kind"], 9))

    ok = 0
    for quiz in quizzes:
        try:
            if solve_quiz(page, eid, quiz, args):
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
    ap.add_argument("--no-pause", action="store_true", help="ไม่ต้องรอ Enter เมื่อจบ (สำหรับรันอัตโนมัติ)")
    ap.add_argument("--no-topics", action="store_true", help="ข้ามการเรียนบทเรียน (ใช้เมื่อปลดล็อกข้อสอบแล้ว)")
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
                course = fetch_course_json_full(ctx, cid) if cid else None
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
        restore_session(ctx)
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

        if not args.no_pause:
            try:
                input("👉 กด [Enter] เพื่อปิดเบราว์เซอร์: ")
            except EOFError:
                pass
        save_session(ctx)
        ctx.close()


if __name__ == "__main__":
    main()
