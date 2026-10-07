#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动跑完整组对话：按预设的学生回答一路追问到收尾。

可断点续跑：从数据库里已有的学生作答条数接着往下发，被中断了再跑一次即可。
用法：python3 sim/auto.py
"""
import os
import sys
import json

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM_DIR = os.path.join(BASE, "sim")
os.environ.setdefault("DB_PATH", os.path.join(SIM_DIR, "sim_conversations.db"))
sys.path.insert(0, BASE)
os.chdir(BASE)

from app import app, get_db  # noqa: E402

STATE_PATH = os.path.join(SIM_DIR, "sim_state.json")
LOG_PATH = os.path.join(SIM_DIR, "sim_log.jsonl")

# 模拟学生的回答（贴合这篇"学以成人"，覆盖：含糊→举例→给机制→想反方→自我修正）
ANSWERS = [
    "就是他们把为国家做事当成了绝对正确，把爱国变成了无条件服从命令吧。",
    "比如如果命令是让自己去伤害无辜的人，那就不该服从，这种时候服从命令就是在作恶。",
    "我作文里想说的其实就一条：一个人如果没有“不能伤害人”这条底线，他的知识越强就越危险。",
    "因为本事是工具，工具往哪边用，得看用它的人心里有没有底线。",
    "也许会有人说，那照你讲的，是不是品德好就行、知识多少不重要？我不是这个意思，我是说方向比大小更要紧。",
    "如果重写，我会在第二段补一句，说明我举的是反例——他们不是学得不够，而是学得够好但方向错了。",
    "我原来没想到还有人会从“没有底线的人也可能做出好事”这个角度来讲，那我的说法确实说满了。",
    "这么聊下来我明白了，我那个“地基”的比喻其实是想说方向问题，但我在作文里没把这个“为什么”写出来。",
]


def log(entry):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_answers():
    """学生应答序列：默认用内置的（贴合"学以成人"那篇）。

    换作文时必须换应答——否则学生答的内容和作文无关，跑出来的追问看着正常，
    其实是我们预设的答法把它引到了别处。
    用法：python3 sim/auto.py --answers sim/cases/xx_answers.txt（一行一条）
    """
    path = None
    for i, a in enumerate(sys.argv):
        if a == "--answers" and i + 1 < len(sys.argv):
            path = sys.argv[i + 1]
            del sys.argv[i:i + 2]
            break
        if a.startswith("--answers="):
            path = a.split("=", 1)[1]
            del sys.argv[i]
            break
    if not path:
        return ANSWERS
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]


def main():
    answers = load_answers()
    with open(STATE_PATH, encoding="utf-8") as f:
        st = json.load(f)
    conn = get_db()
    used = conn.execute("SELECT COUNT(*) c FROM messages WHERE conversation_id=? AND role='user'",
                        (st["conv_id"],)).fetchone()["c"]
    conn.close()
    print("已有学生作答 %d 条，从第 %d 条继续（应答共 %d 条）"
          % (used, used + 1, len(answers)), flush=True)

    for i in range(used, len(answers)):
        ans = answers[i]
        c = app.test_client()
        with c.session_transaction() as sess:
            sess["student_id"] = st["student_id"]
            sess["student_name"] = st.get("student_name", "")
            sess["conv_id"] = st["conv_id"]
        r = c.post("/chat", data={"message": ans})
        data = r.get_json()
        if not data or data.get("error"):
            print("FAILED:", r.status_code, data, flush=True)
            return
        log({"role": "user", "content": ans})
        log({"role": "assistant", "content": data["reply"],
             "strategy": data.get("strategy"), "wrapped": data.get("wrapped")})
        print("\n[学生·第%d次] %s" % (i + 1, ans), flush=True)
        print("\n[AI] %s" % data["reply"], flush=True)
        print("\n-- 策略：%s ｜ 收尾：%s --" % (data.get("strategy"), data.get("wrapped")),
              flush=True)
        if data.get("wrapped"):
            print("\n=== 本组对话已收尾 ===", flush=True)
            return
    print("\n=== 预设回答用尽，仍未收尾 ===", flush=True)


if __name__ == "__main__":
    main()
