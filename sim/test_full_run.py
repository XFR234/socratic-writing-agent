# -*- coding: utf-8 -*-
"""正常作答场景走完整组对话（真实 API），统计三项硬指标：
单回合问句数 / 与上一轮的相似度 / 虚构书名 + 是否正常收尾。

这几项就是这次加的生成期复检要盯的：普通对话里它们不该触发，
一旦触发说明判据定得太紧（频繁重生成会把对话拖慢、也会把话说僵）。

用法：/usr/bin/python3 sim/test_full_run.py [case文件名]
"""
import os
import re
import sys
import time
import difflib

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.path.join(BASE, "sim")
sys.path.insert(0, BASE)
DB = os.path.join(SIM, "full_run.db")
if os.path.exists(DB):
    os.remove(DB)
os.environ["DB_PATH"] = DB
os.chdir(BASE)

import app as A  # noqa: E402

A.init_db()
CASE = sys.argv[1] if len(sys.argv) > 1 else "p5_黄晨兴.txt"
ESSAY = open(os.path.join(SIM, "cases", CASE), encoding="utf-8").read()
TOPIC, BODY = "", []
for ln in ESSAY.split("\n"):
    s = ln.strip()
    if s.startswith("题目："):
        TOPIC = s.split("：", 1)[1]
    elif not s.startswith(("材料：", "要求：")):
        BODY.append(ln)
ESSAY = "\n".join(BODY).strip()
print("用例：%s ｜ 题目：%s ｜ 字数：%d\n" % (CASE, TOPIC, len(ESSAY)))

ANSWERS = [
    "兼听就是要多听别人的意见，不能只听一方面的。",
    "比如我作文里写到的北斗，每一颗卫星都有自己的功用，配合起来才完整。",
    "因为每颗卫星观测的角度不一样，单独一颗看不全，所以要互相配合。",
    "也会有例外吧，如果听到了错误的信息，反而会影响判断。",
    "有人可能会说，听太多反而会犹豫不决，错失良机。",
    "我会补一个限定：兼听是自己有判断力的基础上听取意见，不是盲从。",
    "这么聊下来我发现我最大的问题是没说清楚“兼听”的前提是什么。",
    "如果重写，我会先把“兼听”的定义写清楚，再举例子。",
]

c = A.app.test_client()
r = c.post("/start", data={"name": "模拟·正常作答", "sid": "FULL1",
                           "essay": ESSAY, "topic": TOPIC})
d = r.get_json()
assert d and d.get("ok"), d
with c.session_transaction() as sess:
    st = {"student_id": sess["student_id"], "conv_id": sess["conv_id"]}

conn = A.get_db()
prev_text = d["history"][-1]["content"]
known = A.allowed_book_titles(ESSAY, "")
log = []
for i, ans in enumerate(ANSWERS, 1):
    cc = A.app.test_client()
    with cc.session_transaction() as sess:
        sess["student_id"] = st["student_id"]
        sess["conv_id"] = st["conv_id"]
    t0 = time.time()
    rr = cc.post("/chat", data={"message": ans})
    secs = time.time() - t0
    dd = rr.get_json()
    if not dd or dd.get("error"):
        print("FAILED:", rr.status_code, dd)
        break
    reply = dd.get("reply") or ""
    row = conn.execute("SELECT point, help_type FROM messages WHERE conversation_id=? "
                       "AND role='assistant' ORDER BY id DESC LIMIT 1",
                       (st["conv_id"],)).fetchone()
    sim = difflib.SequenceMatcher(None, prev_text[:400], reply[:400]).ratio()
    n_q = reply.count("？") + reply.count("?")
    fake = [t for t in re.findall(r"《([^》]{1,16})》", reply) if t not in known]
    print("[学生] " + ans)
    print("[AI]   " + reply)
    print("-- 点：%s ｜ 策略：%s ｜ 收尾：%s ｜ 相似：%.0f%% ｜ 问句：%d ｜ "
          "虚构：%s ｜ 耗时：%.1fs --\n"
          % (row["point"], dd.get("strategy"), dd.get("wrapped"),
             sim * 100, n_q, fake or "无", secs))
    log.append({"n_q": n_q, "sim": sim, "fake": fake, "sec": secs,
                "wrapped": dd.get("wrapped")})
    prev_text = reply
    if dd.get("wrapped"):
        print("=== 已收尾 ===")
        break
conn.close()

print("=" * 60)
print("回合数：%d" % len(log))
print("单回合最多问句数：%d（要求 <%d）" % (max([x["n_q"] for x in log], default=0),
                                     A.MAX_QUESTIONS_PER_TURN))
print("与上一轮最大相似度：%.0f%%（要求 <55%%）"
      % (max([x["sim"] for x in log], default=0) * 100))
print("虚构书名：%s" % ([t for x in log for t in x["fake"]] or "无"))
print("平均耗时：%.1fs/回合" % (sum([x["sec"] for x in log]) / max(len(log), 1)))
print("收尾：%s" % ("是" if any(x["wrapped"] for x in log) else "否"))
