"""审题对话质量实测（真实 API）：复现许总 10-03 截图里的两轮。

要验三件事：
  1) 开场是不是直接问"哪个词需要界定"（不再问"材料里有什么矛盾"）
  2) 学生说"什么叫 X"时，AI **绝不解释**（只能换个大白话的小问题重问）
  3) 学生方向答对时能收尾；整程不出现安慰句、不提"你作文里"

用法：cd socratic-writing-agent && /usr/bin/python3 sim/test_stage0_quality.py
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
DB = "/tmp/stage0_quality.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DB_PATH"] = DB

import app as A  # noqa: E402

TOPIC = "学以成人"
MATERIAL = ("《礼记·大学》云：“大学之道，在明明德。”又说“自天子以至于庶人，壹是皆以修身为本。”"
            "儒家强调修身，认为通过学习可以提高人的品行；老庄则主张人应追求精神自由，不受外物束缚；"
            "墨家强调兼爱，希望人们互相帮助。今人也有不同看法：有人认为学只是获取知识，与做人是两回事。")

# 模拟真实学生：先没听懂，再慢慢说对
REPLIES = [
    "什么叫需要界定的词？",                       # ← 截图轮1 同类：没听懂 AI 的话
    "我觉得是“学”吧，学就是上学、读书、学知识。",   # 方向对但单薄
    "“成人”应该是说成为一个真正的人，不只是年龄到了。",  # 方向对
    "所以学不只是学知识，还得学会怎么做一个对别人有用的人。",  # 更明确
]

# 出现这些说明违反了红线
BAD_EXPLAIN = ("指的是", "就是指", "意思是", "所谓", "也就是说", "就是说")
BAD_SOOTHE = ("挺难", "很正常", "慢慢来", "别急", "不容易", "有难度")
BAD_ESSAY_REF = ("你作文里", "你写过的", "你之前写过的作文", "你的作文")

client = A.app.test_client()
r = client.post("/start_prompt", data={"name": "实测乙", "topic": TOPIC,
                                       "material": MATERIAL, "idea": ""})
d = r.get_json()
opening = (d.get("history") or [{}])[0].get("content", "")
print("【开场】" + opening)
print("  开场问的是概念？", "是" if ("界定" in opening or "概念" in opening) else "否 ← 要改")
print("  开场有没有绕到材料矛盾？", "有 ← 要改" if "矛盾" in opening or "不满意" in opening else "没有")

violations = []
for i, txt in enumerate(REPLIES, 1):
    print("\n[学生·第%d次] %s" % (i, txt))
    rr = client.post("/chat", data={"message": txt})
    dd = rr.get_json()
    if rr.status_code != 200 or dd.get("error"):
        print("  出错：", rr.status_code, dd)
        break
    reply = dd["reply"]
    print("[AI] " + reply)
    print("      （求助=%s 收尾=%s）" % (dd.get("help") or "-", dd.get("wrapped")))
    for k in BAD_EXPLAIN:
        if k in reply:
            violations.append("第%d轮解释了概念：「%s」" % (i, k))
    for k in BAD_SOOTHE:
        if k in reply:
            violations.append("第%d轮出现了安慰：「%s」" % (i, k))
    for k in BAD_ESSAY_REF:
        if k in reply:
            violations.append("第%d轮把作文搬出来了（他还没写）：「%s」" % (i, k))
    if dd.get("wrapped"):
        print("\n→ 判定收尾。学生答的是方向性内容，符合许总「方向对就行」的要求。")
        break
else:
    print("\n→ 四轮仍未收尾（许总要求：方向对就该收）")

print("\n================ 违规检查 ================")
if violations:
    for v in violations:
        print("  ✗ " + v)
else:
    print("  未发现：不解释、不安慰、不提作文 —— 三条都守住")
