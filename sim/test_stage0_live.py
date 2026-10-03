"""阶段 0（审题）真实 API 端到端：确认改用独立界面后，对话链路仍然通。

不写正式库（DB 走 /tmp），不调真实模型以外的任何外部服务。
用法：cd socratic-writing-agent && /usr/bin/python3 sim/test_stage0_live.py
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
DB = "/tmp/stage0_live.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DB_PATH"] = DB
# 用 .env 里的真实 key（不覆盖）
sys.path.insert(0, BASE)

import app as A  # noqa: E402

TOPIC = "学以成人"
MATERIAL = ("《礼记·大学》云：“大学之道，在明明德。”又说“自天子以至于庶人，"
            "壹是皆以修身为本。”古人把“学”与“成人”连在一处说，"
            "但今天也有人认为，学只是获取知识，与做人是两回事。")

REPLIES = [
    "我觉得学就是读书、上课、考试，成人就是长大了、懂事了。",
    "成人……可能是变成一个有用的人吧，能养活自己，让家里放心。",
    "我好像说不太清。是不是说读书是为了学会做人，不只是为了考试？"
    "可是我不太确定，读书多的人也有做坏事的。",
]

client = A.app.test_client()
r = client.post("/start_prompt", data={"name": "实测甲", "topic": TOPIC,
                                       "material": MATERIAL, "idea": REPLIES[0]})
print("POST /start_prompt ->", r.status_code)
d = r.get_json()
for m in d.get("history", []):
    print(("  [AI] " if m["role"] == "assistant" else "  [我] ") + m["content"][:90])

print("\n—— 逐回合真实对话 ——")
for i, txt in enumerate(REPLIES, 1):
    print("\n[我·第%d次] %s" % (i, txt))
    rr = client.post("/chat", data={"message": txt})
    dd = rr.get_json()
    if rr.status_code != 200 or dd.get("error"):
        print("  出错：", rr.status_code, dd)
        break
    print("[AI] %s" % dd["reply"])
    print("      （策略=%s 收尾=%s 求助=%s）" % (dd.get("strategy") or "-",
                                              dd.get("wrapped"), dd.get("help") or "-"))
    if dd.get("wrapped"):
        print("\n→ 审题已收尾，前端此时会弹出「写审题笔记」")
        rr2 = client.post("/finish_prompt", data={
            "note": "核心概念是“学”和“成人”，学不只是读书求知，成人也不只是长大。"})
        print("→ POST /finish_prompt ->", rr2.status_code)
        html = client.get("/").get_data(as_text=True)
        print("→ 回到首页（STAGE0=false）：", "const STAGE0 = false" in html)
        break
else:
    print("\n（三轮未收尾——审题收尾偏晚，需关注）")

html = client.get("/").get_data(as_text=True)
print("\n页面检查：STAGE0 =", "const STAGE0 = true" in html)
