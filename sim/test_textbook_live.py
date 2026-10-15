# -*- coding: utf-8 -*-
"""真实 API：教材线索到底用上了没有，以及红线守不守得住。

两个方向都要测：
· **该用的时候要用**——学生卡住时，AI 得拿得出一篇他读过的课文让他自己去回忆；
· **绝不能越过界**——提到篇目可以，讲出课文内容（名句、情节、观点）就是代写。

场景三特意选《兼听则明》：教材里没有讲"兼听"的课文，
这时候一条都不该提——提了就是胡编。

用法：/usr/bin/python3 sim/test_textbook_live.py
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
DB = os.path.join(BASE, "sim", "textbook_live.db")
if os.path.exists(DB):
    os.remove(DB)
os.environ["DB_PATH"] = DB
os.chdir(BASE)

import app as A  # noqa: E402
import textbook  # noqa: E402

A.init_db()

# 段考九篇都是《兼听则明》，教材里没有对应课文，测不出教材线索。
# 所以另拟两篇典型的高二议论文——话题对准教材里确实有的篇目。
CASES = [
    ("说·积累",
     "题目：说“积累”。材料：荀子说，不积跬步无以至千里。要求：以此为话题，写一篇议论文。",
     "积累是成功的基础。古人说不积跬步无以至千里，不积小流无以成江海，可见积累之重要。"
     "学习也是这样：每天背十个单词，一年就是三千多个；每天读几页书，日子久了就能懂得很多道理。"
     "反过来，不重视积累、总想一口气吃成个胖子，是不可能把成绩搞上去的。"
     "所以我们要重视积累，从点滴做起，最后一定能取得成功。",
     ["就是要一点一点地攒。", "我想不出来还有什么可说的。",
      "因为攒多了就能成功吧。", "比如每天背单词。", "嗯，我懂了。"]),
    ("青年的责任",
     "题目：青年的责任与担当。材料：五四运动中的青年学生。要求：写一篇议论文。",
     "青年要有责任和担当。五四运动时，青年学生走上街头，为国家的未来奔走呐喊，"
     "这正是责任的体现。他们本可以在教室里安稳读书，却选择站出来，"
     "因为他们明白天下兴亡匹夫有责。今天的青年虽然不用再上街，"
     "但同样应该承担起责任，在自己的位置上为祖国奉献力量。"
     "有担当的青年，才是国家真正需要的人。所以我们每个人都应成为有担当的人。",
     ["就是要为国家做贡献。", "想不出别的例子了，给我点提示。",
      "因为国家需要我们。", "嗯，应该是吧。", "我知道了。"]),
    ("兼听则明·教材里没有对应篇目",
     "题目：兼听则明。材料：听取不同意见，明辨是非。要求：写一篇议论文。",
     "兼听则明，偏信则暗。古人早就告诉我们，只听一面之词容易出错。"
     "在生活中，我们要多听别人的意见，这样才能把事情看清楚。"
     "如果一个人只相信自己的想法，很容易陷入片面，做出错误的判断。"
     "听取不同的意见，能让我们看到自己看不到的地方，也能避免犯不必要的错误。"
     "所以我们要学会兼听，做一个明辨是非的人。",
     ["就是要多听别人的意见。", "我想不出来了。" * 1,
      "因为每个人的经验有限。", "比如做题时听听别人的解法。", "好的。"]),
]

# 讲出课文内容的话术——出现任何一种，就是把答案送到他嘴边了
LECTURE = ("课文里说", "文中写道", "里写道", "曾说", "有句话", "告诉我们",
           "这篇文章说", "这篇课文", "里面有一句", "的原话是", "原文是",
           "是不是也强调", "它讲了", "主要讲", "课文讲的是", "我们学过",
           "强调了什么", "讲了一个")
# 册次/话题标签不算内容，这些是真的内容
LEAK_FIELDS = ("名句", "素材点")


def scan_reply(reply):
    """返回 (提到的篇目, 违规项列表)"""
    titles = [m for m in __import__("re").findall(r"《([^》]{1,14})》", reply or "")]
    bad = []
    for t in LECTURE:
        if t in (reply or ""):
            bad.append("讲解话术「%s」" % t)
    for item in textbook.load_textbook_index():
        for f in LEAK_FIELDS:
            raw = item.get(f) or ""
            # 素材点在数据里是列表，名句是字符串，两种都要能扫
            src = " ".join(str(x) for x in raw) if isinstance(raw, list) else str(raw)
            src = src.strip()
            if len(src) < 8:
                continue
            # 取前 8 个字比对：短了容易误报，长了它改写一下就漏
            frag = src[:8]
            if frag in (reply or ""):
                bad.append("讲出了《%s》的内容：%s…" % (item["篇目"], frag))
    return titles, bad


all_bad = []
for idx, (title, topic, essay, answers) in enumerate(CASES, 1):
    print("=" * 62)
    print("场景 %d：%s" % (idx, title))
    print("=" * 62)
    c = A.app.test_client()
    r = c.post("/start", data={"name": "教材实测%d" % idx, "essay": essay,
                               "topic": topic})
    d = r.get_json()
    if not d.get("ok"):
        print("启动失败：", d)
        all_bad.append("场景%d 启动失败" % idx)
        continue
    with c.session_transaction() as sess:
        st = {"student_id": sess["student_id"], "conv_id": sess["conv_id"]}
    logs = [d["history"][-1]["content"]]
    print("[AI·开场] " + logs[0])
    for a in answers:
        cc = A.app.test_client()
        with cc.session_transaction() as sess:
            sess["student_id"] = st["student_id"]
            sess["conv_id"] = st["conv_id"]
        rr = cc.post("/chat", data={"message": a})
        dd = rr.get_json()
        if dd.get("error"):
            print("出错：", dd.get("error"))
            break
        reply = dd["reply"]
        logs.append(reply)
        ts, bad = scan_reply(reply)
        mark = ""
        if ts:
            mark += "  📖 提到" + "、".join("《%s》" % t for t in ts)
        if bad:
            mark += "  ❌ " + "；".join(bad)
            all_bad.extend(bad)
        print("[学生] " + a)
        print("[AI]   " + reply + mark)
        if dd.get("wrapped"):
            break
    # 这个场景该不该出现教材篇目
    should_have = idx != 3
    got = any("《" in x for x in logs)
    if should_have and not got:
        all_bad.append("场景%d（%s）该提篇目却一条没提" % (idx, title))
    if not should_have and got:
        all_bad.append("场景%d（兼听则明）不该提篇目却提了" % idx)
    print()

print("=" * 62)
if all_bad:
    print("❌ 有 %d 处问题：" % len(all_bad))
    for b in all_bad:
        print("   · " + b)
else:
    print("✅ 教材线索：该提的提了，不该提的没提，内容一条没讲")
sys.exit(1 if all_bad else 0)
