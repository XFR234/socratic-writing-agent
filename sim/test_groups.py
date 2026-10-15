# -*- coding: utf-8 -*-
"""验证「组次」机制，并把整段对话原样打出来给研究者看。

对应许总 2026-10-15 提的现象："学生如果聊过一次再登陆，好像就不再问了"。
根因在 chat()：一组收尾后 already_wrapped 恒为 True，wrap_mode 恒为 "after"，
而那条模板写的是"不要再抛新的追问问题"——学生再来就只剩"本组已结束"，支架空转。

现在一轮可以有几组，每组只追一截：
· 第 1 组：主张层面（主张模糊／限定缺失／反驳缺席）
· 第 2 组：支撑层面（担保断裂／支撑薄弱）
· 到上限或六点追遍 → 停在最后这组，提示去改稿

用法：/usr/bin/python3 sim/test_groups.py
"""
import os
import sys
import difflib

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
NAME = "演示·分组%d" % (os.getpid() % 10000)


def start():
    """模拟一次登录/提交：返回 (conv_id, sid, 开场白)。"""
    c = A.app.test_client()
    r = c.post("/start", data={"name": NAME, "sid": "GRP",
                               "essay": ESSAY, "topic": TOPIC})
    d = r.get_json()
    assert d and d.get("ok"), d
    with c.session_transaction() as sess:
        conv_id = sess.get("conv_id")
        sid = sess["student_id"]
    opening = (d.get("history") or [{}])[-1].get("content", "")
    return conv_id, sid, opening


def talk(conv_id, sid, ans):
    cc = A.app.test_client()
    with cc.session_transaction() as sess:
        sess["student_id"] = sid
        sess["conv_id"] = conv_id
    rr = cc.post("/chat", data={"message": ans})
    return rr.get_json()


def run_group(seq, label, max_turns=12):
    conv_id, sid, opening = start()
    print("  [%s·开场] %s\n" % (label, opening))
    strategies, points, turns, wrapped = [], [], 0, False
    prev_text = opening
    for i in range(max_turns):
        ans = seq[i % len(seq)]
        dd = talk(conv_id, sid, ans)
        if not dd or dd.get("error"):
            print("  %s FAILED: %s" % (label, dd))
            break
        reply = dd.get("reply") or ""
        conn = A.get_db()
        row = conn.execute(
            "SELECT point FROM messages WHERE conversation_id=? AND role='assistant' "
            "ORDER BY id DESC LIMIT 1", (conv_id,)).fetchone()
        conn.close()
        sim = difflib.SequenceMatcher(None, prev_text[:400], reply[:400]).ratio()
        strategies.append(dd.get("strategy"))
        points.append(row["point"] if row else None)
        turns += 1
        print("  [学生] %s" % ans)
        print("  [AI]   %s" % reply)
        print("        点=%s ｜ 策略=%s ｜ 相似=%.0f%% ｜ 问句=%d ｜ 收尾=%s\n"
              % (row["point"] if row else None, dd.get("strategy"),
                 sim * 100, reply.count("？") + reply.count("?"), dd.get("wrapped")))
        prev_text = reply
        if dd.get("wrapped"):
            wrapped = True
            break
    return conv_id, turns, strategies, wrapped, points


# 第 1 组：正经作答 -> 追主张层面（含糊 / 太满 / 没反方）
SEQ1 = [
    "兼听就是要多听别人的意见，不能只听一方面的。",
    "因为每个人的经历不一样，看到的东西也不同，合起来才更全面。",
    "比如团队讨论的时候，多听几个人的想法再做决定会更周全。",
    "也会有例外，如果大家信息都不对，听了反而更乱。",
    "有人可能觉得听太多会犹豫，但我觉得还是比只听一方好。",
    "如果重写，我会把“兼听”的前提先写清楚。",
]
# 第 2 组：学生再来 -> 应落在支撑层面（桥断了 / 货太少）
SEQ2 = [
    "我再想想，其实我想说的就是兼听能让人判断更准。",
    "因为听得多，才知道哪一种是真的。",
    "嗯，可能第二段那个例子没有说清楚它为什么能说明这个道理。",
    "如果重写，我会在那个例子后面补一句解释。",
    "我觉得现在说清楚了。",
]

print("=" * 64)
print("演示：同一学生、同一篇作文，连聊两组")
print("=" * 64 + "\n")

c1, t1, s1, w1, p1 = run_group(SEQ1, "第1组")
print("-" * 64)
print("第1组结束：conv_id=%s  回合=%d  收尾=%s" % (c1, t1, w1))
print("断裂点：%s" % p1)
print("策略序列：%s" % s1)
print("-" * 64 + "\n")

print(">>> 学生退出后再次登录（系统应自动开第 2 组）\n")
c2, t2, s2, w2, p2 = run_group(SEQ2, "第2组")
print("-" * 64)
print("第2组结束：conv_id=%s  回合=%d  收尾=%s" % (c2, t2, w2))
print("断裂点：%s" % p2)
print("策略序列：%s" % s2)
print("-" * 64)

# ---- 断言 1：组次切换（收尾后学生再来会自动开下一组）----
checks = []
checks.append(("第2组是新开的一组（conv_id 变了）", c2 != c1))
checks.append(("两组都正常收尾", bool(w1 and w2)))

# ---- 断言 2：按截分组——一组只追一截，不许跨截 ----
G1_OK = {"退一步", "对立面", "跳出去"}          # 主张层面
G2_OK = {"环顾式", "进一步"}                    # 支撑层面
bad1 = [s for s in s1 if s and s != "回顾看" and s not in G1_OK]
bad2 = [s for s in s2 if s and s != "回顾看" and s not in G2_OK]
checks.append(("第1组只追主张层面（不出现环顾式/进一步）", not bad1))
checks.append(("第2组只追支撑层面（不出现退一步/对立面/跳出去）", not bad2))
if bad1:
    print("  ⚠ 第1组跨截：%s" % bad1)
if bad2:
    print("  ⚠ 第2组跨截：%s" % bad2)

# ---- 断言 3：追问回合的策略列不留空（卡片是指派的，不该为空）----
blank1 = [i + 1 for i, s in enumerate(s1[:-1]) if not s]
blank2 = [i + 1 for i, s in enumerate(s2[:-1]) if not s]
checks.append(("第1组策略列无空格（末回合为收尾，不计）", not blank1))
checks.append(("第2组策略列无空格（末回合为收尾，不计）", not blank2))
if blank1:
    print("  ⚠ 第1组空策略出现在第 %s 回合" % blank1)
if blank2:
    print("  ⚠ 第2组空策略出现在第 %s 回合" % blank2)

print()
for name, passed in checks:
    print("  %s  %s" % ("✅" if passed else "❌", name))
ok = all(p for _, p in checks)
print("\n结论：" + ("组次机制正常：学生再来会开新一组，且一组只追一截 ✅"
                  if ok else "仍有问题 ❌"))
sys.exit(0 if ok else 1)
