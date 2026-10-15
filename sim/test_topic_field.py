# -*- coding: utf-8 -*-
"""学生端必填的「题干信息」字段：前端有框、后端必填、落库、进提示词、进 CSV。

背景（2026-10-15 许总）：线上回到 09-29 的 f15d84b（四之前）后再改，
那个版本学生端只有姓名/学号/作文。加上题干输入框并设为必填——
没有题干，AI 只看得到学生写了什么，看不出扣没扣题。

用法：/usr/bin/python3 sim/test_topic_field.py
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
DB = "/tmp/topic_field_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DB_PATH"] = DB
os.environ["ZHIPU_API_KEY"] = ""          # demo 模式，不调模型

import app as A  # noqa: E402
from prompts import build_stage1_system  # noqa: E402

A.init_db()
checks = []


def ck(name, ok, extra=""):
    checks.append((name, ok))
    print(("  ok   " if ok else "  FAIL ") + name + (("  | " + extra) if extra else ""))


TOPIC = ("题目：兼听则明\n材料：有人说，听取不同意见，会让我们明辨是非、智慧通达。"
         "也有人说，大数据时代每天都有各种“声音”涌来，容易迷失自我。\n"
         "要求：以“兼听则明”为题，写一篇议论文。")
ESSAY = "兼听则明，偏信则暗。古人早已告诉我们，只听一面之词容易偏颇。" * 6

print("== A 学生端：有题干框，且必填 ==")
c = A.app.test_client()
html = c.get("/").get_data(as_text=True)
ck("页面有题干输入框", 'id="topic"' in html)
ck("标注必填（红星）", "题干信息 *</label>" in html)
ck("文案写清是填写题干", "题干信息" in html and "抄" in html)
ck("提交时带上 topic 参数", "topic=${encodeURIComponent(topic)}" in html)
ck("缺题干时前端拦住", "请填写题干信息" in html)

print("\n== B 后端：题干必填（前端只是提醒，接口本身要拦）==")
r = c.post("/start", data={"name": "甲", "essay": ESSAY, "topic": ""})
ck("空题干被拒", r.status_code == 400, "HTTP %s" % r.status_code)
ck("报错文案讲清要抄什么", bool(r.get_json()) and "题干" in r.get_json().get("error", ""),
   str(r.get_json().get("error", ""))[:24])

r = c.post("/start", data={"name": "甲", "essay": ESSAY, "topic": "兼听则明"})
ck("只有题目四个字也能过（不卡）", bool(r.get_json().get("ok")),
   str(r.get_json().get("error", "")))

print("\n== C 落库 ==")
conn = A.get_db()
row = conn.execute("SELECT topic FROM conversations ORDER BY id DESC LIMIT 1").fetchone()
ck("题干存进库", row["topic"] == "兼听则明", str(row["topic"]))
conn.close()

print("\n== D 进提示词（AI 得拿它当尺子）==")
sys_p = build_stage1_system(1, "", TOPIC)
ck("题干进系统提示词", "【本次作文题的题干" in sys_p and "兼听则明" in sys_p)
ck("提示词要它别直接讲题意", "不要直接告诉他题目是什么意思" in sys_p)
ck("没题干时不注入整块", "【本次作文题的题干" not in build_stage1_system(1, "", ""))

print("\n== E 每个来回都带着（不只开场一次）==")
with c.session_transaction() as sess:
    st = {"student_id": sess["student_id"], "conv_id": sess["conv_id"]}
cc = A.app.test_client()
with cc.session_transaction() as sess:
    sess["student_id"] = st["student_id"]
    sess["conv_id"] = st["conv_id"]
rr = cc.post("/chat", data={"message": "因为多听才能全面了解事情。"})
ck("对话能正常走", bool(rr.get_json().get("reply")), str(rr.get_json().get("error", ""))[:30])

print("\n== F 进 CSV（研究要用）==")
with c.session_transaction() as sess:
    sess["admin"] = True
r = c.get("/admin/export")
txt = r.get_data(as_text=True)
ck("CSV 有题干列", "题干" in txt.split("\n")[0], txt.split("\n")[0][:40])
ck("CSV 里有内容", "兼听则明" in txt)

bad = [n for n, ok in checks if not ok]
print("\n共 %d 项，通过 %d，失败 %d" % (len(checks), len(checks) - len(bad), len(bad)))
sys.exit(1 if bad else 0)
