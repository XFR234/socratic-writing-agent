# -*- coding: utf-8 -*-
"""复现"学生答不出、AI 反复问同一处"的场景，验证不再原地打转。

背景（许总 2026-10-15 截图）：学生说"我想不出来"→AI 给抓手重问→学生答了→
又问同一处→学生说"怎么又问我重复的问题"→还问→"我刚才说过了"→还问。
连追四轮同一处，学生越答越抵触，这组数据是废的。

机制层修了三处：①"同点最多两轮"的配额提到所有分支之外（原先求助分支绕开了它）；
②连续求助到第三次直接收尾；③生成后与上一条比对，太像就重生成。
本脚本用一组"答不出 + 抗议"的真实应答跑一遍，看是否还打转。

用法：/usr/bin/python3 sim/test_no_loop.py
"""
import os
import re
import sys
import json
import difflib

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.path.join(BASE, "sim")
sys.path.insert(0, BASE)
DB = os.path.join(SIM, "no_loop.db")
if os.path.exists(DB):
    os.remove(DB)
os.environ["DB_PATH"] = DB
os.chdir(BASE)

import app as A  # noqa: E402

A.init_db()
ESSAY = open(os.path.join(SIM, "cases", "p5_黄晨兴.txt"), encoding="utf-8").read()
TOPIC, MATERIAL, BODY = None, None, []
for ln in ESSAY.split("\n"):
    s = ln.strip()
    if s.startswith("题目："):
        TOPIC = s.split("：", 1)[1]
    elif s.startswith("材料："):
        MATERIAL = s.split("：", 1)[1]
    elif s:
        BODY.append(ln)
ESSAY = "\n".join(BODY)

# 学生应答：一路答不出、并抗议重复（截图里的原样）
ANSWERS = [
    "我觉得就是配合吧。",
    "我想不出来。",
    "就这么多了，我说不上来别的。",
    "怎么又问我重复的问题？",
    "我刚才说过了。",
    "我真的答不上来了。",
    "我不会了。",
]

c = A.app.test_client()
r = c.post("/start", data={"name": "模拟·答不出", "sid": "LOOP1", "essay": ESSAY,
                           "topic": TOPIC, "material": MATERIAL})
d = r.get_json()
assert d and d.get("ok"), d
with c.session_transaction() as sess:
    st = {"student_id": sess["student_id"], "conv_id": sess["conv_id"]}

print("== 开场 ==\n" + d["history"][-1]["content"] + "\n")

conn = A.get_db()
conv_id = st["conv_id"]
prev_text = d["history"][-1]["content"]
rows_log = []
for i, ans in enumerate(ANSWERS, 1):
    cc = A.app.test_client()
    with cc.session_transaction() as sess:
        sess["student_id"] = st["student_id"]
        sess["conv_id"] = st["conv_id"]
    rr = cc.post("/chat", data={"message": ans})
    dd = rr.get_json()
    if not dd or dd.get("error"):
        print("FAILED:", rr.status_code, dd)
        break
    # 从库里取这一条 assistant 的 point / help_type（返回值里没有）
    row = conn.execute(
        "SELECT point, help_type FROM messages WHERE conversation_id=? "
        "AND role='assistant' ORDER BY id DESC LIMIT 1", (conv_id,)).fetchone()
    sim = difflib.SequenceMatcher(None, prev_text[:400],
                                  (dd.get("reply") or "")[:400]).ratio()
    reply = dd.get("reply") or ""
    known = A.allowed_book_titles(ESSAY, "")
    fake = [t for t in re.findall(r"《([^》]{1,16})》", reply) if t not in known]
    n_q = reply.count("？") + reply.count("?")
    print("[学生] " + ans)
    print("[AI]   " + reply)
    print("-- 点：%s ｜ 求助：%s ｜ 策略：%s ｜ 收尾：%s ｜ 与上一轮相似：%.0f%% "
          "｜ 问句数：%d ｜ 虚构书名：%s --\n"
          % (row["point"], row["help_type"] or "—", dd.get("strategy"),
             dd.get("wrapped"), sim * 100, n_q, fake or "无"))
    rows_log.append({"ans": ans, "reply": reply, "point": row["point"],
                     "help": row["help_type"], "sim": sim,
                     "wrapped": dd.get("wrapped"), "n_q": n_q, "fake": fake})
    prev_text = dd.get("reply") or ""
    if dd.get("wrapped"):
        print("=== 已收尾 ===")
        break
conn.close()

# 判定
pts = [x["point"] for x in rows_log if x["point"]]
worst_run, cur, n = [], None, 0
for p in pts:
    if p == cur:
        n += 1
    else:
        cur, n = p, 1
    worst_run.append((p, n))
max_same = max([n for _, n in worst_run], default=0)
max_sim = max([x["sim"] for x in rows_log], default=0)
wrapped = any(x["wrapped"] for x in rows_log)
max_q = max([x["n_q"] for x in rows_log], default=0)
fakes = [t for x in rows_log for t in x["fake"]]

print("=" * 60)
print("同一个断裂点最多连追：%d 轮（要求 ≤2）" % max_same)
print("与上一轮最大相似度：%.0f%%（要求 <55%%）" % (max_sim * 100))
print("单回合最多问句数：%d（要求 <%d）" % (max_q, A.MAX_QUESTIONS_PER_TURN))
print("虚构书名：%s（要求 0）" % (fakes or "无"))
print("收尾：%s" % ("是" if wrapped else "否"))
ok = (max_same <= 2 and max_sim < 0.55 and wrapped
      and max_q < A.MAX_QUESTIONS_PER_TURN and not fakes)
print("\n结论：" + ("不再原地打转 ✅" if ok else "仍有问题 ❌"))
sys.exit(0 if ok else 1)
