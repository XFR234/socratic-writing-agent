# -*- coding: utf-8 -*-
"""批量跑若干篇真实作文（真实 API），统计**策略分布**与三项硬指标。

用来回答"五张策略卡是不是都用上了、哪一张从没出现过"——
策略绑定断裂点之前，「环顾式」几乎不出现，这张表能把它看出来。

用法：/usr/bin/python3 sim/test_strategy_mix.py [篇数]
"""
import os
import re
import sys
import time
import glob
import difflib

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.path.join(BASE, "sim")
sys.path.insert(0, BASE)
DB = os.path.join(SIM, "strategy_mix.db")
if os.path.exists(DB):
    os.remove(DB)
os.environ["DB_PATH"] = DB
os.chdir(BASE)

import app as A  # noqa: E402

A.init_db()

# 一组"正常作答"的应答序列：含糊 → 举例 → 给机制 → 说限定 → 想反方 → 自我修正
ANSWERS = [
    "兼听就是要多听别人的意见，不能只听一方面的。",
    "比如我作文里写到的事，说明只靠一个人确实看不全。",
    "因为每个人掌握的信息不一样，合起来才更接近真相。",
    "也会有例外吧，如果听到的信息本身就是错的，反而会影响判断。",
    "有人可能会说，听太多反而会犹豫不决，错失良机。",
    "我会补一个限定：兼听是建立判断力之上的，不是什么都听。",
    "这么聊下来我发现最大的问题是没把前提写清楚。",
]

files = sorted(glob.glob(os.path.join(SIM, "cases", "p*_*.txt")))
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 3
files = files[:LIMIT]

all_strategies = []
for path in files:
    raw = open(path, encoding="utf-8").read()
    topic, body = "", []
    for ln in raw.split("\n"):
        s = ln.strip()
        if s.startswith("题目："):
            topic = s.split("：", 1)[1]
        elif not s.startswith(("材料：", "要求：")):
            body.append(ln)
    essay = "\n".join(body).strip()
    print("=" * 60)
    print("用例：%s ｜ 题目：%s" % (os.path.basename(path), topic))

    c = A.app.test_client()
    # 每篇用一个独立姓名：同名＝同一学生，会复用/串进历史上下文
    r = c.post("/start", data={"name": "模拟·策略统计%d" % files.index(path), "sid": "MIX",
                               "essay": essay, "topic": topic})
    d = r.get_json()
    if not d or d.get("error"):
        print("  FAILED:", d)
        continue
    with c.session_transaction() as sess:
        st = {"student_id": sess["student_id"], "conv_id": sess["conv_id"]}

    conn = A.get_db()
    prev_text = d["history"][-1]["content"]
    for ans in ANSWERS:
        cc = A.app.test_client()
        with cc.session_transaction() as sess:
            sess["student_id"] = st["student_id"]
            sess["conv_id"] = st["conv_id"]
        rr = cc.post("/chat", data={"message": ans})
        dd = rr.get_json()
        if not dd or dd.get("error"):
            print("  FAILED:", dd)
            break
        reply = dd.get("reply") or ""
        row = conn.execute(
            "SELECT strategy, point FROM messages WHERE conversation_id=? "
            "AND role='assistant' ORDER BY id DESC LIMIT 1", (st["conv_id"],)).fetchone()
        sim = difflib.SequenceMatcher(None, prev_text[:400], reply[:400]).ratio()
        n_q = reply.count("？") + reply.count("?")
        print("  [点 %s] [策略 %s] 相似 %.0f%% 问句 %d ｜ %s"
              % (row["point"], dd.get("strategy"), sim * 100, n_q, reply[:60]))
        all_strategies.append(dd.get("strategy"))
        prev_text = reply
        if dd.get("wrapped"):
            print("  === 已收尾 ===")
            break
    conn.close()

print("\n" + "=" * 60)
print("策略分布（共 %d 回合）：" % len(all_strategies))
for s in A.STRATEGY_ORDER:
    n = all_strategies.count(s)
    print("  %-6s %2d 次 %s" % (s, n, "█" * n))
missing = [s for s in ("退一步", "环顾式", "对立面", "进一步")
           if s not in all_strategies]
print("\n从未出现过：%s" % (missing or "无 —— 五张卡都用到 ✅"))
