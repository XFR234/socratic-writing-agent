# -*- coding: utf-8 -*-
"""用假模型验证"生成—检查—重写"这条链路本身（不调真实 API，秒级）。

验证三件事，都是刚移植到这个分支上的：
1) 三问过载：模型一次甩 3 个问句 → 要求重写；两次不改 → 确定性截断到第一个问句。
2) 原地打转：模型把上一轮的话换个词又发一遍 → 相似度超阈值 → 重写。
3) 虚构书名：模型自己造一本《竹林中》 → 命中白名单检查 → 重写。

真实模型的验证另见 sim/test_no_loop.py（那个走真实 API）。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
DB = os.path.join(BASE, "sim", "guard.db")
if os.path.exists(DB):
    os.remove(DB)
os.environ["DB_PATH"] = DB
os.chdir(BASE)

import app as A  # noqa: E402

A.init_db()
A.ZHIPU_API_KEY = "fake-key-for-test"
A.judge_turn = lambda *a, **k: ("环顾式", False)   # 判定不参与本用例

ESSAY = ("作文正文示例。兼听则明，偏信则暗。我们要听取多方意见，"
         "也要保有定力，不被信息洪流裹挟。")
HISTORY = [("user", "【学生本轮提交的作文】" + ESSAY),
           ("user", "我觉得就是要听别人的意见。"),
           ("assistant", "你说的“听别人的意见”具体指哪些人？")]

# 用例：(模型每次依次返回的内容, 期望通过的条件)
cases = [
    ("三问过载→重写→仍三问→截断",
     ["你说完这句后，那这个概念具体指什么？有没有例外？换一种说法你会怎么界定？"],
     lambda t: t.count("？") == 1),
    ("三问过载→重写后收敛到一问",
     ["第一问问？第二问问？第三问问？",
      "这里的关键到底落在哪一步？"],
     lambda t: t.count("？") == 1),
    ("原地打转→相似度超阈值→重写",
     ["你说的“听别人的意见”具体指哪些对象、范围多大？",
      "重写版：你作文里那一句是怎么接到这句结论上的？"],
     lambda t: "重写版" in t),
    ("虚构书名→重写→仍提→再重写",
     ["你读过的《竹林中》里是怎么讲的？",
      "你自己作文里那一句是怎么接的？"],
     lambda t: "竹林中" not in t),
]

ok_all = True
for name, replies, cond in cases:
    calls = {"n": 0}

    def fake_call(system_prompt, history, json_mode=False, temperature=0.6):
        i = min(calls["n"], len(replies) - 1)
        calls["n"] += 1
        return replies[i]

    A.call_zhipu = fake_call
    text, strategy, wrap = A.generate_assistant(
        1, HISTORY, last_user="我觉得就是要听别人的意见。", essay=ESSAY)
    ok = cond(text or "")
    ok_all = ok and ok_all
    print("[%s] 调用 %d 次 → %r" % ("PASS" if ok else "FAIL", calls["n"], text))
    print("        %s\n" % name)

print("=" * 60)
print("结论：" + ("生成期复检链路正常 ✅" if ok_all else "有问题 ❌"))
sys.exit(0 if ok_all else 1)
