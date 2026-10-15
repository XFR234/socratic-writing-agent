# -*- coding: utf-8 -*-
"""调试用：跑一个来回，把每次模型调用（生成 / 判定）的内容与决策打出来。

用来查"为什么重写没生效"这类问题——不看调用过程就只能猜。
用法：/usr/bin/python3 sim/debug_one.py [case] [学生第1句话]
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.path.join(BASE, "sim")
sys.path.insert(0, BASE)
DB = os.path.join(SIM, "debug.db")
if os.path.exists(DB):
    os.remove(DB)
os.environ["DB_PATH"] = DB
os.chdir(BASE)

import app as A  # noqa: E402

A.init_db()
CASE = sys.argv[1] if len(sys.argv) > 1 else "p3_夏晗茜.txt"
FIRST = sys.argv[2] if len(sys.argv) > 2 else "兼听就是要多听别人的意见，不能只听一方面的。"

raw = open(os.path.join(SIM, "cases", CASE), encoding="utf-8").read()
topic, body = "", []
for ln in raw.split("\n"):
    s = ln.strip()
    if s.startswith("题目："):
        topic = s.split("：", 1)[1]
    elif not s.startswith(("材料：", "要求：")):
        body.append(ln)
essay = "\n".join(body).strip()

orig = A.call_zhipu
_n = {"i": 0}


def spy(system_prompt, history, json_mode=False, temperature=0.6):
    _n["i"] += 1
    kind = "判定" if json_mode else "生成"
    print("  >>> 第 %d 次调用（%s）system=%d字 history=%d条"
          % (_n["i"], kind, len(system_prompt), len(history)))
    out = orig(system_prompt, history, json_mode=json_mode, temperature=temperature)
    print("  <<< 返回：%s" % (out or "").replace("\n", " ")[:120])
    return out


A.call_zhipu = spy

c = A.app.test_client()
r = c.post("/start", data={"name": "调试", "sid": "DBG", "essay": essay, "topic": topic})
d = r.get_json()
with c.session_transaction() as sess:
    st = {"student_id": sess["student_id"], "conv_id": sess["conv_id"]}
print("开场：%s\n" % (d["history"][-1]["content"])[:80])

cc = A.app.test_client()
with cc.session_transaction() as sess:
    sess["student_id"] = st["student_id"]
    sess["conv_id"] = st["conv_id"]
print("第 1 回合：")
rr = cc.post("/chat", data={"message": FIRST})
dd = rr.get_json()
print("  策略=%s 收尾=%s" % (dd.get("strategy"), dd.get("wrapped")))
print("  正文：%s" % (dd.get("reply") or "").replace("\n", " "))
