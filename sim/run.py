#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地模拟：用真实 API 跑一遍学生端完整对话。

走的是真实路由（/start、/chat），所以收尾判定、策略判定、跨轮次注入
全都与线上一致；只是把数据库换成本地隔离文件，不污染线上数据。

用法：
  python3 sim/run.py start <作文文件路径>
  python3 sim/run.py reply "<学生的回答>"
  python3 sim/run.py dump            # 打印本组完整对话（含策略标注）
  python3 sim/run.py reset           # 清空模拟库，重新开始
"""
import os
import sys
import json

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM_DIR = os.path.join(BASE, "sim")
os.environ.setdefault("DB_PATH", os.path.join(SIM_DIR, "sim_conversations.db"))
sys.path.insert(0, BASE)
os.chdir(BASE)

from app import app, get_db, init_db  # noqa: E402

init_db()
STATE_PATH = os.path.join(SIM_DIR, "sim_state.json")
LOG_PATH = os.path.join(SIM_DIR, "sim_log.jsonl")


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(st):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def log(entry):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# 默认题面：题目与材料自 2026-10-03 起必填。跑真实学生作文时**必须换成那一篇
# 真题的题面**——AI 要靠它判断关键词有没有被读窄，拿别的题目糊上去等于测了个假的。
# 换法二选一：
#   ① 命令行：python3 sim/run.py start sim/cases/xx.txt --topic "题目" --material "材料"
#   ② 文件头三行（推荐，题面跟着作文走，不会发错）：
#        题目：……
#        材料：……
#        （空一行，下面是作文正文）
SIM_TOPIC = "学以成人"
SIM_MATERIAL = ("材料：一个人从出生到成人，成长中离不开学习。"
                "有人以为学知识就够了，其实还要学做人、学会分辨是非。"
                "学了却做错，知识越多错得越远。")


def parse_case(path, topic=None, material=None):
    """从作文文件里取出题目／材料／正文。

    命令行传的 topic/material 优先；否则看文件头有没有「题目：」「材料：」；
    都没有就用默认题面，并明确提示（免得跑完了才发现题面是错的）。
    """
    raw = open(path, encoding="utf-8").read().strip()
    file_topic, file_material = None, None
    body, in_head = [], True
    for ln in raw.split("\n"):
        s = ln.strip()
        if in_head and (s.startswith("题目：") or s.startswith("题目:")):
            file_topic = s.split("：", 1)[-1].split(":", 1)[-1].strip()
            continue
        if in_head and (s.startswith("材料：") or s.startswith("材料:")):
            file_material = s.split("：", 1)[-1].split(":", 1)[-1].strip()
            continue
        if s == "" and in_head and not body:
            continue        # 头部与正文之间的空行
        in_head = False
        body.append(ln)
    essay = "\n".join(body).strip()
    t = topic or file_topic or SIM_TOPIC
    m = material or file_material or SIM_MATERIAL
    if not (topic or file_topic):
        print("[提示] 没拿到题目，用的是默认题面「%s」——跑真实作文请带上题面。" % t)
    return t, m, essay


def make_client(st):
    c = app.test_client()
    if st.get("student_id"):
        with c.session_transaction() as sess:
            sess["student_id"] = st["student_id"]
            sess["student_name"] = st.get("student_name", "")
            sess["conv_id"] = st["conv_id"]
    return c


def do_start(essay_path, topic=None, material=None):
    topic, material, essay = parse_case(essay_path, topic, material)
    if len(essay) < 100:
        print("作文太短（%d 字），后端要求不少于 100 字。" % len(essay))
        return
    name = "模拟学生A"
    c = app.test_client()
    # 题目与材料必填（2026-10-03 起），这里跟着填一份固定的题面
    r = c.post("/start", data={"name": name, "sid": "SIM01", "essay": essay,
                              "topic": topic, "material": material})
    data = r.get_json()
    if not data or not data.get("ok"):
        print("START FAILED:", r.status_code, data)
        return
    st = {"student_id": None, "conv_id": None, "student_name": name}
    # 取会话里的 student_id / conv_id
    with c.session_transaction() as sess:
        st["student_id"] = sess.get("student_id")
        st["conv_id"] = sess.get("conv_id")
    save_state(st)
    log({"role": "essay", "content": essay})
    log({"role": "case", "content": "题目：%s\n材料：%s" % (topic, material)})
    opening = data["history"][-1]["content"] if data.get("history") else ""
    log({"role": "assistant", "content": opening,
         "strategy": data["history"][-1].get("strategy") if data.get("history") else ""})
    print("== 轮次 %s / 阶段 %s ==" % (data.get("round"), data.get("stage")))
    print("题目：%s｜材料：%s" % (topic, material[:40] + ("…" if len(material) > 40 else "")))
    print("[AI 开场]\n" + opening)


def do_reply(text):
    st = load_state()
    if not st.get("conv_id"):
        print("尚未 start")
        return
    c = make_client(st)
    r = c.post("/chat", data={"message": text})
    data = r.get_json()
    if not data or data.get("error"):
        print("CHAT FAILED:", r.status_code, data)
        return
    log({"role": "user", "content": text})
    log({"role": "assistant", "content": data["reply"], "strategy": data.get("strategy"),
         "wrapped": data.get("wrapped")})
    print("[我（学生）] " + text)
    print("\n[AI] " + data["reply"])
    print("\n-- 策略：%s ｜ 收尾：%s --" % (data.get("strategy"), data.get("wrapped")))


def do_dump():
    st = load_state()
    conn = get_db()
    rows = conn.execute(
        "SELECT role, content, strategy FROM messages WHERE conversation_id=? ORDER BY id",
        (st.get("conv_id"),)).fetchall()
    conn.close()
    for r in rows:
        tag = "AI" if r["role"] == "assistant" else "学生"
        mark = ("  【%s】" % r["strategy"]) if r["strategy"] else ""
        print("%s%s：%s" % (tag, mark, r["content"]))
        print("-" * 60)


def do_reset():
    for p in (STATE_PATH, LOG_PATH):
        if os.path.exists(p):
            os.remove(p)
    dbp = os.environ["DB_PATH"]
    if os.path.exists(dbp):
        os.remove(dbp)
    print("已重置（删掉状态、日志与模拟库）")


def take_flag(name):
    """取出 --topic "xxx" / --material "xxx" 这类可选参数（取走后从 argv 里删掉）。"""
    for i, a in enumerate(sys.argv):
        if a == "--" + name and i + 1 < len(sys.argv):
            v = sys.argv[i + 1]
            del sys.argv[i:i + 2]
            return v
        if a.startswith("--%s=" % name):
            v = a.split("=", 1)[1]
            del sys.argv[i]
            return v
    return None


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "start":
        do_start(sys.argv[2], take_flag("topic"), take_flag("material"))
    elif cmd == "reply":
        do_reply(sys.argv[2])
    elif cmd == "dump":
        do_dump()
    elif cmd == "reset":
        do_reset()
    else:
        print(__doc__)
