"""ทดสอบ fetch_course_units (ไม่ต้องต่อเน็ต) — ตรวจว่า walk ได้ถูกและไม่ซ้ำ"""
import quiz_solver as bot


class FakePage:
    def __init__(self, data):
        self.data = data
        self.url = "https://learn.thaimooc.ac.th/courses/course-v1:X+1+0/courseware/"

    def evaluate(self, script, arg=None):
        return self.data


DATA = {
    "root": "block-v1:X+1+0+type@course+block@course",
    "blocks": {
        "block-v1:X+1+0+type@course+block@course": {
            "type": "course",
            "display_name": "คอร์สทดสอบ",
            "children": ["ch1", "ch2"],
        },
        "ch1": {"type": "chapter", "display_name": "บทที่ 1", "children": ["seq1"]},
        "ch2": {"type": "chapter", "display_name": "Final", "children": ["seq2"]},
        "seq1": {"type": "sequential", "display_name": "แบบฝึกหัด", "children": ["vert1"]},
        "seq2": {"type": "sequential", "display_name": "Final Exam", "children": ["vert2"]},
        "vert1": {
            "type": "vertical",
            "display_name": "แบบฝึกหัด",
            "block_url": "/courses/course-v1:X+1+0/jump_to/vert1",
            "children": ["p1", "p2"],
            "completion": {"complete": True},
        },
        "vert2": {
            "type": "vertical",
            "display_name": "Final Exam",
            "block_url": "/courses/course-v1:X+1+0/jump_to/vert2",
            "children": ["p3"],
            "completion": {"complete": False},
        },
        "p1": {"type": "problem", "display_name": "ข้อ 1",
               "block_url": "/courses/course-v1:X+1+0/jump_to/p1"},
        "p2": {"type": "problem", "display_name": "ข้อ 2",
               "block_url": "/courses/course-v1:X+1+0/jump_to/p2"},
        "p3": {"type": "problem", "display_name": "ข้อ 3",
               "block_url": "/courses/course-v1:X+1+0/jump_to/p3"},
    },
}

units = bot.fetch_course_units(FakePage(DATA), "course-v1:X+1+0")
for u in units:
    print(u)

assert len(units) == 2, f"คาดว่า 2 หน่วย แต่ได้ {len(units)}"
assert units[0]["name"] == "แบบฝึกหัด" and units[0]["types"] == ["problem", "problem"]
assert units[0]["done"] is True and units[0]["path"] == "บทที่ 1 / แบบฝึกหัด"
assert units[1]["name"] == "Final Exam" and units[1]["types"] == ["problem"]
assert units[1]["done"] is False
print("\n✅ ผ่านทั้งหมด")
