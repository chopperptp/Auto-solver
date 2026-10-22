"""
thaimooc_bot.py — ไล่เรียน + ทำแบบทดสอบ Thai MOOC Academy (learn.thaimooc.ac.th) แบบหลายคอร์ส

ระบบของ Thai MOOC
------------------
• portal            : https://thaimooc.ac.th/courses/TAT00986
• ห้องเรียน (edX)    : https://learn.thaimooc.ac.th/courses/course-v1:TAT+00986+0/
• SSO (Keycloak)    : https://sso.thaimooc.ac.th  (ล็อกอินด้วย ThaiD / LINE / อีเมล)

ขั้นตอนการใช้
-------------
1) ใส่ลิงก์คอร์สในไฟล์ `thaimooc_links.txt` (หนึ่งบรรทัดต่อ 1 คอร์ส)
   ใส่ได้ทั้งลิงก์ portal (thaimooc.ac.th/courses/XXXX) และลิงก์ห้องเรียน (learn.thaimooc.ac.th/...)
2) รัน:
       python thaimooc_bot.py
   ระบบจะเปิดเบราว์เซอร์ → กดปุ่ม ThaiD ให้อัตโนมัติ → คุณยืนยันตัวตนในแอป/หน้าเว็บ
   → กด [Enter] → เริ่มไล่เรียนและทำแบบทดสอบ
   (session ถูกจดจำไว้ในโฟลเดอร์ browser_profile ครั้งเดียวก็พอ)

ตัวเลือกอื่น
-------------
    python thaimooc_bot.py --list                 # ดูรายการลิงก์ที่จะทำ แล้วออก
    python thaimooc_bot.py --learn                # เรียนให้จบก่อน แล้วค่อยทำแบบทดสอบ
    python thaimooc_bot.py --skip-done            # ข้ามหน่วยที่ทำเสร็จแล้ว
    python thaimooc_bot.py --no-retry-wrong       # ตอบผิดแล้วไม่ต้องลองตัวเลือกอื่น
    python thaimooc_bot.py --no-quiz              # เรียน/เปิดหน่วยอย่างเดียว ไม่ทำแบบทดสอบ
    python thaimooc_bot.py --dry-run              # ทดสอบว่าดึงข้อมูลได้ ไม่คลิกอะไร
    python thaimooc_bot.py --exam-password 1234   # รหัสผ่านของแบบสอบ
    python thaimooc_bot.py --links-file my.txt    # อ่านลิงก์จากไฟล์อื่น

ค่าเริ่มต้น (ไม่ใส่ option)
---------------------------
    ทำเฉพาะ "แบบฝึกหัด / แบบทดสอบ" ทุกหน่วยในคอร์สให้ครบ (ไม่เรียน)
    เช่น แบบทดสอบก่อนเรียน, แบบฝึกหัดบทที่ 1-4, Final Exam
"""

import argparse
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from playwright.sync_api import Page, sync_playwright

import quiz_solver as bot  # ใช้ฟังก์ชันกลางของ quiz_solver.py (เรียน, ตอบข้อสอบ, เรียก AI)

HERE = Path(__file__).parent
LINKS_FILE = HERE / "thaimooc_links.txt"
PROFILE_DIR = HERE / "browser_profile"

LMS_BASE = bot.THAIMOOC_LMS          # https://learn.thaimooc.ac.th
PORTAL_BASE = bot.THAIMOOC_PORTAL    # https://thaimooc.ac.th
LOGIN_URL = bot.THAIMOOC_LOGIN       # https://learn.thaimooc.ac.th/login (เด้งไป SSO เอง)

# ============================================================================
# ใส่ลิงก์คอร์สที่ต้องการทำตรงนี้ได้เลย (ไม่ต้องแก้ไฟล์อื่น)
# ใส่ได้ทั้ง: https://thaimooc.ac.th/courses/TAT00986
#            https://learn.thaimooc.ac.th/courses/course-v1:TAT+00986+0/
#            course-v1:TAT+00986+0   หรือ   TAT00986
# ============================================================================
COURSE_LINKS: list[str] = [
    # "https://thaimooc.ac.th/courses/TAT00986",
]


@dataclass
class Task:
    """งาน 1 รายการ: ลิงก์ + วิธีจัดการ"""

    url: str
    mode: str = "course"   # "course" = เรียนให้จบ + ทำแบบทดสอบ | "quiz" = ทำแบบทดสอบอย่างเดียว


# ============================================================================
# 1. จัดการรายการลิงก์
# ============================================================================
def normalize_link(link: str) -> str:
    """แปลงรหัสคอร์ส/ลิงก์ยาว ให้เป็น URL ของห้องเรียน (Open edX) ที่ใช้ได้"""
    link = link.replace("\ufeff", "").strip()
    if not link:
        return link

    # 1) ใส่แค่รหัสคอร์ส เช่น course-v1:TAT+00986+0
    if link.startswith("course-v1:"):
        cid = re.split(r"[/?#]", link)[0]
        return f"{LMS_BASE}/courses/{cid}/"

    # 2) ใส่แค่รหัสคอร์สของ portal เช่น TAT00986
    cid = bot.portal_code_to_course_id(link)
    if cid:
        return f"{LMS_BASE}/courses/{cid}/"

    # 3) ลิงก์หน้า portal: https://thaimooc.ac.th/courses/TAT00986
    m = re.search(r"thaimooc\.ac\.th/courses/([A-Za-z]+\d{5})(?![A-Za-z0-9])", link)
    if m:
        cid = bot.portal_code_to_course_id(m.group(1))
        if cid:
            return f"{LMS_BASE}/courses/{cid}/"

    # 4) ลิงก์ห้องเรียน/หน่วยย่อย -> ตัดกลับไปที่หน้าคอร์ส
    m = re.match(r"^(https?://[^/]+/courses/course-v1:[^/?#]+)", link)
    if m:
        return m.group(1) + "/"

    m = re.match(r"^(https?://[^/]+/courses/[^/?#]+)", link)
    if m:
        return m.group(1) + "/"

    return link


def parse_link_line(line: str) -> Optional[Task]:
    """แปลง 1 บรรทัดจากไฟล์ลิงก์ -> Task (None = บรรทัดว่าง/คอมเมนต์)

    กติกา:
      • ขึ้นต้นด้วย #  = คอมเมนต์ (ข้าม)
      • ขึ้นต้นด้วย quiz: = ทำแบบทดสอบอย่างเดียว (ไม่เรียน)
      • อื่น ๆ = เรียนคอร์สให้จบ + ทำแบบทดสอบ
    """
    line = line.replace("\ufeff", "").strip()
    if not line or line.startswith("#"):
        return None

    mode = "course"
    low = line.lower()
    for prefix in ("quiz:", "q:"):
        if low.startswith(prefix):
            mode = "quiz"
            line = line[len(prefix):].strip()
            break

    if not line:
        return None

    # โหมดคอร์สตัดลิงก์ให้เป็นหน้าคอร์ส ส่วนโหมดแบบทดสอบใช้ URL ตามที่ใส่มาตรง ๆ
    url = normalize_link(line) if mode == "course" else line
    return Task(url=url, mode=mode)


def load_links(args) -> list[Task]:
    """รวบรวมงานจาก: ไฟล์ลิงก์ -> COURSE_LINKS -> argument ของคำสั่ง (ตัดลิงก์ซ้ำ)"""
    raw: list[str] = []

    link_file = Path(args.links_file) if args.links_file else LINKS_FILE
    if link_file.exists():
        # utf-8-sig = รองรับไฟล์ที่บันทึกแบบมี BOM (ค่าเริ่มต้นของ Notepad บน Windows)
        for line in link_file.read_text(encoding="utf-8-sig").splitlines():
            raw.append(line)
    elif args.links_file:
        print(f"⚠️ ไม่พบไฟล์ {link_file}")

    raw.extend(COURSE_LINKS)
    raw.extend(args.urls)

    # ตัดลิงก์ซ้ำ (เรียงตามลำดับเดิม)
    seen: set[str] = set()
    result: list[Task] = []
    for line in raw:
        task = parse_link_line(line)
        if task is None:
            continue
        key = f"{task.mode}|{task.url}"
        if key in seen:
            continue
        seen.add(key)
        result.append(task)

    return result


# ============================================================================
# 2. ล็อกอิน (ให้ผู้ใช้ล็อกอินเองในเบราว์เซอร์)
# ============================================================================
def is_logged_in(page: Page) -> bool:
    """ตรวจว่า session ปัจจุบันล็อกอินอยู่แล้วหรือยัง"""
    try:
        url = (page.url or "").lower()
        if any(k in url for k in ("/login", "openid-connect", "sso.thaimooc")):
            return False
        if "/dashboard" in url:
            return True
    except Exception:
        pass

    try:
        # Open edX: มีเมนูบัญชีผู้ใช้ / ลิงก์ account / logout
        if page.locator(
            "#navbar-account, .navbar-account, a[href*='/account/'], "
            "a[href*='/logout'], .navbar-right .dropdown-toggle, .user-menu"
        ).count() > 0:
            return True
    except Exception:
        pass

    return not bot.is_login_page(page)


def _check_session(ctx) -> bool:
    """เปิดแท็บใหม่เช็ค /dashboard (ไม่รบกวนแท็บที่ผู้ใช้กำลังล็อกอินอยู่)"""
    probe = None
    try:
        probe = ctx.new_page()
        probe.goto(f"{LMS_BASE}/dashboard", wait_until="domcontentloaded", timeout=45000)
        probe.wait_for_timeout(1500)
        return is_logged_in(probe) and not bot.is_login_page(probe)
    except Exception:
        return False
    finally:
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass


def wait_for_login(page: Page) -> None:
    """ล็อกอิน Thai MOOC: ถ้ายังไม่ล็อกอิน -> เด้งไป SSO -> กดปุ่ม ThaiD ให้ -> รอผู้ใช้ยืนยัน"""
    if _check_session(page.context):
        print("✅ ล็อกอินอยู่แล้ว (ใช้ session เดิมจาก browser_profile)\n")
        return

    print("🌐 กำลังเปิดหน้าเข้าสู่ระบบ Thai MOOC ...")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
    except Exception as e:
        print(f"⚠️ เปิดหน้าไม่สำเร็จ: {e}")
    time.sleep(2)

    # หน้า /login ของ edX จะเด้งไป SSO (Keycloak) เอง -> กดปุ่ม ThaiD ให้เลย
    if bot.is_login_page(page):
        print("🔐 พบหน้าเข้าสู่ระบบ — กำลังกดปุ่ม ThaiD ให้ ...")
        if bot.click_thaid_login(page):
            print("   ✅ เปิดหน้า ThaiD แล้ว (มี QR Code ให้สแกน)")
        else:
            print("   ℹ️ ไม่เจอปุ่ม ThaiD — เลือกวิธีล็อกอินเองในหน้าต่างเบราว์เซอร์")
            print("      (ThaiD / LINE / อีเมล-รหัสผ่าน)")

    print("=" * 62)
    print("🔐 กรุณายืนยันตัวตนด้วย ThaiD ให้เสร็จก่อน")
    print("   • สแกน QR Code ในหน้าต่างเบราว์เซอร์ด้วยแอป ThaiD")
    print("   • QR หมดอายุเร็ว (~1 นาที) — ถ้าหมดอายุให้กดปุ่มขอ QR ใหม่แล้วสแกนอีกครั้ง")
    print("   • เสร็จแล้วกลับมาที่ Terminal นี้แล้วกด [Enter]")
    print("   session จะถูกจดจำไว้ในโฟลเดอร์ browser_profile — ครั้งเดียวก็พอ")
    print("=" * 62)

    for attempt in range(1, 11):
        try:
            input("   กด [Enter] เมื่อล็อกอินเสร็จ: ")
        except EOFError:
            break

        if _check_session(page.context):
            print("✅ ล็อกอินเรียบร้อย เริ่มทำงานต่อได้เลย\n")
            return

        print(f"   ⚠️ ยังไม่เห็นว่าล็อกอิน (ครั้งที่ {attempt}/10) — ล็อกอินในเบราว์เซอร์แล้วกด Enter อีกครั้ง")

    raise SystemExit("❌ ล็อกอินไม่สำเร็จ — หยุดทำงาน (ล็อกอินในเบราว์เซอร์ให้เสร็จแล้วรันใหม่)")


# ============================================================================
# 3. ทำแบบทดสอบปลายคอร์ส (post-test) หลังเรียนจบ
# ============================================================================
def absolute_url(page: Page, url: str) -> str:
    if url.startswith("http"):
        return url
    base = re.match(r"(https?://[^/]+)", page.url)
    return (base.group(1) if base else LMS_BASE) + url


def goto(page: Page, url: str, tries: int = 3, timeout: int = 90000) -> bool:
    """เปิดหน้าเว็บแบบทนทาน (Thai MOOC โหลดช้าเป็นบางช่วง)"""
    for i in range(1, tries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return True
        except Exception as e:
            print(f"   ⚠️ เปิดหน้าไม่สำเร็จ (ครั้งที่ {i}/{tries}): {type(e).__name__}")
            time.sleep(3)
    return False


def do_final_quiz(page: Page, course_id: str, profile: dict, args) -> None:
    """ไล่ทำหน่วยแบบฝึกหัด/แบบทดสอบทั้งหมดของคอร์ส (ไม่ต้องเรียน)"""
    units = bot.fetch_course_units(page, course_id)
    targets = [
        u for u in units
        if any(t in ("problem", "exam") for t in (u.get("types") or []))
    ]

    if not targets:
        print("\nℹ️ ไม่พบหน่วยแบบทดสอบในคอร์สนี้")
        return

    skipped = [u for u in targets if getattr(args, "skip_done", False) and u.get("done")]
    if skipped:
        print(f"\n⏭️  ข้าม {len(skipped)} หน่วยที่ทำเสร็จแล้ว (--skip-done)")
        targets = [u for u in targets if u not in skipped]

    if not targets:
        print("\n✅ ทำครบทุกหน่วยแล้ว ไม่มีอะไรต้องทำเพิ่ม")
        return

    print(f"\n📝 พบหน่วยแบบทดสอบ {len(targets)} หน่วย — เริ่มทำตามลำดับ:")
    for i, u in enumerate(targets, 1):
        mark = "✅" if u.get("done") else "▫️"
        kinds = "/".join(u.get("types") or [])
        print(f"   {mark} {i}. [{kinds}] {u['name']}")

    results: list[tuple[str, str, int, str]] = []

    if args.dry_run:
        print("\n(dry-run: ดูรายการอย่างเดียว ไม่เปิดหน้าและไม่คลิกอะไร)")
        return

    # ---- ดึงเนื้อหาบทเรียนมาเป็นบริบทให้ AI (ทำให้ตอบตรงกับที่สอนจริง) ----
    chapter_blocks: dict[str, dict] = {}
    for u in targets:
        ch = (u.get("chapter") or "").strip()
        if ch and ch not in chapter_blocks:
            chapter_blocks[ch] = u.get("chapter_blocks") or {"videos": [], "html": []}

    texts: dict[str, str] = {}
    if chapter_blocks:
        try:
            texts = bot.fetch_chapter_texts(page, course_id, chapter_blocks)
        except Exception as e:
            print(f"⚠️ ใช้เนื้อหาบทเรียนไม่ได้: {str(e)[:120]}")

    all_text = "\n\n".join(t for t in texts.values() if t)

    for idx, unit in enumerate(targets, 1):
        url = absolute_url(page, unit["url"])
        kinds = "/".join(unit.get("types") or [])
        print("\n" + "-" * 52)
        print(f"📌 [{idx}/{len(targets)}] {unit['name']}  (type: {kinds})")

        # เลือกบริบท: บทเรียนปกติใช้เนื้อหาบทนั้น / pre-test & final ใช้ทุกบท
        chapter = (unit.get("chapter") or "").strip()
        if chapter.startswith("บทที่"):
            lesson_text = texts.get(chapter, "")
        else:
            lesson_text = all_text
        if lesson_text:
            print(f"   📚 ใช้เนื้อหาบทเรียน {len(lesson_text):,} ตัวอักษร เป็นบริบท")

        answered = 0
        status = "ไม่ทราบผล"
        per_problem = bool(profile.get("per_problem_submit"))

        try:
            if not goto(page, url):
                print("   ❌ เปิดหน้าไม่ได้ (ข้ามหน่วยนี้)")
                status = "เปิดหน้าไม่ได้"
                results.append((unit["name"], kinds, 0, status))
                continue

            if not bot.wait_for_problems(page, profile, timeout=25):
                print("   ℹ️ ไม่พบข้อสอบในหน้านี้ — บันทึกหน้าไว้ตรวจสอบ")
                bot.dump_debug_page(page, "debug_quiz", course_id=course_id)
                status = "ไม่พบข้อสอบ"
                results.append((unit["name"], kinds, 0, status))
                continue

            if "exam" in (unit.get("types") or []):
                bot.start_exam(page, args.exam_password)

            if per_problem:
                # Open edX: ตอบ + ส่งทีละข้อ (แต่ละข้อมีปุ่ม Submit ของตัวเอง)
                res = bot.solve_page_one_by_one(
                    page, profile, dry_run=False, context=lesson_text,
                    chapter=chapter, retry_wrong=not getattr(args, "no_retry_wrong", False),
                )
                answered = res["answered"]
                status = f"ตอบ {answered} ข้อ (ถูก {res['correct']}, ผิด {res['incorrect']})"
            else:
                qs = bot.extract_questions(page, profile)
                if qs:
                    answered += bot.solve_questions(page, qs, profile, dry_run=False)
                else:
                    print("   ✅ ไม่มีข้อที่ต้องทำในหน้านี้ (ตอบถูกครบแล้ว)")

                if bot.click_submit(page, profile):
                    status = "ส่งคำตอบแล้ว"
                else:
                    status = "ไม่มีปุ่มส่ง"

                # เว็บแบบทีละหน้า: กด Next ไปหน้าถัดไปเรื่อย ๆ
                pages = 1
                for _ in range(30):
                    if not bot.click_continue(page, profile):
                        break
                    pages += 1
                    bot.wait_for_problems(page, profile, timeout=10)
                    more = bot.extract_questions(page, profile)
                    if not more:
                        break
                    answered += bot.solve_questions(page, more, profile, dry_run=False)
                    bot.click_submit(page, profile)
                if pages > 1:
                    status += f" ({pages} หน้า)"

        except Exception as e:
            print(f"   ⚠️ เกิดข้อผิดพลาด: {e}")
            status = f"ผิดพลาด: {e}"

        score = ""
        try:
            st = page.evaluate(
                """() => {
                    const ps = [...document.querySelectorAll('.problem')];
                    const out = {total: ps.length, correct: 0, incorrect: 0};
                    ps.forEach(p => {
                        const s = p.querySelector('.status');
                        if (!s) return;
                        if (s.classList.contains('correct')) out.correct++;
                        else if (s.classList.contains('incorrect')) out.incorrect++;
                    });
                    return out;
                }"""
            )
            if st.get("total"):
                score = (f"ถูก {st['correct']}/{st['total']}"
                         + (f" (ผิด {st['incorrect']})" if st["incorrect"] else ""))
        except Exception:
            pass

        print(f"   👉 ผล: {status}" + (f" | {score}" if score else ""))
        results.append((unit["name"], kinds, answered, status + (f" | {score}" if score else "")))

    # ---- สรุปรวม ----
    if results:
        print("\n" + "=" * 62)
        print("📊 สรุปการทำแบบฝึกหัด/แบบทดสอบ")
        print("=" * 62)
        for name, btype, answered, status in results:
            print(f"   • {name} [{btype}] — {answered} ข้อ — {status}")
        print("=" * 62)


def show_progress(page: Page, course_id: str) -> None:
    """รายงานจำนวนหน่วยที่ทำเสร็จจาก blocks API (แม่นกว่าเดาจาก DOM)"""
    try:
        units = bot.fetch_course_units(page, course_id)
        if not units:
            return
        done = sum(1 for u in units if u.get("done"))
        quizzes = [u for u in units if any(t in ("problem", "exam") for t in (u.get("types") or []))]
        q_done = sum(1 for u in quizzes if u.get("done"))
        print(f"\n📊 ความคืบหน้า: เนื้อหาทั้งหมด {done}/{len(units)} หน่วย")
        print(f"   แบบฝึกหัด/แบบทดสอบที่เสร็จแล้ว: {q_done}/{len(quizzes)} หน่วย")
        for u in quizzes:
            print(f"   {'✅' if u.get('done') else '▫️'} {u.get('path')} :: {u['name']}")
    except Exception as e:
        print(f"   (อ่านความคืบหน้าไม่ได้: {e})")
    except Exception:
        pass


# ============================================================================
# 4. ทำ 1 คอร์ส
# ============================================================================
def process_course(page: Page, task: Task, profile: dict, args) -> bool:
    url = task.url

    print("\n" + "=" * 62)
    if args.learn and task.mode == "course":
        mode_label = "เรียนให้จบ + ทำแบบฝึกหัด/แบบทดสอบ"
    elif args.quiz:
        mode_label = "ทำแบบฝึกหัด/แบบทดสอบให้ครบ (ไม่เรียน)"
    else:
        mode_label = "เรียน/เปิดหน่วยอย่างเดียว"
    print(f"🎓 คอร์ส: {url}")
    print(f"   โหมด: {mode_label}")
    print("=" * 62)

    course_id = args.course_id or bot.course_id_from_url(url)

    if not goto(page, url):
        print("❌ เปิดหน้าไม่สำเร็จ (ลองใหม่แล้ว)")
        return False
    time.sleep(2.5)

    if bot.is_login_page(page):
        print("🔐 ยังไม่ได้ล็อกอิน — กำลังพาไปหน้าเข้าสู่ระบบ ...")
        wait_for_login(page)
        if not goto(page, url):
            print("❌ เปิดหน้าไม่สำเร็จ (ลองใหม่แล้ว)")
            return False
        time.sleep(2.5)

    # ลงทะเบียนคอร์สอัตโนมัติถ้ายังไม่ได้ enroll (ต้อง enroll ถึงจะเข้า courseware/exam ได้)
    bot.ensure_enrolled(page)

    course_id = course_id or bot.course_id_from_url(page.url)
    if not course_id:
        # ไม่ใช่ลิงก์ของ Open edX (เช่น e-Testing ของ thaimooc.ac.th) -> ลองทำแบบทดสอบจากหน้านี้เลย
        print("⚠️ ไม่พบ course_id — จะลองทำแบบทดสอบจากหน้านี้โดยตรง")
        try:
            qs = bot.extract_questions(page, profile)
            if qs:
                bot.solve_questions(page, qs, profile, dry_run=args.dry_run)
                bot.click_submit(page, profile)
            else:
                print("   ไม่พบข้อสอบในหน้านี้ — บันทึกหน้าไว้ตรวจสอบ")
                bot.dump_debug_page(page, "debug_direct")
        except Exception as e:
            print(f"   ⚠️ เกิดข้อผิดพลาด: {e}")
        return True

    print(f"🆔 course_id: {course_id}")

    if args.learn and task.mode == "course":
        bot.learn_course(
            page,
            course_id,
            profile,
            dry_run=args.dry_run,
            exam_password=args.exam_password,
        )
    else:
        print("⏭️  ข้ามการเรียน — ทำเฉพาะแบบฝึกหัด/แบบทดสอบ")

    if args.quiz:
        do_final_quiz(page, course_id, profile, args)

    show_progress(page, course_id)

    print(f"\n✅ เสร็จคอร์ส: {url}")
    return True


# ============================================================================
# 5. main
# ============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="ไล่เรียน + ทำแบบทดสอบ Thai MOOC (Open edX) แบบหลายบท",
    )
    parser.add_argument("urls", nargs="*", help="ลิงก์คอร์สที่ต้องการทำ (เพิ่มได้หลายลิงก์)")
    parser.add_argument("--links-file", default=None, help="ไฟล์ที่เก็บลิงก์ (default: thaimooc_links.txt)")
    parser.add_argument("--course-id", default=None, help="ระบุ course_id ถ้าลิงก์ไม่บอก")
    parser.add_argument("--learn", action="store_true",
                        help="เรียนให้จบก่อน (ดูวิดีโอ/อ่านเนื้อหา) แล้วค่อยทำแบบทดสอบ "
                             "[ค่าเริ่มต้น: ไม่เรียน ทำแบบฝึกหัด/แบบทดสอบอย่างเดียว]")
    parser.add_argument("--no-learn", "--quiz-only", dest="no_learn", action="store_true",
                        help="ทำเฉพาะแบบฝึกหัด/แบบทดสอบ (เป็นค่าเริ่มต้นอยู่แล้ว)")
    parser.add_argument("--skip-done", action="store_true",
                        help="ข้ามหน่วยที่ทำเสร็จแล้ว (ปกติจะทำซ้ำทุกหน่วย)")
    parser.add_argument("--no-retry-wrong", action="store_true",
                        help="ไม่ต้องลองตัวเลือกอื่นเมื่อตอบผิด (ปกติจะลองให้จนกว่าจะถูก)")
    parser.add_argument("--no-quiz", action="store_true", help="เรียนอย่างเดียว ไม่ทำแบบทดสอบ")
    parser.add_argument("--dry-run", action="store_true", help="ทดสอบดึงข้อมูลอย่างเดียว ไม่คลิกอะไร")
    parser.add_argument("--exam-password", default=None, help="รหัสผ่านของแบบสอบ")
    parser.add_argument("--profile-dir", default=str(PROFILE_DIR), help="โฟลเดอร์เก็บ session")
    parser.add_argument("--list", action="store_true", help="แสดงรายการลิงก์แล้วออก")
    args = parser.parse_args()

    args.learn = bool(args.learn) and not args.no_learn
    args.quiz = not args.no_quiz

    if not args.learn and not args.quiz:
        print("⚠️ ไม่มีงานให้ทำ: ต้องเลือกอย่างน้อยหนึ่งอย่าง (ค่าเริ่มต้นคือทำแบบฝึกหัด/แบบทดสอบ)")
        return

    tasks = load_links(args)

    print(f"🎯 จะทำทั้งหมด {len(tasks)} คอร์ส")
    for i, task in enumerate(tasks, 1):
        label = "เรียน+ทำข้อสอบ" if args.learn else "ทำข้อสอบ"
        if task.mode == "quiz":
            label = "quiz: ทำข้อสอบหน้านี้"
        print(f"   {i}. [{label}] {task.url}")

    if args.list:
        return

    if not tasks:
        print("\n❌ ไม่พบลิงก์คอร์ส")
        print("   ใส่ลิงก์ในไฟล์ thaimooc_links.txt หรือในตัวแปร COURSE_LINKS หรือพิมพ์ลิงก์ตอนรัน")
        return

    # --course-id ใช้ได้กับคอร์สเดียวเท่านั้น
    if args.course_id and len(tasks) > 1:
        print("⚠️ มีหลายคอร์ส — จะไม่ใช้ --course-id (ระบบจะอ่าน course_id จากลิงก์ของแต่ละคอร์สแทน)")
        args.course_id = None

    profile = bot.PROFILES["thaimooc"]

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=args.profile_dir,
            headless=False,
            slow_mo=300,
            viewport={"width": 1440, "height": 900},
            args=["--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # ---- ล็อกอินก่อน ----
        wait_for_login(page)

        # ---- ทำทีละคอร์ส ----
        ok = 0
        for i, task in enumerate(tasks, 1):
            print(f"\n>>> คอร์สที่ {i}/{len(tasks)}")
            try:
                if process_course(page, task, profile, args):
                    ok += 1
            except KeyboardInterrupt:
                print("\n⛔ ผู้ใช้ยกเลิก")
                break
            except Exception as e:
                print(f"⚠️ คอร์สนี้ล้มเหลว: {e}")

        print("\n" + "=" * 62)
        print(f"🏁 เสร็จ {ok}/{len(tasks)} คอร์ส")
        print("=" * 62)
        print("📌 เบราว์เซอร์เปิดค้างไว้เพื่อดูผลคะแนนจริง")
        try:
            input("👉 กด [Enter] ใน Terminal นี้เพื่อปิดเบราว์เซอร์: ")
        except EOFError:
            pass

        ctx.close()


if __name__ == "__main__":
    main()
