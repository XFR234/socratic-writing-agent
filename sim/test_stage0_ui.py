"""阶段 0（审题）界面链路离线自测：不调模型、不碰线上库。

覆盖许总 10-03 报的两个问题：
  1) 审题没有自己的对话框（内联在首页表单里）
  2) 对话与输入框位置错乱（输入框被挂在消息流中间）

用法：cd socratic-writing-agent && /usr/bin/python3 sim/test_stage0_ui.py
"""
import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
DB = "/tmp/stage0_ui_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DB_PATH"] = DB
os.environ["ZHIPU_API_KEY"] = ""

import app as A  # noqa: E402

client = A.app.test_client()
fails = []


def check(name, ok):
    print(("  ok   " if ok else "  FAIL ") + name)
    if not ok:
        fails.append(name)


def page():
    return client.get("/").get_data(as_text=True)


print("A. 点「先做审题」→ 应进入独立的审题界面")
r = client.post("/start_prompt", data={
    "name": "测试甲", "topic": "学以成人", "material": "材料：古人云……", "idea": "我觉得是读书"})
check("POST /start_prompt 返回 200", r.status_code == 200)
html = page()
check("顶部徽标是「审题 · 概念辨析」", "审题 · 概念辨析" in html)
check("不再同时显示写作轮徽标", "第1轮 ·" not in html)
check("前端知道自己在审题这屏（STAGE0=true）", "const STAGE0 = true" in html)
check("首页表单已整块隐藏", 'id="startBox" class="card form hidden"' in html)
check("对话卡片显示出来", 'id="chatBox" class="card chat-card ' in html)
check("有审题专属的说明卡", "审题这一步在做什么" in html)
check("旧的表单内嵌对话容器已删除", 'id="p_chat"' not in html)
check("输入框有独立 id，便于收尾时隐藏", 'id="inputRow"' in html)
check("输入框提示语已换成审题版", "说说你怎么理解这个概念" in html)

print("B. 刷新页面 → 审题对话不能丢")
html2 = page()
check("刷新后仍在审题界面", "const STAGE0 = true" in html2)
check("刷新后历史消息还在", "咱们先不急" in html2 or "材料里" in html2)

print("C. AI 收尾（策略记「回顾看」）→ 应弹审题笔记")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
conv = conn.execute("SELECT id FROM conversations WHERE round=0 ORDER BY id DESC LIMIT 1").fetchone()
conn.execute("INSERT INTO messages(conversation_id, role, content, strategy, created_at) "
             "VALUES(?,?,?,?,?)",
             (conv["id"], "assistant", "我们聊得差不多了，你把这几点写成三行笔记。", "回顾看", "2026-10-03 18:00"))
conn.commit()
conn.close()
html3 = page()
check("服务端告知「已聊完」（WRAPPED=true）", "const WRAPPED = true" in html3)
check("仍是审题界面", "const STAGE0 = true" in html3)

print("D. 写审题笔记 → 回正常首页（进入写作轮）")
r = client.post("/finish_prompt", data={"note": "核心概念是学与成人，学不只是读书求知。"})
check("POST /finish_prompt 返回 200", r.status_code == 200)
html4 = page()
check("已退出审题界面", "const STAGE0 = false" in html4)
check("回到首页表单（要交本轮作文）", 'id="startBox" class="card form "' in html4)
check("首页必填说明还在", "的为必填项" in html4)

print("E. 再次主动点「先做审题」→ 应能回到那条审题记录继续")
r = client.post("/start_prompt", data={
    "name": "测试甲", "topic": "学以成人", "material": "材料：古人云……", "idea": ""})
check("POST /start_prompt 返回 200", r.status_code == 200)
html5 = page()
check("又回到审题界面", "const STAGE0 = true" in html5)

print()
if fails:
    print("未通过 %d 项：" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("全部通过")
