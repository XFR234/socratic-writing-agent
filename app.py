# -*- coding: utf-8 -*-
"""苏格拉底式对话支架 · 网页版智能体 (Flask)
学生端：输入姓名/学号 -> 进入对话（阶段一虚拟论敌 / 阶段二策略卡）
教师端：后台按学生、按日期查看全部对话，并一键导出 CSV
模型：智谱 GLM-4-Flash（永久免费），无 API key 时自动进入 demo 模式。

严格遵循开题报告：四轮行动研究，轮次(1-4)由教师后台设定，
第1-2轮=阶段一（虚拟论敌，AI主动追问），第3-4轮=阶段二（逻辑顾问，学生用策略卡主动追问）。
"""
import os
import re
from dotenv import load_dotenv

import sqlite3
import csv
import io
import json
import urllib.request
from datetime import datetime

from flask import (
    Flask, request, session, render_template, redirect,
    url_for, jsonify, Response,
)

from prompts import (
    STRATEGY_INFO, STRATEGY_ORDER, STAGE2_SYSTEM, STAGE1_OPENING_NO_ESSAY,
    STAGE2_OPENING, build_stage1_opening, build_stage2_user_prefix,
    build_stage1_system, ROUND_STAGE, TOULMIN_MAP,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DB_PATH 可经环境变量覆盖（如需挂载持久磁盘）；默认项目内 SQLite 文件
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "conversations.db"))
# 用绝对路径加载 .env，避免 Flask debug reloader 子进程因 cwd 变化而读不到配置
load_dotenv(os.path.join(BASE_DIR, ".env"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY", "")
ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL_NAME = os.environ.get("MODEL_NAME", "glm-4-flash")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "socratic-dev-secret-change-me")


# ---------------- 数据库 ----------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        sid TEXT,
        created_at TEXT
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER,
        round INTEGER,
        stage INTEGER,
        essay TEXT,
        created_at TEXT,
        FOREIGN KEY(student_id) REFERENCES students(id)
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER,
        role TEXT,
        content TEXT,
        strategy TEXT,
        created_at TEXT,
        FOREIGN KEY(conversation_id) REFERENCES conversations(id)
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('active_round','1')")
    conn.commit()
    conn.close()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def get_active_round():
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key='active_round'").fetchone()
    conn.close()
    return int(row["value"]) if row else 1


def set_active_round(round_num):
    if round_num not in (1, 2, 3, 4):
        round_num = 1
    conn = get_db()
    conn.execute("UPDATE settings SET value=? WHERE key='active_round'", (str(round_num),))
    conn.commit()
    conn.close()


def get_active_stage():
    return ROUND_STAGE.get(get_active_round(), 1)


def get_latest_conversation(student_id):
    """返回该学生最近一次对话（用于跨登录复用，让学生看到历史、AI 记住断裂点）。"""
    conn = get_db()
    row = conn.execute(
        "SELECT id, round, stage, essay FROM conversations "
        "WHERE student_id=? ORDER BY created_at DESC LIMIT 1",
        (student_id,)).fetchone()
    conn.close()
    return row


# ---------------- 模型调用 ----------------
def parse_strategy_tag(text):
    """提取末尾【策略：xxx】/【引导：xxx】标注并返回(clean_text, strategy)。"""
    m = re.search(r"【(?:策略|引导)[：:]\s*(.+?)】", text)
    if m:
        strategy = m.group(1).strip()
        clean = re.sub(r"【(?:策略|引导)[：:].+?】\s*$", "", text).strip()
        return clean, strategy
    return text.strip(), ""


def call_zhipu(system_prompt, history):
    """调用智谱 OpenAI 兼容接口。history: [(role, content), ...]"""
    messages = [{"role": "system", "content": system_prompt}]
    for role, content in history:
        messages.append({"role": role, "content": content})
    payload = json.dumps({
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": 0.7,
    }).encode("utf-8")
    req = urllib.request.Request(
        ZHIPU_URL,
        data=payload,
        headers={
            "Authorization": "Bearer " + ZHIPU_API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


DEMO_STRATEGY_CYCLE = STRATEGY_ORDER


def demo_respond(stage, history, last_user, round_num=1):
    """无 API key 时的演示应答，保证界面可跑通流程。"""
    if stage == 1:
        turn = max(0, len([h for h in history if h[0] == "user"]))
        # 阶段一按轮次可用策略池轮换，保持与研究设计一致
        from prompts import ROUND_POOL
        pool = ROUND_POOL.get(round_num, ROUND_POOL[1])
        strat = pool[turn % len(pool)]
        q = {
            "退一步": "你提到的这个观点里，核心概念具体指什么？换一种说法你会怎么界定它？",
            "跳出去": "如果换一个完全不同的领域（比如生物演化的角度）来看，你这个结论还成立吗？",
            "对立面": "设想一个坚决反对你的人，他最可能从哪个角度反驳你？什么情况下你的观点站不住？",
            "环顾式": "支撑你论点的证据和你的主张之间，逻辑上的'为什么成立'是哪一步？中间还缺什么？",
            "进一步": "如果这个观点成立，会带来哪些更深层的后果或启示？",
            "回顾看": "回顾我们刚才的对话，你觉得自己的论证哪一部分变扎实了？重写会改哪里？",
        }[strat]
        return q + f"\n\n【策略：{strat}】"
    else:
        strat = "回顾看"
        m = re.search(r"\[学生使用策略：(.+?)\]", last_user or "")
        if m:
            strat = m.group(1).strip()
        return (f"（演示模式·逻辑顾问）我收到你用「{strat}」策略的提问了。"
                f"你能先自己说说：按这个角度，你现在的论证哪里还站不稳吗？\n\n【引导：{strat}】")


def generate_assistant(stage, history, last_user="", essay="", round_num=1):
    """返回 (text, strategy)。"""
    if stage == 1:
        system_prompt = build_stage1_system(round_num)
        if essay and essay.strip() and not history:
            # 仅在首次（无历史）把作文作为首条 user 消息，让 AI 基于作文开问；
            # 之后一律用 history，保留完整上下文，AI 才能记住断裂点、基于学生回答继续追问
            api_history = [("user", "【学生提交的前测作文】\n" + essay.strip()
                            + "\n请直接基于这篇作文，抛出一个具体的追问问题作为第一次对话，"
                              "第一句话就是问题本身，不要先分析、不要列诊断清单。")]
        else:
            api_history = history
    else:
        system_prompt = STAGE2_SYSTEM
        api_history = history
    if ZHIPU_API_KEY:
        try:
            raw = call_zhipu(system_prompt, api_history)
            return parse_strategy_tag(raw)
        except Exception:
            raw = demo_respond(stage, history, last_user, round_num)
            return parse_strategy_tag(raw)
    raw = demo_respond(stage, history, last_user, round_num)
    return parse_strategy_tag(raw)


# ---------------- 路由：学生端 ----------------
@app.route("/")
def index():
    logged_in = "student_id" in session
    history_messages = []
    if logged_in and session.get("conv_id"):
        conn = get_db()
        rows = conn.execute(
            "SELECT role, content, strategy FROM messages "
            "WHERE conversation_id=? ORDER BY id",
            (session["conv_id"],)).fetchall()
        conn.close()
        history_messages = [{"role": r["role"], "content": r["content"],
                             "strategy": r["strategy"]} for r in rows]
    return render_template(
        "student.html",
        student_name=session.get("student_name", ""),
        round_num=get_active_round(),
        stage=get_active_stage(),
        strategies=STRATEGY_INFO,
        strategy_order=STRATEGY_ORDER,
        stage2_order=__import__("prompts").STAGE2_CARD_ORDER,
        round_pool=__import__("prompts").ROUND_POOL.get(get_active_round(), __import__("prompts").ROUND_POOL[1]),
        simplified=__import__("prompts").ROUND_SIMPLIFIED.get(get_active_round(), False),
        logged_in=logged_in,
        history_messages=history_messages,
    )


@app.route("/start", methods=["POST"])
def start():
    name = (request.form.get("name") or "").strip()
    sid = (request.form.get("sid") or "").strip()
    essay = (request.form.get("essay") or "").strip()
    if not name:
        return jsonify({"error": "请填写姓名"}), 400
    round_num = get_active_round()
    stage = ROUND_STAGE.get(round_num, 1)
    conn = get_db()
    # 同名复用学生记录
    stu = conn.execute("SELECT id FROM students WHERE name=?", (name,)).fetchone()
    if stu:
        student_id = stu["id"]
        if sid:
            conn.execute("UPDATE students SET sid=? WHERE id=?", (sid, student_id))
    else:
        cur = conn.execute("INSERT INTO students(name, sid, created_at) VALUES(?,?,?)",
                           (name, sid, now_str()))
        student_id = cur.lastrowid

    # 按轮次分对话：每个学生每个轮次一个独立对话，后台/导出天然区分第几轮。
    conv = conn.execute(
        "SELECT id, essay FROM conversations WHERE student_id=? AND round=? "
        "ORDER BY created_at DESC LIMIT 1",
        (student_id, round_num)).fetchone()

    if conv:
        # 复用“当前轮次”的对话（学生看到本轮历史；AI 上下文已含旧对话，记住断裂点）
        conv_id = conv["id"]
        if not essay and conv["essay"]:
            essay = conv["essay"]  # 本轮已有作文则沿用
        rows = conn.execute(
            "SELECT role, content, strategy FROM messages WHERE conversation_id=? ORDER BY id",
            (conv_id,)).fetchall()
        history = [{"role": r["role"], "content": r["content"], "strategy": r["strategy"]}
                   for r in rows]
        opening = None  # 复用旧对话，历史已含开场，不再生成新开场白
    else:
        # 当前轮次尚无对话：新建。作文必填——若本次未粘贴且无历史作文，则要求填写。
        existing = conn.execute(
            "SELECT essay FROM conversations WHERE student_id=? AND essay IS NOT NULL "
            "AND essay<>'' ORDER BY created_at DESC LIMIT 1", (student_id,)).fetchone()
        base_essay = essay or (existing["essay"] if existing else "")
        if not base_essay:
            conn.close()
            return jsonify({"error": "请先粘贴一段你的作文或观点，再开始本轮对话"}), 400

        cur = conn.execute(
            "INSERT INTO conversations(student_id, round, stage, essay, created_at) VALUES(?,?,?,?,?)",
            (student_id, round_num, stage, base_essay, now_str()))
        conv_id = cur.lastrowid

        # 带入该生所有历史对话作为上下文（AI 记住此前轮次的断裂点，实现跨轮次记忆）
        hist_rows = conn.execute(
            "SELECT role, content FROM messages m JOIN conversations c "
            "ON c.id=m.conversation_id WHERE c.student_id=? ORDER BY m.id",
            (student_id,)).fetchall()
        history_ctx = [(r["role"], r["content"]) for r in hist_rows]

        if stage == 1:
            opening, strategy = generate_assistant(1, history_ctx, essay=base_essay,
                                                   round_num=round_num)
        else:
            opening = STAGE2_OPENING
            strategy = "回顾看"
        conn.execute(
            "INSERT INTO messages(conversation_id, role, content, strategy, created_at) VALUES(?,?,?,?,?)",
            (conv_id, "assistant", opening, strategy, now_str()))
        history = [{"role": "assistant", "content": opening, "strategy": strategy}]

    conn.commit()
    conn.close()
    session["student_id"] = student_id
    session["student_name"] = name
    session["conv_id"] = conv_id
    return jsonify({"ok": True, "opening": opening, "history": history,
                    "stage": stage, "round": round_num})


@app.route("/chat", methods=["POST"])
def chat():
    if "student_id" not in session or "conv_id" not in session:
        return jsonify({"error": "请先填写姓名开始对话"}), 400
    user_text = (request.form.get("message") or "").strip()
    if not user_text:
        return jsonify({"error": "消息为空"}), 400
    conv_id = session["conv_id"]
    conn = get_db()
    conv = conn.execute(
        "SELECT round, stage, essay FROM conversations WHERE id=?", (conv_id,)).fetchone()
    round_num = conv["round"]
    stage = conv["stage"]
    essay = conv["essay"] or ""
    # 取历史
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id",
        (conv_id,)).fetchall()
    history = [(r["role"], r["content"]) for r in rows]
    # 阶段二：学生用策略卡，记录策略
    strategy_used = ""
    raw_user = user_text
    if stage == 2:
        m = re.search(r"\[学生使用策略：(.+?)\]", user_text)
        if m:
            strategy_used = m.group(1).strip()
            user_text = re.sub(r"\[学生使用策略：(.+?)\]\s*", "", user_text).strip()
    history.append(("user", user_text))
    conn.execute(
        "INSERT INTO messages(conversation_id, role, content, strategy, created_at) VALUES(?,?,?,?,?)",
        (conv_id, "user", user_text, strategy_used, now_str()))
    # 生成 AI 回复
    text, strategy = generate_assistant(stage, history, raw_user, essay, round_num)
    conn.execute(
        "INSERT INTO messages(conversation_id, role, content, strategy, created_at) VALUES(?,?,?,?,?)",
        (conv_id, "assistant", text, strategy, now_str()))
    conn.commit()
    conn.close()
    return jsonify({"reply": text, "strategy": strategy})


@app.route("/history")
def history():
    """返回当前会话对话的全部历史消息，供学生端回放。"""
    if "conv_id" not in session:
        return jsonify({"history": []})
    conn = get_db()
    rows = conn.execute(
        "SELECT role, content, strategy FROM messages WHERE conversation_id=? ORDER BY id",
        (session["conv_id"],)).fetchall()
    conn.close()
    h = [{"role": r["role"], "content": r["content"], "strategy": r["strategy"]}
         for r in rows]
    return jsonify({"history": h})


@app.route("/history_list")
def history_list():
    """学生端：列出该生所有对话（按轮次），供切换查看往期。"""
    if "student_id" not in session:
        return jsonify({"list": []})
    conn = get_db()
    rows = conn.execute(
        "SELECT c.id, c.round, c.stage, c.created_at, "
        "(SELECT content FROM messages WHERE conversation_id=c.id ORDER BY id LIMIT 1) "
        "AS first_msg FROM conversations c WHERE c.student_id=? ORDER BY c.created_at DESC",
        (session["student_id"],)).fetchall()
    conn.close()
    out = [{"conv_id": r["id"], "round": r["round"], "stage": r["stage"],
            "created_at": r["created_at"], "preview": (r["first_msg"] or "")[:36]}
           for r in rows]
    return jsonify({"list": out})


@app.route("/history/<int:cid>")
def view_history(cid):
    """学生端：查看某往期对话的全部消息（只读）。"""
    if "student_id" not in session:
        return jsonify({"history": []})
    conn = get_db()
    conv = conn.execute("SELECT id FROM conversations WHERE id=? AND student_id=?",
                        (cid, session["student_id"])).fetchone()
    if not conv:
        conn.close()
        return jsonify({"history": []})
    rows = conn.execute(
        "SELECT role, content, strategy FROM messages WHERE conversation_id=? ORDER BY id",
        (cid,)).fetchall()
    conn.close()
    h = [{"role": r["role"], "content": r["content"], "strategy": r["strategy"]}
         for r in rows]
    return jsonify({"history": h})


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ---------------- 路由：教师后台 ----------------
@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        return render_template("admin_login.html", error="密码错误")
    if session.get("admin"):
        return redirect(url_for("admin_dashboard"))
    return render_template("admin_login.html")


@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    students = conn.execute(
        "SELECT s.id, s.name, s.sid, COUNT(DISTINCT c.id) AS convs, "
        "MAX(c.created_at) AS last_active, "
        "(SELECT c2.round FROM conversations c2 WHERE c2.student_id=s.id "
        " ORDER BY c2.created_at DESC LIMIT 1) AS last_round "
        "FROM students s "
        "LEFT JOIN conversations c ON c.student_id=s.id "
        "GROUP BY s.id ORDER BY last_active DESC").fetchall()
    round_num = get_active_round()
    stage = ROUND_STAGE.get(round_num, 1)
    api_ok = bool(ZHIPU_API_KEY)
    conn.close()
    return render_template("admin_dashboard.html", students=students,
                           round_num=round_num, stage=stage, api_ok=api_ok)


@app.route("/admin/student/<int:sid>")
def admin_student(sid):
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    stu = conn.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    convs = conn.execute(
        "SELECT * FROM conversations WHERE student_id=? ORDER BY created_at",
        (sid,)).fetchall()
    conv_data = []
    for conv in convs:
        msgs = conn.execute(
            "SELECT role, content, strategy, created_at FROM messages "
            "WHERE conversation_id=? ORDER BY id", (conv["id"],)).fetchall()
        conv_data.append({"conv": conv, "msgs": msgs})
    conn.close()
    return render_template("admin_student.html", stu=stu, conv_data=conv_data,
                           toulmin_map=TOULMIN_MAP)


@app.route("/admin/settings", methods=["POST"])
def admin_settings():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    round_num = int(request.form.get("round", 1))
    set_active_round(round_num)
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/export")
def admin_export():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    rows = conn.execute(
        "SELECT s.name, s.sid, c.round, c.stage, c.created_at AS conv_time, "
        "m.role, m.strategy, m.content, m.created_at "
        "FROM messages m JOIN conversations c ON m.conversation_id=c.id "
        "JOIN students s ON c.student_id=s.id ORDER BY s.id, c.id, m.id").fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["姓名", "学号", "轮次", "阶段", "对话开始时间", "角色",
                     "使用/引导策略", "图尔敏要素", "内容", "消息时间"])
    for r in rows:
        toulmin = TOULMIN_MAP.get(r["strategy"], "") if r["strategy"] else ""
        writer.writerow([r["name"], r["sid"] or "", r["round"], r["stage"], r["conv_time"],
                         "学生" if r["role"] == "user" else "AI",
                         r["strategy"] or "", toulmin, r["content"], r["created_at"]])
    data = "\ufeff" + output.getvalue()
    return Response(
        data.encode("utf-8"),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=socratic_export_%s.csv"
                 % today_str()},
    )


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin_login"))


# 生产环境（gunicorn 等）导入模块时即建表，确保服务启动即有数据库
init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    # use_reloader=False：避免 reloader 子进程 cwd 变化导致 .env 加载失败
    # debug 仅本地开发开启：FLASK_DEBUG=1 python app.py
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1", use_reloader=False)
