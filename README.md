# Auto-solver แบบทดสอบออนไลน์ (Dek-D / Thai MOOC / อื่นๆ)

ใช้ Gemini AI อ่านโจทย์ เลือกคำตอบ แล้วคลิกเลือกในเบราว์เซอร์อัตโนมัติ (Playwright)

## ติดตั้ง

```bash
pip install -U playwright google-genai python-dotenv
playwright install chromium
```

ตั้งคีย์ในไฟล์ `.env`:

```
GEMINI_API_KEY=xxxxx
```

## ไฟล์ในโปรเจกต์

| ไฟล์ | ใช้ทำอะไร |
|---|---|
| `quiz_solver.py` | **ตัวทำข้อสอบทั่วไปในเบราว์เซอร์** — Dek-D, เว็บอื่นๆ (auto-detect), และโหมด `--learn` |
| `thaimooc_bot.py` | **ตัวทำ Thai MOOC** — ล็อกอินเอง แล้วไล่เรียน + ทำแบบทดสอบทีละคอร์สจนครบจากไฟล์ลิงก์ |
| `thaimooc_links.txt` | 📥 **ไฟล์ใส่ลิงก์คอร์ส Thai MOOC** (1 บรรทัด = 1 คอร์ส) |
| `quiz_links.txt` | 📥 **ไฟล์ใส่ลิงก์แบบทดสอบทั่วไป** (Dek-D ฯลฯ) ใช้กับ `quiz_solver.py` |
| `README.md` | คู่มือนี้ |
| `browser_profile/` | session ของเบราว์เซอร์ (ล็อกอินครั้งเดียว ใช้ซ้ำได้ — ใช้ร่วมกันทั้ง 2 สคริปต์) |

---

## ไฟล์ใส่ลิงก์ (เปิดแก้ได้เลย)

เปิดไฟล์ด้วย VS Code / Notepad แล้ววางลิงก์ที่ต้องการ ตรงท้ายไฟล์

### `thaimooc_links.txt` (สำหรับ Thai MOOC)

| ที่ใส่ | ใส่อะไร | ผลลัพธ์ |
|---|---|---|
| ทั่วไป | `https://thaimooc.ac.th/courses/TAT00986` | **เรียนคอร์สให้จบ + ทำแบบทดสอบปลายคอร์ส** |
| ขึ้นต้นด้วย `quiz:` | `quiz: https://thaimooc.ac.th/etesting/xxxx` | **ทำแบบทดสอบอย่างเดียว** (ข้ามการเรียน) |

### `quiz_links.txt` (สำหรับเว็บทดสอบทั่วไป)

| ที่ใส่ | ใส่อะไร | ผลลัพธ์ |
|---|---|---|
| ทุกบรรทัด | `https://www.dek-d.com/quiz/supertest/27994/` | ทำแบบทดสอบทีละลิงก์ (ระบบเดาโปรไฟล์ให้เอง) |

### กติกาการใส่ลิงก์ (ทั้ง 2 ไฟล์)

- 1 บรรทัด = 1 ลิงก์
- ขึ้นต้นด้วย `#` = คอมเมนต์ (ข้ามไป ใช้กำกับคอมเมนต์/จดว่าทำไหนแล้วได้)
- บรรทัดว่าง = ไม่ต้องทำอะไร
- ทำเสร็จแล้วลบบรรทัดนั้นออก หรือใส่ `#` นำหน้าก็ได้
- รองรับไฟล์ที่บันทึกแบบมี BOM (utf-8 ของ Windows) อยู่แล้ว

---

## 1) Thai MOOC Academy (`learn.thaimooc.ac.th`) — `thaimooc_bot.py`

### ระบบของ Thai MOOC

| อะไร | ที่อยู่ |
|---|---|
| หน้าเว็บหลัก (portal) | `https://thaimooc.ac.th/courses/TAT00986` |
| ห้องเรียน (Open edX) | `https://learn.thaimooc.ac.th/courses/course-v1:TAT+00986+0/` |
| ล็อกอิน (SSO / ThaiD) | `https://sso.thaimooc.ac.th` — สคริปต์กดปุ่ม **ThaiD** ให้เอง |

### ขั้นตอน

**1) ใส่ลิงก์คอร์ส** — copy URL จากแถบที่อยู่ แล้ววางใน **`thaimooc_links.txt`**

```
https://thaimooc.ac.th/courses/TAT00986
https://learn.thaimooc.ac.th/courses/course-v1:TAT+00986+0/
course-v1:TAT+00986+0
TAT00986
```

> ใส่แบบไหนก็ได้ทั้ง 4 แบบ — ระบบแปลงเป็นลิงก์ห้องเรียนเดียวกันให้อัตโนมัติ
> ถ้าต้องการ "ทำแบบทดสอบอย่างเดียว" (ไม่เรียน) ให้ขึ้นต้นด้วย `quiz:` → `quiz: https://...`

**2) รัน**

```bash
python thaimooc_bot.py
```

ระบบจะ:
1. เปิดเบราว์เซอร์ไปที่หน้าเข้าสู่ระบบ → เด้งไป SSO ของ Thai MOOC → **กดปุ่ม ThaiD ให้อัตโนมัติ**
2. **คุณสแกน QR Code ด้วยแอป ThaiD** (QR หมดอายุ ~1 นาที กดขอใหม่ได้) → กด `[Enter]` กลับมาที่ Terminal
   - ถ้าเคยล็อกอินแล้ว (session ใน `browser_profile/`) จะข้ามขั้นนี้ไปเลย
   - ถ้าไม่สะดวก ThaiD สามารถเลือก LINE หรืออีเมล/รหัสผ่านเองในหน้าต่างเบราว์เซอร์ได้
3. ลงทะเบียนคอร์สให้อัตโนมัติ (ถ้ายังไม่ได้ enroll) แล้วเริ่มทำทีละคอร์ส:
   - **ค่าเริ่มต้น: ทำแบบฝึกหัด/แบบทดสอบให้ครบ** (ไม่เรียน) — ไล่ทุกหน่วย เช่น แบบทดสอบก่อนเรียน, แบบฝึกหัดบทที่ 1-4, Final Exam
     - เปิดหน้าของแต่ละหน่วย → รอโจทย์เรนเดอร์ → ให้ AI ตอบ → กด Submit → ไปหน้าถัดไปจนจบ
     - ถ้าเป็นแบบสอบ (exam) จะกด Start / ใส่รหัสผ่านให้ก่อน
   - สรุปผลท้ายคอร์ส: ชื่อหน่วย / จำนวนข้อที่ตอบ / สถานะ
   - ถ้าต้องการเรียนด้วย ให้ใส่ `--learn` (จะไล่ดูวิดีโอ/อ่านเนื้อหาก่อนแล้วค่อยทำข้อสอบ)
4. เปิดเบราว์เซอร์ค้างไว้ให้ดูผลคะแนน กด `[Enter]` เพื่อปิด

### ตัวเลือก

```bash
python thaimooc_bot.py --list                 # ดูรายการลิงก์ที่จะทำ แล้วออก
python thaimooc_bot.py --learn                # เรียนให้จบก่อน แล้วค่อยทำแบบทดสอบ
python thaimooc_bot.py --skip-done            # ข้ามหน่วยที่ทำเสร็จแล้ว (ปกติทำซ้ำทุกหน่วย)
python thaimooc_bot.py --no-quiz              # ไม่ทำข้อสอบ (ใช้คู่กับ --learn เพื่อเรียนอย่างเดียว)
python thaimooc_bot.py --dry-run              # ดูรายการหน่วยข้อสอบที่จะทำ แต่ไม่คลิกอะไร
python thaimooc_bot.py --exam-password 1234   # รหัสผ่านของแบบสอบ (ถ้าไม่ใส่จะให้พิมพ์เองในเบราว์เซอร์)
python thaimooc_bot.py "https://thaimooc.ac.th/courses/TAT00986"   # ใส่ลิงก์ตอนรัน ไม่ต้องแก้ไฟล์
python thaimooc_bot.py --links-file my.txt    # อ่านลิงก์จากไฟล์อื่น
```

---

## 2) เว็บแบบทดสอบทั่วไป (Dek-D ฯลฯ) — `quiz_solver.py`

```bash
python quiz_solver.py                                   # อ่านลิงก์จาก quiz_links.txt (ถ้าไฟล์ว่างจะใช้ Dek-D ค่าเริ่มต้น)
python quiz_solver.py "https://..."                     # ระบุลิงก์เอง (ทำทีเดียว)
python quiz_solver.py <url> --site openedx              # บังคับโปรไฟล์
python quiz_solver.py --dry-run                         # ทดสอบดึงข้อมูลอย่างเดียว ไม่คลิก ไม่ส่ง
python quiz_solver.py --no-submit                       # ตอบแต่ไม่กดส่งคำตอบ
python quiz_solver.py --links-file my.txt               # อ่านลิงก์จากไฟล์อื่น
python quiz_solver.py <url> --learn                     # โหมดเรียนคอร์ส Open edX ให้จบก่อน
```

### เว็บที่รองรับ

| โปรไฟล์ | เว็บ | หมายเหตุ |
|---|---|---|
| `dek-d` | dek-d.com/quiz | ทุกข้ออยู่หน้าเดียว กดส่งคำตอบทีเดียว |
| `thaimooc` | learn.thaimooc.ac.th (Thai MOOC Academy) | Open edX + SSO ThaiD — เลือกอัตโนมัติเมื่อเจอลิงก์ไทยมูค |
| `openedx` | edX ตัวอื่น ๆ | ต้องกด Check ทีละข้อ แล้วกด Continue ไปหน้าถัดไป |
| `generic` | เว็บอื่นทั่วไป | อัตโนมัติจัดกลุ่ม `input[type=radio]` ทั้งหน้า |

---

## เว็บที่ต้อง login

1. รันสคริปต์ → สคริปต์จะเปิดหน้า login ให้ → **ล็อกอินเอง**
   - Thai MOOC: สคริปต์กดปุ่ม **ThaiD** ให้อัตโนมัติ → สแกน QR ด้วยแอป ThaiD
2. กด Enter กลับมาที่ Terminal → สคริปต์ทำงานต่อ
3. session ถูกจดจำไว้ในโฟลเดอร์ `browser_profile/` → ครั้งหน้าไม่ต้องล็อกอินใหม่

## เพิ่มเว็บใหม่

แก้ `PROFILES` ใน `quiz_solver.py`:

```python
"mysite": {
    "match": ("mysite.com",),             # จับคู่ URL เพื่อเลือกโปรไฟล์อัตโนมัติ
    "question": ".my-question",           # บล็อกของ 1 ข้อ
    "statement": ".my-question .title",   # ข้อความโจทย์
    "submit": "button:has-text('ส่งคำตอบ')",
},
```

ถ้าโครงสร้างไม่มีในลิสต์ ให้ลอง `--site generic` ก่อน — ตัว auto-detect
จัดกลุ่ม radio ทั้งหน้าแล้วเดาโจทย์จากข้อความรอบ ๆ ให้เอง

## แก้ปัญหา (Troubleshooting)

| อาการ | วิธีแก้ |
|---|---|
| ล็อกอินไม่ผ่าน / ThaiD ไม่ขึ้น | สคริปต์เปิดหน้า SSO ให้แล้ว — กดปุ่ม **ThaiD** เองได้ในเบราว์เซอร์ หรือเลือก LINE / อีเมล-รหัสผ่าน |
| QR ThaiD หมดอายุ | กดปุ่มขอ QR ใหม่ในหน้าเว็บ แล้วสแกนอีกครั้ง (ไม่ต้องรันสคริปต์ใหม่) |
| ขึ้น "ยังไม่เห็นว่าล็อกอิน" | ล็อกอินให้เสร็จก่อน แล้วกด Enter ที่ Terminal อีกครั้ง |
| `Anonymous users do not have permission...` | ยังไม่ล็อกอิน หรือยังไม่ enroll คอร์ส — สคริปต์จะกด Enroll ให้เองหลังล็อกอิน |
| หาข้อสอบไม่เจอ | สคริปต์จะบันทึกหน้าไว้เป็น `debug_page.html` / `debug_quiz.html` — ส่งไฟล์นั้นมาให้ปรับ selector |
| อยากดูรายการลิงก์ก่อนรัน | `python thaimooc_bot.py --list` |

## หมายเหตุ

- โมเดลที่ใช้: `gemini-3.7-flash` และ fallback รุ่นอื่น (โมเดลเก่า เช่น `gemini-1.5-flash` ถูกปิดแล้ว)
- ถ้าเจอ error ชั่วคราว (503/429) สคริปต์จะ retry เองอัตโนมัติ
- คำตอบ AI ถูก cache ไว้ ไม่เรียก API ซ้ำถ้าเจอคำถามเดิม
- ถ้าคำตอบ AI ไม่ตรงกับตัวเลือกใดเลย สคริปต์จะข้ามข้อนั้น (ไม่เดา)
#
