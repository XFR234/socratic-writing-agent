# -*- coding: utf-8 -*-
"""验证「组次」机制：一组收尾后，学生再来 -> 自动开下一组，且不再重复追已追过的点。

对应许总 2026-10-15 提的现象："学生如果聊过一次再登陆，好像就不再问了"。
根因是收尾后 wrap_mode 恒为 "after"，那条模板是"不要再抛新的追问问题"。

本脚本走真实路由（test_client + 手动恢复 session），与线上完全一致：
  /start 交作文 -> 一路聊到收尾 -> 重新 /start（模拟再次登录）-> 应开第 2 组。

用法：/usr/bin/python3 sim/test_groups.py
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.path.join(BASE, "sim")
sys.path.insert(0, BASE)
DB = os.path.join(SIM, "groups.db")
if os.path.exists(DB):
    os.remove(DB)
os.environ["DB_PATH"] = DB
os.chdir(BASE)

import app as A  # noqa: E402

A.init_db()

raw = open(os.path.join(SIM, "cases", "p1_敬子睿.txt"), encoding="utf-8").read()
TOPIC, BODY = "", []
for ln in raw.split("\n"):
    s = ln.strip()
    if s.startswith("题目："):
        TOPIC = s.split("：", 1)[1]
    elif not s.startswith(("材料：", "要求：")):
            BODY.append(ln)
ESSAY = "\n".join(BODY).strip()
NAME = "模拟·分组%d" % (os.getpid() % 10000)


def start():
    """模拟一次登录/提交。返回 (conv_id, sid)。"""
    c = A.app.test_client()
    r = c.post("/start", data={"name": NAME, "sid": "GRP",
                               "essay": ESSAY, "topic": TOPIC})
    d = r.get_json()
    assert d and d.get("ok"), d
    with c.session_transaction() as sess:
        return sess.get("conv_id"), sess["student_id"]


def talk(conv_id, sid, ans):
    cc = A.app.test_client()
    with cc.session_transaction() as sess:
        sess["student_id"] = sid
        sess["conv_id"] = conv_id
    rr = cc.post("/chat", data={"message": ans})
    return rr.get_json()


def run_group(seq, label, max_turns=12):
    conv_id, sid = start()
    strategies, turns, wrapped = [], 0, False
    for i in range(max_turns):
        dd = talk(conv_id, sid, seq[i % len(seq)])
        if not dd or dd.get("error"):
            print("  %s FAILED: %s" % (label, dd))
            break
        turns += 1
        strategies.append(dd.get("strategy"))
        if dd.get("wrapped"):
            wrapped = True
            break
    return conv_id, turns, strategies, wrapped


SEQ1 = [
    "兼听就是要多听别人的意见，不能只听一方面的。",
    "因为每个人的经历不一样，看到的东西也不同，合起来才更全面。",
    "比如团队讨论的时候，多听几个人的想法再做决定会更周全。",
    "也会有例外，如果大家信息都不对，听了反而更乱。",
    "有人可能觉得听太多会犹豫，但我觉得还是比只听一方好。",
]
SEQ2 = [
    "我明白你的意思了。",
    "我再想想：其实是要先判断信息来源，再决定听不听。",
    "如果重写，我会把“兼听”的前提先写清楚。",
    "我觉得第二段那个例子跟观点的连接还是有点跳。",
    "对，我应该补一句说明它为什么能说明这个道理。",
]

c1, t1, s1, w1 = run_group(SEQ1, "第1组")
print("第1组：conv_id=%s 回合=%d 收尾=%s 策略=%s\n" % (c1, t1, w1, s1))
c2, t2, s2, w2 = run_group(SEQ2, "第2组")
print("第2组：conv_id=%s 回合=%d 收尾=%s 策略=%s\n" % (c2, t2, w2, s2))
c3, t3, s3, w3 = run_group(SEQ2, "第3组")
print("第3组：conv_id=%s 回合=%d 收尾=%s 策略=%s\n" % (c3, t3, w3, s3))

ok = True
print("=" * 60)
if c2 != c1:
    print("✅ 收尾后再次登录 -> 自动开了新一组（%s -> %s）" % (c1, c2))
else:
    print("❌ 收尾后再次登录仍复用旧组 —— 现象未消除")
    ok = False
if c3 == c2:
    print("✅ 达到组上限后不再开新组（停在 %s）" % c2)
else:
    print("❌ 超出上限仍在开新组（%s -> %s）" % (c2, c3))
    ok = False

conn = A.get_db()
stu = conn.execute("SELECT id FROM students WHERE name=?", (NAME,)).fetchone()
rows = conn.execute("SELECT id, group_no FROM conversations WHERE student_id=? "
                    "ORDER BY id", (stu["id"],)).fetchall()
pts = conn.execute(
    "SELECT m.point FROM messages m JOIN conversations c ON c.id=m.conversation_id "
    "WHERE c.student_id=? AND m.role='assistant' AND m.point IS NOT NULL "
    "AND m.point<>''", (stu["id"],)).fetchall()
conn.close()
print("\n该生本轮的对话：%s" % [(r["id"], r["group_no"]) for r in rows])
print("跨组已追过的点：%s" % [p["point"] for p in pts])
print("\n结论：" + ("组次机制正常 ✅" if ok else "仍有问题 ❌"))
sys.exit(0 if ok else 1)
