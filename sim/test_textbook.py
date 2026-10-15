# -*- coding: utf-8 -*-
"""教材索引：只给篇目线索、不给内容，且只在学生求助时才给。

背景（2026-10-15 许总）：在 09-29 的旧版本（f15d84b）基础上加回教材索引。
那个版本没有 10-03 的求助判定机制，所以触发时机改成轻量关键词判定——
不额外调 API，省 15 秒也不引入判定漂移。

红线是这一条：教材索引绝不能变成"AI 替学生回忆课文"。
索引里有素材点和名句，但**只注入篇目**，其余留在库里不送出去。

用法：/usr/bin/python3 sim/test_textbook.py
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ["DB_PATH"] = "/tmp/textbook_test.db"
if os.path.exists(os.environ["DB_PATH"]):
    os.remove(os.environ["DB_PATH"])
os.environ["ZHIPU_API_KEY"] = ""          # 单元部分不调模型

import app as A  # noqa: E402
import textbook  # noqa: E402

A.init_db()
checks = []


def ck(name, ok, extra=""):
    checks.append((name, ok))
    print(("  ok   " if ok else "  FAIL ") + name + (("  | " + extra) if extra else ""))


print("== A 索引本身 ==")
idx = textbook.load_textbook_index()
ck("索引能读到", len(idx) >= 20, "共 %d 条" % len(idx))
ck("每条都有篇目与册次", all(x.get("篇目") and x.get("册次") for x in idx))

print("\n== B 检索命中：该中的要中，不该中的一条都不给 ==")
h = textbook.search_textbook("学习要靠积累和坚持，不能三天打鱼两天晒网", top_n=3)
titles = "、".join(x["篇目"] for x in h)
ck("学习类说法能命中《劝学》", any("劝学" in x["篇目"] for x in h), titles)
h2 = textbook.search_textbook("青年要有责任和担当，把个人融入国家", top_n=3)
t2 = "、".join(x["篇目"] for x in h2)
ck("责任担当类能命中相关篇目", len(h2) > 0, t2)
# 教材里没有讲"兼听"的课文，检索不到是**正确行为**——
# 硬凑一条不相干的，学生只会觉得 AI 在胡编
h3 = textbook.search_textbook("兼听则明，要多听取不同的意见", top_n=3)
print("     （兼听类命中 %d 条：%s）" % (len(h3), "、".join(x["篇目"] for x in h3) or "无"))
h4 = textbook.search_textbook("ahsdkjfh 乱码测试", top_n=3)
ck("检索不到时不硬凑", len(h4) == 0,
   "、".join(x["篇目"] for x in h4) or "0 条")
# 单字撞车曾经能让任意两句话都"命中"，这里卡死：
# 这句话什么实质内容都没有，一条都不该命中
h5 = textbook.search_textbook("他说得也有道理，我觉得人还是要有点自己的想法", top_n=3)
ck("泛泛而谈一条都不命中", len(h5) == 0,
   "、".join(x["篇目"] for x in h5) or "0 条")

print("\n== C 红线：只给篇目，不给内容 ==")
hints = textbook.search_textbook("责任和担当，个人对国家", top_n=2)
block = textbook.format_textbook_hints(hints)
ck("线索块里有篇目", "《" in block)
ck("写明只提篇目", "只提篇目" in block)
ck("写明不许讲素材点/名句", "素材点" in block and "不许" in block)
# 素材点与名句绝不能出现在注入内容里
leak = [k for x in hints for k in ("素材点", "名句") if x.get(k) and x[k] in block]
ck("素材点与名句没有注入", not leak, str(leak)[:40])
# 完整索引里确实有内容字段（说明是"故意不注入"，不是数据缺失）
ck("内容字段确实存在于索引（只是不外送）",
   any(x.get("名句") for x in idx))

print("\n== D 触发时机：只有求助时才检索 ==")
for t in ("我想不出来。", "不知道。", "说不上来。", "我卡住了，帮帮我。"):
    ck("短句求助能识别：%s" % t, A.looks_like_help_request(t))
for t in ("因为每个人经验有限，多听能少犯错，比如做题时听听别人的解法。",
          "我觉得兼听就是要多听别人的意见，这样才能全面了解事情。"):
    ck("正经作答不算求助：%s" % t[:16], not A.looks_like_help_request(t))

print("\n== E 已经提过的篇目不再重复提 ==")
hist = [("assistant", "你读过的《劝学》里，有没有讲过跟这有关的？")]
got = A.pick_textbook_hints("学习要靠积累和坚持", history=hist, top_n=2)
ck("提过的《劝学》被剔除",
   not any("劝学" in x["篇目"] for x in got),
   "、".join(x["篇目"] for x in got) or "(无)")
got2 = A.pick_textbook_hints("学习要靠积累和坚持", history=None, top_n=2)
ck("没有历史时仍能给出", len(got2) > 0, "、".join(x["篇目"] for x in got2))

print("\n== F 注入机制：有线索才注入，没线索不硬塞 ==")
TB_HINTS = A.pick_textbook_hints("学习要靠积累和坚持，做事要专心", history=None, top_n=2)
ck("能取出教材线索", len(TB_HINTS) > 0, "、".join(x["篇目"] for x in TB_HINTS))
n_yes = A.build_turn_nudge(1, False, "我想不出来。", "", 2,
                           prev_questions=None, next_point="支撑薄弱",
                           textbook_hints=TB_HINTS)
ck("有线索时注入教材块", "【教材线索" in n_yes)
ck("注入位置在追问要求之前",
   n_yes.find("【教材线索") < n_yes.find("1. **追问的方向"))
n_empty = A.build_turn_nudge(1, False, "我想不出来。", "", 2,
                             prev_questions=None, next_point="支撑薄弱",
                             textbook_hints=[])
ck("没有线索时不硬塞（宁可不提，也别提错）", "【教材线索" not in n_empty)
n_no = A.build_turn_nudge(1, False, "因为多听能少犯错。", "", 2,
                          prev_questions=None, next_point="支撑薄弱")
ck("没带线索时正常追问不受影响", "【教材线索" not in n_no
   and "1. **追问的方向" in n_no)
# 收尾回合不该再给教材线索（那会儿不该再抛新材料）
n_wrap = A.build_turn_nudge(1, False, "我想不出来。", "now", 2,
                            prev_questions=None, next_point="",
                            textbook_hints=A.pick_textbook_hints("兼听则明"))
ck("收尾回合不注入", "【教材线索" not in n_wrap)

print("\n== H 求助回合：整段换成求助结构 ==")
n_help = A.build_turn_nudge(1, False, "我想不出来。", "", 2,
                            prev_questions=None, next_point="支撑薄弱",
                            textbook_hints=TB_HINTS, help_mode=True)
ck("求助时走求助结构", "他卡住了" in n_help)
ck("教材线索改成强措辞（写成可选用它就不选）",
   "就用它给他一个抓手" in n_help and "可选用" not in n_help)
ck("要求只给一个问句", "只能有一个问句" in n_help)
ck("不再走普通追问结构", "1. **追问的方向" not in n_help)
n_help2 = A.build_turn_nudge(1, False, "我想不出来。", "", 2,
                             next_point="", textbook_hints=[], help_mode=True)
ck("没有教材线索时，抓手要求照样在", "给一个具体抓手" in n_help2)
ck("红线：不许讲课文内容", "不许讲课文内容" in n_help)
ck("红线：不许只有安抚", "不许只有安抚" in n_help)
ck("红线：不许替他概括课文内容",
   "也不许替他概括" in n_help and "是不是也强调了持之以恒" in n_help)
n_help3 = A.build_turn_nudge(1, False, "因为多听能少犯错。", "", 2,
                             next_point="支撑薄弱", textbook_hints=TB_HINTS,
                             help_mode=False)
ck("不求助时不走求助结构", "他卡住了" not in n_help3)

print("\n== I 检索串只用题目那一行，不把整道题干喂进去 ==")
FULL = ("题目：兼听则明\n材料：有人说，听取不同意见，会让我们明辨是非、智慧通达。"
        "也有人说，大数据时代每天都有各种“声音”涌来，容易迷失自我。\n"
        "要求：以“兼听则明”为题，写一篇议论文。")
ck("题目行抽得出来", A.topic_head(FULL).startswith("题目：兼听则明"),
   A.topic_head(FULL))
ck("没有「题目：」字样时按前 24 字退化", A.topic_head("兼听则明，偏信则暗") != "")
noise = textbook.search_textbook(FULL, top_n=3)
clean = textbook.search_textbook(A.topic_head(FULL), top_n=3)
# 整道题干里的"时代""社会"会撞出不相干的篇目（实测撞出《以工匠精神雕琢时代品质》）
ck("整道题干会撞出噪声，题目行不会",
   len(noise) > len(clean),
   "全文命中 %d 条：%s｜题目行命中 %d 条" % (
       len(noise), "、".join(x["篇目"] for x in noise) or "无", len(clean)))

print("\n== G 端到端走一遍路由（demo 模式，验不报错）==")
ESSAY = "兼听则明，偏信则暗。古人早已告诉我们，只听一面之词容易偏颇。" * 6
c = A.app.test_client()
r = c.post("/start", data={"name": "教材测试", "essay": ESSAY,
                           "topic": "题目：兼听则明\n材料：听取不同意见，明辨是非。"})
ck("提交成功", bool(r.get_json().get("ok")), str(r.get_json().get("error", "")))
with c.session_transaction() as sess:
    st = {"student_id": sess["student_id"], "conv_id": sess["conv_id"]}
for msg in ["因为多听能少犯错。", "我想不出来。"]:
    cc = A.app.test_client()
    with cc.session_transaction() as sess:
        sess["student_id"] = st["student_id"]
        sess["conv_id"] = st["conv_id"]
    rr = cc.post("/chat", data={"message": msg})
    dd = rr.get_json()
    ck("对话能走：%s" % msg, bool(dd.get("reply")), str(dd.get("error", ""))[:40])

bad = [n for n, ok in checks if not ok]
print("\n共 %d 项，通过 %d，失败 %d" % (len(checks), len(checks) - len(bad), len(bad)))
sys.exit(1 if bad else 0)
