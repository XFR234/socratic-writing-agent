# -*- coding: utf-8 -*-
"""换题重开：同一账号在同一轮次里能不能换一篇作文。

背景（许总 2026-10-11 反馈）："同一个账号里只能测试一个作文题目，再发另一个就宕机了"。
真实原因不是崩，是后端**静默复用**旧对话、把新提交的作文悄悄丢掉——
页面于是跳回上一篇的对话，AI 还在追上一篇，看起来就像卡住/宕机。

本用例覆盖：
  A 首次提交 → 建对话
  B 换题换文再提交 → 必须返回 need_switch，且不新建对话（等用户确认）
  C 确认重开（new_conv=1）→ 新建第二条，作文是新交的那篇
  D 刷新首页 → 进的是第二条
  E 重复提交同一篇（正式场景）→ 静默复用，不打扰
  F 上一条对话仍可回看（往期对话）
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
DB = "/tmp/switch_topic_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DB_PATH"] = DB
os.environ["ZHIPU_API_KEY"] = ""          # demo 模式，不调模型，快

import app as A  # noqa: E402

NAME = "许老师自测"
E1 = "兼听则明。古人云兼听则明偏信则暗。我们要广泛听取意见才能看清全貌。" * 6
E2 = "学以成人。古人云学然后知不足。我以为学习让人成长为一个真正的人。" * 6

checks = []


def ck(name, ok, extra=""):
    checks.append((name, ok))
    print(("  ok   " if ok else "  FAIL ") + name + (("  " + extra) if extra else ""))


def post(essay, topic, material, new_conv=False):
    data = {"name": NAME, "sid": "TEST", "essay": essay,
            "topic": topic, "material": material}
    if new_conv:
        data["new_conv"] = "1"
    r = A.app.test_client().post("/start", data=data)
    return r.get_json()


print("== A 首次提交《兼听则明》==")
d1 = post(E1, "兼听则明", "材料一：听取不同意见……")
ck("返回 ok、有对话历史", bool(d1.get("ok")) and bool(d1.get("history")))
ck("没有触发换题提示", not d1.get("need_switch"))

print("\n== B 换题换文再提交《学以成人》==")
d2 = post(E2, "学以成人", "材料二：一个人从出生到成人……")
ck("返回 need_switch", bool(d2.get("need_switch")))
ck("带回了上一篇的题目", d2.get("old_topic") == "兼听则明", str(d2.get("old_topic")))
ck("带回了这一次的新题目", d2.get("new_topic") == "学以成人", str(d2.get("new_topic")))
conn = A.get_db()
n = conn.execute("SELECT COUNT(*) c FROM conversations").fetchone()["c"]
conn.close()
ck("此时仍只有 1 条对话（未经确认不新建）", n == 1, "实际 %d 条" % n)

print("\n== C 确认重开（new_conv=1）==")
d3 = post(E2, "学以成人", "材料二：一个人从出生到成人……", new_conv=True)
ck("返回 ok", bool(d3.get("ok")))
conn = A.get_db()
rows = conn.execute(
    "SELECT id, topic, essay FROM conversations ORDER BY id").fetchall()
conn.close()
ck("库里变成 2 条对话", len(rows) == 2, "实际 %d 条" % len(rows))
ck("第 2 条的题目是新题", rows[1]["topic"] == "学以成人", rows[1]["topic"])
ck("第 2 条的作文是新交的那篇", A._norm_essay(rows[1]["essay"]) == A._norm_essay(E2))
ck("第 1 条仍保留（可回看）",
   rows[0]["topic"] == "兼听则明" and A._norm_essay(rows[0]["essay"]) == A._norm_essay(E1))

print("\n== D 刷新首页 → 应进第二条 ==")
c = A.app.test_client()
c.post("/start", data={"name": NAME, "sid": "TEST", "essay": E1,
                       "topic": "兼听则明", "material": "材料一"})
html = c.get("/").get_data(as_text=True)
ck("首页展示的是新题那篇", "学以成人" in html)

print("\n== E 重复提交同一篇（正式场景，不该打扰）==")
d5 = post(E2, "学以成人", "材料二：一个人从出生到成人……")
ck("不弹换题提示", not d5.get("need_switch"))
ck("复用旧对话（历史一致）", d5.get("history") == d3.get("history"))

print("\n== F 换题但没交作文 → 明确报错，不静默 ==")
r = A.app.test_client().post("/start", data={
    "name": NAME, "sid": "TEST", "essay": "", "topic": "新题目",
    "material": "材料", "new_conv": "1"})
d6 = r.get_json()
ck("返回 400 + 说明要重新粘作文", r.status_code == 400 and "整篇粘进来" in (d6.get("error") or ""),
   (d6.get("error") or "")[:40])

print("\n== G 审题入口：换一道题重审 ==")
# 审题是另一条入口（round=0），同样的问题同样要修：
# 阶段 0 原先"每人只建一次"，换题再点「开始审题」会把新题目悄悄丢掉。
cl = A.app.test_client()


def post_prompt(topic, material, new_conv=False):
    data = {"name": "审题自测", "topic": topic, "material": material, "idea": ""}
    if new_conv:
        data["new_conv"] = "1"
    r = cl.post("/start_prompt", data=data)
    return r.get_json()


g1 = post_prompt("兼听则明", "材料一：听取不同意见……")
ck("审题首次提交 ok", bool(g1.get("ok")) and g1.get("stage") == 0)
g2 = post_prompt("学以成人", "材料二：一个人从出生到成人……")
ck("换题后返回 need_switch", bool(g2.get("need_switch")))
ck("带回了上一道题的题目", g2.get("old_topic") == "兼听则明", str(g2.get("old_topic")))
conn = A.get_db()
n0 = conn.execute("SELECT COUNT(*) c FROM conversations WHERE round=0").fetchone()["c"]
conn.close()
ck("未确认时不新建审题对话", n0 == 1, "实际 %d 条" % n0)
g3 = post_prompt("学以成人", "材料二：一个人从出生到成人……", new_conv=True)
ck("确认重审返回 ok", bool(g3.get("ok")))
conn = A.get_db()
rows0 = conn.execute(
    "SELECT id, topic FROM conversations WHERE round=0 ORDER BY id").fetchall()
conn.close()
ck("审题变成 2 条", len(rows0) == 2, "实际 %d 条" % len(rows0))
ck("第 2 条是新题", rows0[1]["topic"] == "学以成人", rows0[1]["topic"])
html2 = cl.get("/").get_data(as_text=True)
ck("刷新后停在新的审题那屏", "const STAGE0 = true" in html2 and "学以成人" in html2)
g4 = post_prompt("学以成人", "材料二：一个人从出生到成人……")
ck("同题再审不打扰", not g4.get("need_switch"))

print("\n" + "-" * 60)
bad = [n for n, ok in checks if not ok]
print("共 %d 项，通过 %d，失败 %d" % (len(checks), len(checks) - len(bad), len(bad)))
if bad:
    for n in bad:
        print("  失败：" + n)
    sys.exit(1)
print("全部通过")
