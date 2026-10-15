# -*- coding: utf-8 -*-
"""苏格拉底式对话支架 · 网页版智能体 (Flask)
学生端：输入姓名/学号 -> 进入对话（阶段一虚拟论敌 / 阶段二策略卡）
教师端：后台按学生、按日期查看全部对话，并一键导出 CSV
模型：智谱 GLM-4-Flash（永久免费），无 API key 时自动进入 demo 模式。

严格遵循开题报告：四轮行动研究，轮次(1-4)由教师后台设定，
第1-2轮=阶段一（虚拟论敌，AI主动追问），第3-4轮=阶段二（逻辑顾问，学生用策略卡主动追问）。
"""
import difflib
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
    build_stage1_system, ROUND_STAGE, TOULMIN_MAP, DRAFT_TYPE_BY_ROUND,
    POINT_ORDER, POINT_DESC, POINT_STRATEGY, build_strategy_card, STAGE1_ASK,
    POINT_GROUPS,
)

import textbook

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DB_PATH 可经环境变量覆盖（如需挂载持久磁盘）；默认项目内 SQLite 文件
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "conversations.db"))
# 用绝对路径加载 .env，避免 Flask debug reloader 子进程因 cwd 变化而读不到配置
load_dotenv(os.path.join(BASE_DIR, ".env"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY", "")
ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL_NAME = os.environ.get("MODEL_NAME", "glm-4-flash")

# 每轮必须提交的作文最小字数：低于此长度视为“只写了一个观点”，不予通过
MIN_ESSAY_LEN = int(os.environ.get("MIN_ESSAY_LEN", "100"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "socratic-dev-secret-change-me")


# ---------------- 数据库 ----------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table, column, decl):
    """轻量迁移：表已存在时补列（线上旧库升级用，避免删库）。"""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]
    if column not in cols:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))


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
        draft_type TEXT,
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
    # 旧库迁移：补 draft_type 列（第1轮＝原稿 / 第2轮＝升格稿），并回填历史数据
    ensure_column(conn, "conversations", "draft_type", "TEXT")
    conn.execute("UPDATE conversations SET draft_type='原稿' "
                 "WHERE round=1 AND (draft_type IS NULL OR draft_type='')")
    conn.execute("UPDATE conversations SET draft_type='升格稿' "
                 "WHERE round=2 AND (draft_type IS NULL OR draft_type='')")
    # 追问方向记录：这一轮 AI 追的是哪个断裂点。**只用于内部推进**（让判定知道
    # 哪些点已经追过、该换点了），不作为研究编码数据——断裂点编码由研究者人工完成。
    ensure_column(conn, "messages", "point", "TEXT")
    # 本次作文题的题干（题目＋材料／写作要求），学生必填。
    # 没有它，AI 只看得到学生写了什么，看不出扣没扣题。
    ensure_column(conn, "conversations", "topic", "TEXT")
    # 这一回合是不是"学生向你要材料"（求助回合）。
    # 【2026-10-15】为什么非要落库：连着三次求助就该收了，否则 AI 一直在给抓手、
    # 学生一直说"我不会"——截图里那组就是这么连问了四轮。
    # 只靠当回合判定算不出"连着几次"，得看历史，所以要持久化。
    ensure_column(conn, "messages", "help_type", "TEXT")
    # 组次：同一轮次里第几组追问（收尾后学生再来 → 另开一组，沿新的断裂点继续）。
    ensure_column(conn, "conversations", "group_no", "INTEGER")
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


def has_round_conversation(student_id, round_num):
    """该生本轮是否已建过对话（已建则本轮不必再次提交作文）。"""
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM conversations WHERE student_id=? AND round=? LIMIT 1",
        (student_id, round_num)).fetchone()
    conn.close()
    return bool(row)


def get_prev_essay(student_id, round_num):
    """第2轮（升格稿）需要第1轮的原稿做对照，让 AI 判断"上轮追的断裂点补上了没"。
    其余轮次返回空串。"""
    if round_num != 2:
        return ""
    conn = get_db()
    row = conn.execute(
        "SELECT essay FROM conversations WHERE student_id=? AND round=1 "
        "AND essay IS NOT NULL AND essay<>'' ORDER BY created_at DESC LIMIT 1",
        (student_id,)).fetchone()
    conn.close()
    return (row["essay"] or "") if row else ""


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
# 研究标注格式：@@策略=环顾式@@ / @@收尾=是@@（写在回复最末尾，解析后剥离，学生看不到）
META_STRATEGY_RE = re.compile(r"@@\s*策略\s*[=＝:：]\s*(.+?)\s*@@")
META_WRAP_RE = re.compile(r"@@\s*收尾\s*[=＝:：]\s*(是|否)\s*@@")
LEGACY_STRATEGY_RE = re.compile(r"【(?:策略|引导)[：:]\s*(.+?)】")


def parse_meta(text):
    """把模型回复拆成 (给学生的正文, 引导策略, 是否收尾)。

    这是**降级路径**：正常走的是 JSON 结构化输出（见 JSON_TAIL / parse_json_reply），
    只有模型没按 JSON 返回、或 JSON 调用失败时才会用到这里，用来兜住旧格式
    【策略：xx】以及任何残留标注，保证标注不会泄漏到学生端。
    """
    raw = text or ""
    m = META_STRATEGY_RE.search(raw)
    w = META_WRAP_RE.search(raw)
    strategy = m.group(1).strip() if m else ""
    should_wrap = bool(w) and w.group(1) == "是"
    if not strategy:
        lm = LEGACY_STRATEGY_RE.search(raw)
        if lm:
            strategy = lm.group(1).strip()
    clean = META_STRATEGY_RE.sub("", raw)
    clean = META_WRAP_RE.sub("", clean)
    clean = LEGACY_STRATEGY_RE.sub("", clean)
    clean = re.sub(r"[ \t]+\n", "\n", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    # 兜底：模型偶尔仍会在末尾附标注，而且可能是 JSON 形式
    # （实测漏出过“研究标注：{"策略": "对立面", "是否收尾": "是"}”，直接显示给学生了）。
    # 系统提示词已改成“说完就完了”，这里再兜一层，保证标注不出现在学生端。
    clean = re.sub(r"(?:研究)?(?:标注|记录)[：:]?\s*\{[^{}]*\}", "", clean)
    clean = re.sub(r"\{\s*[“\"]?\s*(?:策略|strategy|wrap|收尾)[^{}]*\}", "", clean)
    clean = re.sub(r"[ \t]+\n", "\n", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    # 策略名归一化：模型可能写成“策略：环顾式（追问论据）”，这里只保留标准策略名。
    # 注意遍历用的是 STRATEGY_ORDER 里的标准名，逐个子串匹配即可。
    if strategy:
        for s in STRATEGY_ORDER:
            if s in strategy:
                strategy = s
                break
    if should_wrap:
        # 收尾回合统一记为「回顾看」，后台与 CSV 才能一眼认出收尾、并对应到
        # TOULMIN_MAP 里的“整体结构·元认知反思”。
        strategy = "回顾看"
    return clean, strategy, should_wrap


# 模型的高频套话开头。提示词里禁过两遍，glm-4-flash 照写不误——这是它训练先验里
# 极常见的开场句式，靠提示词压不住，只能在生成后做一次确定性清理。
TIC_OPENERS = ("我注意到你提到", "你在作文中提到", "你在文中提到", "你刚才提到",
               "你之前提到", "你曾提到", "你在文中说", "你提到的", "你提到",
               "你所说的")


# 开场特有的毛病：模型爱用“你刚才说‘……’”——可那一句的出处是**作文**、不是对话，
# 说“刚才说”会让学生以为是自己前面提过。开场一律改写成“你在作文里写”。
OPENING_SAY_OPENERS = ("你刚才提到", "你刚才说", "你之前说", "你曾说过", "你说过", "你说到")


# 纯废话插入语（把话接下去用的垫话）。删掉不影响语义，留着只显得啰嗦。
# 用正则连它前面的“那么，/这里/还有”一起吃掉，避免删完剩下孤零零的“这里，”。
FILLER_RE = re.compile(
    r"(?:那么|这里|还有|另外|所以|其实|然后|接着)?[，,]?\s*"
    r"(?:我(?:想|很|挺|也|还是)?(?:问一下|问一问|问问|先了解一下|了解一下|知道|好奇|感兴趣的是|想问)"
    r"|让(?:我)?问一下)"
    r"[，,]?\s*"
)


def clean_ai_tics(text, is_opening=False):
    """清掉追问里的套话。

    两类分开处理，因为它们出现的规律不同：
    · 引用式（“你提到”“你刚才说”）只出现在开头，只在开头清；
    · 垫话式（“我想了解一下”“那么，我想知道”）会接在复述学生那句话之后，
      位置飘忽，所以改成**在第一句问号之前整段里清**——这段里的垫话删掉都不影响语义。
    """
    if not text:
        return text
    out = text.lstrip()
    if is_opening:
        for t in sorted(OPENING_SAY_OPENERS, key=len, reverse=True):
            if out.startswith(t):
                rest = out[len(t):].lstrip("，,、：: ")
                if rest[:1] in ("“", "‘", "\""):
                    return "你在作文里写" + rest
                break
    # 引用式：只在开头这一小段里找（正文中间的“你提到的那个例子”是正常指代，不动）
    for t in sorted(TIC_OPENERS, key=len, reverse=True):
        idx = out.find(t, 0, 40)
        if idx >= 0:
            rest = (out[:idx] + out[idx + len(t):]).lstrip("，,、：: ")
            if is_opening and idx == 0 and rest[:1] in ("“", "‘", "\""):
                rest = "你在作文里写" + rest
            rest = re.sub(r"[，,、]{2,}", "，", rest)      # 删掉插入语后可能留下连着的逗号
            rest = re.sub(r"([。！？；])\1+", r"\1", rest)
            out = rest
            break
    # 垫话式：第一句问号之前的都清掉（一句话里塞两个也能清干净）
    q = out.find("？")
    seg, tail = (out[:q], out[q:]) if q >= 0 else (out, "")
    for _ in range(3):
        new_seg = FILLER_RE.sub("", seg, count=1)
        if new_seg == seg:
            break
        seg = new_seg
    return seg + tail


def collect_prev_questions(history, limit=5, width=110):
    """取出前面几轮 AI 已经问过的问题，用于在就近约束里**逐条列出**。

    为什么不能只说“不许重复”：只给抽象禁令时，模型会把同一句质询换个问法再问一遍
    （实测出现“他们是否意识到……道德风险”连续两轮几乎原样出现）。把已经问过的问题
    原文摊开在它面前，才有约束力。
    """
    qs = []
    for role, content in history:
        if role != "assistant":
            continue
        c = " ".join((content or "").split())
        if c:
            qs.append(c[:width])
    return qs[-limit:]


# 学生的求助信号词：他是在向 AI 要材料，而不是在作答。
# 用子串匹配（"提示"能接住"给我点提示""给个提示""提示一下"各种说法）——
# 实测按整句匹配，"想不出别的例子了"这种既不是"想不出来"也不是"想不到"，
# 一条都接不住，教材线索于是一次都没给出去。
HELP_SIGNALS = (
    "不知", "想不出", "想不到", "说不上", "答不上", "不会", "没思路",
    "没头绪", "举不出", "没例子", "没想法", "没头绪", "提示", "帮我",
    "写不出", "说不出", "卡住", "不清楚", "想不起来", "没啥可说",
)


def looks_like_help_request(text):
    """他这句话是在要材料，还是在作答？

    09-29 这个版本没有 10-03 那套求助判定机制，而教材线索的用途恰恰是
    "他向你要材料时，给他一个篇目让他自己去回忆"。所以这里用关键词判定，
    不额外调一次 API——省一次调用，也不引入判定漂移（temperature 波动）。
    """
    t = (text or "").strip()
    if not t or len(t) > 60:
        # 长句多半是在正经作答（正经作答时不需要教材抓手）
        return False
    return any(s in t for s in HELP_SIGNALS)


# 【2026-10-15】学生抱怨"你一直在重复问"的信号词。
# 截图里学生的原话就是「怎么又问我重复的问题」「我刚才说过了」——旧词表一条都没接住，
# 于是 AI 同一处追了四轮。接住了不算本事（真正的兜底是下面那条与词表无关的配额检查），
# 但至少这一句能把"他已经在抵触了"这个信息用上去。
REPEAT_COMPLAINTS = (
    "又问", "重复的问题", "问重复", "问了好几", "问了好几遍", "问过好几",
    "说过了", "讲过了", "已经说过", "不是说过", "刚说过", "刚刚说过",
    "我刚才不是说了", "我刚说了", "你绕", "一直在绕", "别绕", "答非所问",
)


def looks_like_repeat_complaint(text):
    """他这句话是在抱怨"你又问同一个"，不是在作答。"""
    t = (text or "").strip()
    if not t or len(t) > 60:
        return False
    return any(s in t for s in REPEAT_COMPLAINTS)


# 一回合最多允许几个问句。超过就是"三问过载"——学生不知道该答哪个，
# 通常挑最好答的那个搪塞，追问等于白问。允许 2 个：一个主问 + 一个贴身的限定追问
# （"兼听指什么？是仅仅指多听吗？"这种还读得通），到第 3 个就散了。
MAX_QUESTIONS_PER_TURN = 3
# 与本组上一条 AI 回复的最大相似度。超过就判"同一个问题换了个说法"，
# 重生成。这是文本层的确定性判据，绕不开——断裂点判定失败、点换来换去都能绕过配额，
# 但两句话像不像骗不了人。
REPLY_SIMILARITY_MAX = 0.55


def count_questions(text):
    """数这一条回复里有几个问句（中英文问号都算）。"""
    if not text:
        return 0
    return text.count("？") + text.count("?")


def allowed_book_titles(essay="", extra=""):
    """AI 这一回合可以提到的书名／篇名的白名单。

    【2026-10-15】生成后的书名白名单。为什么需要它：求助回合要"给个抓手"，
    抓手只该来自两个地方——教材索引里的篇目，或者他自己作文里写到的东西。
    可模型为了给抓手，会随手牵出一个篇名来充当"你读过的某某"（实测在
    "连说我想不出来"那一组里连着两轮都提了《竹林中》，即便线索里根本没给它）。
    这类篇名学生未必真读过，他一句话都答不上来，对话就卡死在这里。
    白名单＝教材索引里的篇目 ∪ 他作文里自己提到过的书名，不在名单里的一律重生成。

    **边界要说清**：那一组里《竹林中》其实是他作文原文里写的，属于合法引用；
    这条规则拦的不是"引用作文"，而是**凭空造引用**——来源必须有据可查。
    
    """
    titles = set()
    for it in textbook.load_textbook_index():
        t = (it.get("篇目") or "").split("（")[0].strip()
        if t:
            titles.add(t)
    for src in (essay or "", extra or ""):
        titles.update(re.findall(r"《([^》]{1,16})》", src))
    return titles


def trim_to_one_question(text):
    """兜底：把连珠炮式的追问砍成第一个问句。

    只在"重生成两次还是问了一堆"时动刀——截断会丢内容，但比甩三四个问题给学生强。
    截完太短就放弃，免得切出一个光秃秃的残句。
    """
    if not text:
        return text
    pos = min([i for i in (text.find("？"), text.find("?")) if i >= 0] or [-1])
    if pos < 0:
        return text
    kept = text[:pos + 1].strip()
    return kept if len(kept) >= 8 else text


# 同一轮次最多开几组追问。一组 = 一次完整的"追到点上 → 收尾"。
# 为什么要上限：只要学生在上一组收尾后再来，就会开新的一组；没有上限的话
# 他每来一次就多一组，正式收数据时后台会堆一堆只有开场白的空组。
MAX_GROUPS_PER_ROUND = 2


def count_round_conversations(student_id, round_num):
    """该生本轮已经开了几组对话。"""
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) c FROM conversations WHERE student_id=? AND round=?",
        (student_id, round_num)).fetchone()
    conn.close()
    return int(row["c"]) if row else 0


def round_points_asked(student_id, round_num):
    """该生本轮**所有组**已经追过哪些断裂点——跨组汇总。

    为什么必须跨组：原来只统计当前这条对话，于是每开一组计数归零，
    AI 又从头追一遍上一组已经追过、学生也答明白了的那个点，
    学生看到的就是"怎么又问我重复的问题"。
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT m.point FROM messages m JOIN conversations c "
        "ON c.id=m.conversation_id WHERE c.student_id=? AND c.round=? "
        "AND m.role='assistant' AND m.point IS NOT NULL AND m.point<>'' "
    , (student_id, round_num)).fetchall()
    conn.close()
    return [r["point"] for r in rows]


def round_conversation_wrapped(conv_id):
    """这一组是否已经收尾（出现过「回顾看」的 AI 消息）。"""
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM messages WHERE conversation_id=? AND role='assistant' "
        "AND strategy='回顾看' LIMIT 1", (conv_id,)).fetchone()
    conn.close()
    return bool(row)


def round_fully_covered(student_id, round_num):
    """六个断裂点里，非收尾的那五个是不是都追过了。追遍了就不再开新组。"""
    asked = set(round_points_asked(student_id, round_num))
    return all(p in asked for p in POINT_ORDER if p != "元认知反思缺失")


def round_prev_history(student_id, round_num, exclude_conv_id, limit=6):
    """取该生本轮**更早那几组**的最后若干条对话，用于开场诊断。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT m.role, m.content FROM messages m JOIN conversations c "
        "ON c.id=m.conversation_id WHERE c.student_id=? AND c.round=? "
        "AND c.id<>? ORDER BY m.id DESC LIMIT ?",
        (student_id, round_num, exclude_conv_id, limit)).fetchall()
    conn.close()
    return "\n".join(("AI：" if r["role"] == "assistant" else "学生：")
                     + (r["content"] or "")[:200] for r in reversed(rows))


def topic_head(topic, limit=24):
    """从题干里只取「题目」那一行。

    整道题干抄进来会带进"时代""社会""人生"这类高频词，检索就成了撞运气——
    实测靠一个"时代"命中《以工匠精神雕琢时代品质》，而那篇和兼听则明毫无关系。
    题目那四个字才是检索该用的东西。
    """
    for ln in (topic or "").split("\n"):
        s = ln.strip()
        if s.startswith(("题目：", "题目:")):
            return s[:limit]
    return (topic or "").strip()[:limit]


def pick_textbook_hints(query, history=None, top_n=2):
    """检索教材篇目线索，并剔掉这几轮已经提过的篇目。

    剔除是必要的：不剔的话它会连着两轮都提《劝学》，而学生根本没在讲学习之道——
    "重复提同一篇"和"追问原地打转"是同一种毛病，只是换了载体。
    """
    try:
        hits = textbook.search_textbook(query, top_n=top_n + 3)
    except Exception:
        return []
    if not hits:
        return []
    used = set()
    for role, content in (history or []):
        if role == "assistant":
            used.update(re.findall(r"《([^》]{1,12})》", content or ""))
    out = []
    for h in hits:
        title = h.get("篇目", "")
        if not title:
            continue
        # 索引里篇目带作者后缀（"劝学（荀子）"），比对时取前半
        if title in used or title.split("（")[0] in used:
            continue
        out.append(h)
        if len(out) >= top_n:
            break
    return out


def build_help_nudge(last_user="", next_point="", textbook_hints=None,
                     prev_questions=None):
    """他卡住了、向你要材料时的就近约束（题材、措辞同 10-03 那套，分支砍掉）。

    旧版没有这一环：学生说"我想不出来"，AI 只管继续追问，于是越问越抽象、
    学生越答不出——教材索引加进来后依然用不上（2026-10-15 实测三场，
    一次篇目都没提：写成"可选用"，它就不选）。见 build_turn_nudge 的 help_mode。

    09-29 这个版本原本没有这一环：学生说"我想不出来"，AI 只管继续追问，
    于是越问越抽象、学生越答不出——教材索引加进来后依然用不上
    （2026-10-15 实测三场，一次篇目都没提，因为"可选用"它就不选）。

    结构照 10-03 那套（给抓手 + 缩小问题），但砍掉了那里的分支：
    这里只有一个求助形态，不再分 direct / repeat。

    红线仍是那一条：**给抓手，不给答案**；抓手只能是他自己写过的东西，
    或者教材的篇目。
    """
    quote = (last_user or "").strip()
    if len(quote) > 120:
        quote = quote[:120] + "……"
    # 已经问过的问题逐条摊开：光说"不许重复"压不住它换句式重问
    asked = ""
    if prev_questions:
        asked = ("【你已经问过他下面这几个问题——这一轮**必须换一个**，"
                 "换个说法再问一遍也算重复】\n"
                 + "\n".join("· " + q for q in prev_questions) + "\n")
    tb = ""
    if textbook_hints:
        tb = "\n" + textbook.format_textbook_hints(textbook_hints, strong=True) + "\n"
    focus = ""
    if next_point and next_point in POINT_DESC:
        focus = ("（这一轮仍追同一个点——" + next_point + "，"
                 + build_strategy_card(next_point) + "难度要降下来。）\n")
    return (
        "（以上是你和他的对话。他刚才说的是：“" + quote + "”\n"
        "**他这一句不是在作答，是在向你要材料**——他卡住了，说不下去了。\n"
        + focus + asked + tb +
        "按这个顺序说三件事：\n"
        "1. **一句安抚**（只一句）：承认这一问确实难，卡在这儿很正常。"
        "不许说“你已经很好了”“你很有潜力”这种空话。\n"
        "2. **给一个具体抓手**：\n"
        "　　① 上面给了教材线索时，**就提那一篇**——只提篇目，让他自己去回忆。"
        "（给了线索又不提，教材索引就成了摆设：实测它每次都挑更容易的作文原句。）\n"
        "　　② 没给教材线索时，才退回他作文里（或他刚说过的）已经写下的那一句，"
        "让他看出答案其实就在他自己写的东西里。\n"
        "3. **把问题缩到他答得上来**：不要再问“这个概念是什么意思”这种大问题，"
        "要问一个具体的、能一口答上来的小问题（比如让他从自己写过的那句话里"
        "挑出两三项、先说其中最明显的一项）。\n"
        + tb +
        "【红线——违反就等于替他写】\n"
        "· 不许把答案说出来：不许定义概念、不许写开头或段落、不许举例证、"
        "不许讲课文内容或名句。\n"
        "· **引据必须有出处**：能提的书名／篇名只有两种——上面给的教材线索里的那一篇，"
        "或者他自己作文里写到的那一句。除此之外不要牵出别的篇目来当“你读过的某某”，"
        "（他不熟的东西只会让他更答不上来）；虚构一个篇目是最坏的情况，等于替他找材料。\n"
        "· 提篇目时**不要去问那篇里的情节或人物**（“哪个角色”“哪一段讲了什么”）——"
        "问的是它跟你眼前这个说法对不对得上。追人物情节是考据，推不动论证。\n"
        "· **不许只有安抚没有抓手**，也不许连甩好几个问题——**只能有一个问句**。\n"
        "· 不要用“你提到”“我想了解一下”这类套话开场。\n"
        "· 这一回合**不要**收尾，也**不要**换成另一个断裂点。\n"
        "**最后必须以一个问句收尾。**\n"
        "回复说完就完了，不要附任何标注、括号说明、JSON 或记号。）"
    )


def build_turn_nudge(stage, is_opening=False, last_user="", wrap_mode="", turn_no=1,
                     prev_questions=None, next_point="", point_repeat=0,
                     textbook_hints=None, help_mode=False):
    """追加在每回合对话末尾的即时指令（「就近约束」）。

    为什么放在末尾而不是系统提示词里：系统提示词很长，模型对其中“怎么问”的约束
    遵循度低——实测会出现连续几轮追问同一句、每轮都用“你提到……”套话开场的情况。
    把本回合要做什么紧贴生成位置说一遍，才压得住。

    踩过坑才加上的细节：
    · 必须**显式引用学生刚说的那句话**。否则模型会顺手引用作文的末句当“他最后这一句”
      （曾出现 8 轮里 6 轮都在追问作文末句同一处）。
    · is_opening=True 时不能说“针对他刚才那句”——那是本轮第一次追问。
    · wrap_mode 把“收尾”变成一个明确指令（"now" 收尾 / "after" 收尾后多说的），
      而不是让模型自己判断该不该收——实测它判断不出来，会一路追下去。
    · prev_questions 逐条列出已问过的问题。另一个高频毛病是**用“是否……？”“是不是……”
      这种只要学生答“是／否”的问句**，追问推不动，必须一并禁掉。
    """
    # 他卡住了：整段换成求助结构。教材线索就是在这时才给出去的——
    # 平时不给，是为了避免变成"每问必提《劝学》"的硬拉。
    #     【2026-10-15】wrap_mode 非空时不许再走求助结构——已经判定该收了，
    #     就不能回头"再给一次抓手"。
    if help_mode and not wrap_mode:
        return build_help_nudge(last_user, next_point, textbook_hints, prev_questions)
    asked = ""
    if prev_questions:
        asked = ("你已经问过他下面这几轮问题，**这一回合绝对不要重复它们**——"
                 "换个说法再问一遍是重复，把同一件事从另一个角度绕回来也是重复：\n"
                 + "\n".join("· " + q for q in prev_questions) + "\n")
    focus = ""
    if next_point and next_point in POINT_DESC:
        focus = ("**这一轮只盯住这一个断裂点——" + next_point + "**："
                 + POINT_DESC[next_point] + "\n")
        if point_repeat:
            # 第二次追同一个点：光说“换个角度”它做不到，得把“不许怎么问”点明
            focus += ("这个点你上一轮已经问过一次了（问句见上），他没答到你要的层次。"
                      "**这一轮不许再用“什么情况下”“举个例子”“你怎么看”这类话去问他**，"
                      "也别把他刚才说过的话再问一遍。换一个更小、他更容易接住的切口："
                      "给一个具体的假设情境让他判断，或者拿他刚举的那个例子反过来问。\n")
        else:
            focus += "整个回复就围绕它问一个问题，不要顺手再问别的点，也不要把前几轮问过的点再绕回来。\n"
    if stage == 2:
        return (
            "（以上是你和他的对话记录。他这一句是拿着策略卡主动来追问你的。"
            "现在写你这一回合的回应，要求：\n"
            "1. 你是“逻辑顾问”，只回应、只反问：不替他给出论点、提纲、范文，答案由他自己说。\n"
            "2. 先让他自查一步，再反问一个问题，不要连甩好几个问题。\n"
            "3. 不许用“你提到”“我想问一下”“能否具体说明”“换句话说”这类套话开头，"
            "不许复述他的话当开场，不许重复你前面问过的内容或句式。\n"
            + asked +
            "4. 回复说完就完了，不要附任何标注、括号说明、JSON 或记号。）"
        )
    if is_opening:
        return (
            "（这是本轮的第一个追问。请基于上面【学生本轮提交的作文】写你这回合的回复：\n"
            + asked +
            "1. 第一句话就是你的问题本身，只问一个问题，不要连续甩好几个问题。\n"
            "2. 不许先分析、不要列“诊断清单”、不要评价他的作文；不要用“你的作文很有意思”"
            "这类寒暄。要点名他作文里的哪句话时，直接把原句引出来就行，不要写“你提到”三个字。\n"
            "3. 这个问题必须逼他解释（具体指什么／为什么／凭什么／这中间差哪一步），"
            "不要问“是否……”“是不是……”这类答“是／否”就能应付过去的问题，"
            "也不要问“是 A 还是 B”这种让他二选一的问题。\n"
            "4. 回复说完就完了，不要附任何标注、括号说明、JSON 或记号。）\n"
            + build_strategy_card(next_point, strict=True))
    quote = (last_user or "").strip()
    if len(quote) > 160:
        quote = quote[:160] + "……"
    head = ("（以上是你和他的对话记录，这是你和他之间的**第 " + str(turn_no) + " 轮追问**"
            "（不含开场）。他刚才说的是：“" + quote + "”\n")
    if wrap_mode == "after":
        return head + (
            "他这句是本组已经收尾之后又多说的：**不要再抛新的追问问题**，"
            "只简短回应他这句，肯定他的回看，并告诉他本组已经聊完、可以进入下一轮。\n"
            "回复说完就完了，不要附任何标注、括号说明、JSON 或记号。）")
    if wrap_mode == "now":
        return head + (
            "**这一回合要收尾，不要再问任何新问题。** 学生刚应付完一连串追问，认知负荷已经很高，"
            "所以按这个顺序说三件事：\n"
            "1. 先用两三句话替他梳理这组对话：他一开始的说法是什么、中间卡在哪儿、"
            "最后自己想清楚了什么。只梳理**对话过程和他自己的变化**，不替他补论证、不给结论、"
            "不给范文或模板；\n"
            "2. 再请他回看：现在觉得自己的论证哪部分变结实了？如果重写这篇作文，会改哪里？\n"
            "3. 最后明确告诉他：这组问题到这儿就聊完了。\n"
            "回复说完就完了，不要附任何标注、括号说明、JSON 或记号。）")
    opener_line = ("现在就按上面指明的那个断裂点，针对他这一句里最没说清的地方追问：\n"
                   if focus else "现在只针对他这一句里最没说清的一处继续追问：\n")
    # 教材线索：只在他求助（要材料）时才给，且只给篇目——见 pick_textbook_hints
    tb = ""
    if textbook_hints:
        tb = "\n" + textbook.format_textbook_hints(textbook_hints) + "\n"
    # 【2026-10-15】卡片约束放在整条 nudge 的**最后**——离生成位置最近，权重最高。
    # 放在前面时模型会忽略它，回到它最顺手的问法（实测五张卡长期只用两种）。
    card_tail = build_strategy_card(next_point, strict=True) if next_point else ""
    return head + asked + focus + tb + opener_line + (
        "1. **追问的方向必须始终朝着他的论证本身**——他那句话立不立得住、那个例子撑不撑得住"
        "观点、有没有反例、有没有例外。**不要滑到对他举的例子做细节考据**"
        "（比如那个例子里的人当时心里怎么想、依据了哪条原则、有没有别的动机）——"
        "例子只是用来撑观点的工具，考据例子本身推不动他的论证。\n"
        "2. 第一句话就是你的问题本身，只问一个问题。\n"
        "3. 这个问题必须逼他解释：具体指什么／为什么／凭什么／怎么一步步推出来的／"
        "如果……会怎样／这中间还差哪一步。**严禁问“是否……”“是不是……”“对不对……”"
        "这类只要他回“是／否”就完事的问题，也不许问“是 A 还是 B”这种二选一**——"
        "他随口挑一个就能应付过去，追问就推不动了。\n"
        "4. 如果他答的还是含糊，说明上一轮问得太大了、他摸不着边——"
        "**换一个更小、更具体、他更容易接住的角度来撬**（缩到一个人物、一个情境、"
        "一组对比上），不要把那句质询再问一遍。他已经答明白的点就往前推进到下一个断裂点。\n"
        "5. 不许用“你提到”“我想问一下”“能否具体说明”“换句话说”“从某种程度上”这类套话开头，"
        "不许复述他的话、也不要引用作文里的原句当开场。\n"
        "6. **这一回合绝不要收尾**——时机还没到，收尾由系统决定，不是由你决定。"
        "不许出现“我们来回顾一下”“这组问题就聊到这里”“你已经做得很好了”这类话，"
        "也不许替他总结；整条回复就是这一问，问他，然后等他答。\n"
        "7. 回复说完就完了，不要附任何标注、括号说明、JSON 或记号。）\n"
        + card_tail)


def call_zhipu(system_prompt, history, json_mode=False, temperature=0.6):
    """调用智谱 OpenAI 兼容接口。history: [(role, content), ...]

    json_mode=True 时要求模型输出 JSON（智谱支持 response_format），
    用于稳定拿到「引导策略」「是否收尾」这两个研究字段——实测在长系统提示词下，
    让模型在正文末尾自行附标注的遵循率很低（约 2/9），改用 JSON 约束才可靠。

    temperature：写话用 0.6（要有点变化，不然每轮问法雷同）；**判定类调用必须传 0**
    ——实测同一段对话在 0.6 下会这次判 true、下次判 false，收尾时机随机漂移。
    """
    messages = [{"role": "system", "content": system_prompt}]
    for role, content in history:
        messages.append({"role": role, "content": content})
    body = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    payload = json.dumps(body).encode("utf-8")
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


# 追加在系统提示词末尾的结构化输出要求（配合 response_format=json_object）
# ---------------- 研究字段的独立判定 ----------------
# 为什么单开一次调用：GLM-4-Flash 既不稳定在正文末尾附标注（实测仅约 2/9），
# 也不稳定遵守 response_format=json_object（同一会话里开场遵守、后续就不遵守了）。
# 把「写话」和「打标」拆开，用极短提示词单独判定，才拿得到可靠的研究字段。
JUDGE_SYSTEM = (
    "你是教学对话记录助手，负责给一段“苏格拉底式追问”对话打标注。"
    "只输出一个 JSON 对象（json），不要输出任何解释文字，也不要用代码块包裹。"
)

JUDGE_TEMPLATE = """下面是一段高中议论文陪练对话，AI 扮演“只问不答”的追问者。

【学生这一轮说的】{last_user}

【AI 这一轮的回复】{ai_reply}

请判断两点：

1. strategy —— AI 这一轮主要用的是哪一种追问策略（从下面选最贴近的一个）：
   - 退一步：追问某个关键词、概念到底指什么，要求他把话说清楚；
   - 环顾式：追问**材料与主张之间那一环推理**——他举的例子／引的名言凭什么能说明
     他的观点、中间那一步是怎么连上的、还差什么才撑得住；
     （和“进一步”的分界：环顾式问的是“这条推理连不连得上”，
      进一步问的是“顺着这个结论走下去会有什么后果／更深层含义”）
   - 对立面：追问有没有人反对、什么条件下不成立、话是不是说太满；
   - 跳出去：换一个视角/领域/情境来检验结论，或指出他用的材料太旧；
   - 进一步：顺着他的结论往下问，问后果、影响、更深一层的意思。
   如果这一轮 AI 是在收尾（梳理总结这组讨论、请他回看、说这组聊完了，不再问新问题），
   strategy 填“回顾看”。

2. wrap —— 这一轮是否属于收尾（true 或 false）。

输出格式：{{"strategy": "策略名", "wrap": false}}"""


def parse_json_obj(raw):
    """从模型返回里尽力抠出一个 JSON 对象，失败返回 None。"""
    if not raw:
        return None
    txt = raw.strip()
    txt = re.sub(r"^```(?:json)?\s*", "", txt)
    txt = re.sub(r"\s*```$", "", txt).strip()
    try:
        obj = json.loads(txt)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", txt)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except Exception:
            return None
    return obj if isinstance(obj, dict) else None


def judge_turn(last_user, ai_reply):
    """独立判定本回合的引导策略与是否收尾，返回 (策略, 是否收尾)。

    判定失败返回 ("", False)——不影响学生端对话，后台该行策略留空，便于研究者复核。
    """
    if not ZHIPU_API_KEY:
        return "", False
    prompt = JUDGE_TEMPLATE.format(last_user=(last_user or "")[:300],
                                   ai_reply=(ai_reply or "")[:600])
    try:
        raw = call_zhipu(JUDGE_SYSTEM, [("user", prompt)], json_mode=True, temperature=0)
    except Exception:
        return "", False
    obj = parse_json_obj(raw)
    if not obj:
        return "", False
    strategy = str(obj.get("strategy") or "").strip()
    raw_wrap = obj.get("wrap")
    wrap = raw_wrap if isinstance(raw_wrap, bool) else \
        str(raw_wrap).strip().lower() in ("true", "是", "yes")
    for s in STRATEGY_ORDER:
        if s in strategy:
            strategy = s
            break
    else:
        strategy = ""
    if wrap:
        strategy = "回顾看"
    return strategy, wrap


# 判定调用失败时的保守兜底：只有出现这些明确话术才认定收尾，避免误判提前收摊
# 收尾话术的信号词。词表是实测攒出来的：模型爱说「这组问题我们就聊到这里」
# 「我们来回顾一下这次对话」「可以开始下一轮了」，早期版本只写了「聊到这儿」，
# 漏掉了带"我们"的写法，于是"第 1 个来回就自己收摊"这种最要命的情况一条都拦不住。
WRAPUP_HINTS = ("这组问题到这儿", "这组问题就到", "这组问题就聊到",
                "聊到这儿", "聊到这里", "就聊到这儿", "就聊到这里",
                "这组聊完", "已经聊完", "咱们就到这", "本组聊完",
                "我们来回顾一下", "回顾一下这次对话", "回顾一下我们",
                "梳理一下我们", "总结一这下这组", "这组问题到这儿",
                "可以开始下一轮", "进入下一轮", "开始下一轮", "下一轮讨论")


def looks_like_wrapup(text):
    return any(k in (text or "") for k in WRAPUP_HINTS)


# ---- 追问方向判定：这一轮该追哪个断裂点 ----
# 为什么单开一次判定：让生成模型自己决定追哪儿，它会在同一处打转（实测从“他们怎么判断
# 对错”绕到“依据什么标准”，连问三轮还在原地），还会滑到对例子里的人物做心理考据。
# 把“追哪个点”交给一次短判定，生成模型只负责把这个点问好，链条才推得动。
# 顺带：返回“元认知反思缺失”即表示该收尾，与 judge_wrap 互为印证。
JUDGE_POINT_SYSTEM = (
    "你是教学对话分析助手，负责给一段高中议论文追问对话规划下一步追什么。"
    "只输出一个 JSON 对象（json），不要输出任何解释文字，也不要用代码块包裹。"
)

JUDGE_POINT_TEMPLATE = """这是一篇高中议论文，和围绕它已经发生的一段苏格拉底式追问对话。AI 只问不答，每轮盯住一个论证断裂点发问，学生作答后应该换到下一个断裂点。

【学生的作文】
{essay}

【这组对话已经进行到这里】
{dialogue}

{asked}
六个断裂点按先后分成两截（next 只能从这六个词里选一个原样填）：

【第一截 · 主张层面】——这个观点本身有没有探讨空间：
- 主张模糊：他核心的那句话本身就含糊，关键词含义不清、主张范围不明；
- 限定缺失：他把话说得太满，没交代什么条件下成立、什么情况下不成立；
- 反驳缺席：他从没想过有人会反对，也没交代反方会怎么说。

【第二截 · 支撑层面】——他后面那段够不够证明这个观点：
- 担保断裂：他摆了例子或名言，但**材料到主张之间那一步推理没搭**
  ——说不清这个例子凭什么能说明这个观点（桥断了）；
- 支撑薄弱：桥搭上了，可**依据本身太单薄**
  ——只孤零零一个例子、材料太旧太片面、缺少更根本的依据（货太少）。

【收尾】
- 元认知反思缺失：上面两截他都已经用自己的话说清楚了（哪怕不够严谨），该收尾让他回看了。

**先判第一截，再判第二截**：
· 只要他的核心说法还含糊、说得太满、或者压根没考虑反方，next 就选第一截里的那个点
  ——观点本身还没立清楚，这时候去追证据没有意义；
· 第一截都站得住了，next 才从第二截里选；
· 两截都补上了，next 填“元认知反思缺失”。

请判断两点：
1. covered —— 学生**已经在回答里用自己的话说清楚**的断裂点（他给出了解释、让步或反例就算，哪怕不完美；只是重申观点、只是给词下定义、只是又举一个例子，不算）。
2. next —— **这一轮最该追的那一个**断裂点。不要选 covered 里的，**也不要再选前几轮已经反复追过的**——同一个点追两轮他还答不上来，就换下一个点，别原地打转。

输出格式：{{"covered": ["担保断裂"], "next": "限定缺失"}}"""


# ---- 收尾决策：交给独立判定，不指望生成模型自己“收摊” ----
# 实测 glm-4-flash 在生成时不肯主动收尾（一路追到第 8 轮仍在追问），即使提示词里
# 把收尾条件写得很具体。而“判定该不该收尾”是个比“边写边做元决策”简单得多的任务，
# 独立调用可靠得多。因此：判定说该收 → 后端强制走收尾生成。
JUDGE_WRAP_SYSTEM = (
    "你是教学对话分析助手。判断一段苏格拉底式追问对话是否到了该收尾的时候。"
    "只输出一个 JSON 对象（json），不要输出任何解释文字，也不要用代码块包裹。"
)

JUDGE_WRAP_TEMPLATE = """这是一段高中议论文写作陪练对话。AI 扮演“只问不答”的追问者：每次针对学生论证里最没说清的一处发问，学生作答后继续追问；当学生把被追问的地方说明白了，AI 就该收尾（替他梳理这组讨论、请他回看），不再追问。

【最近几轮对话】
{dialogue}

学生到现在一共作答了 {turns} 次。

请判断：**现在这一回合就应该收尾吗？**

判断标准：学生**只是重申自己的观点、只是给某个词下定义、只是又举了一个例子、只是把 AI 的话换个说法复述一遍**，而始终没给出自己的解释，才算没补上。

满足下面任意一条，就判该收尾：
- 学生针对 AI 追问的那个点，说出了自己的解释或机制（出现“因为……”“原因是……”“它之所以……是因为”“我觉得……是因为……”这类表述）。**哪怕他解释得还不严谨、还不周全，也算补上了**——这组追问的目的是让他自己把话说出来，不是把他逼到答不出来；
- 学生想到了有人会怎么反对他，或什么条件下他的说法不成立；
- 学生出现了自我修正（如“我明白了”“原来我搞混了”“如果重写我会……”）；
- AI 最近两轮问的其实是**同一个点**（换了措辞、拆成更细的小问题，都算同一个点），而学生已经给过解释——再追只是原地打转；
- AI 已经把同一个点追问了两次以上，学生仍在含糊逃避，再追也问不出新东西。

注意：学生作答次数少于 3 次时，一律判 false（一组追问至少要覆盖两三个断裂点才有意义）。

输出格式：{{"wrap": false, "reason": "一句话理由"}}"""

# 收尾的硬下限：学生作答不足这个次数，绝不收尾。
# 这不是“追问上限”（不是到点就收），而是“最少要问够几个来回”，
# 避免判定过宽、学生答一轮就被收掉。
MIN_TURNS_BEFORE_WRAP = 3


def judge_wrap(dialogue_tail, turns):
    """判断本组对话这一回合是否该收尾。判定失败返回 False（宁可继续追问，不误收）。"""
    if not ZHIPU_API_KEY or not dialogue_tail:
        return False
    if turns < MIN_TURNS_BEFORE_WRAP:
        return False
    prompt = JUDGE_WRAP_TEMPLATE.format(dialogue=dialogue_tail, turns=turns)
    try:
        raw = call_zhipu(JUDGE_WRAP_SYSTEM, [("user", prompt)], json_mode=True, temperature=0)
    except Exception:
        return False
    obj = parse_json_obj(raw)
    if not obj:
        return False
    w = obj.get("wrap")
    if isinstance(w, bool):
        return w
    return str(w).strip().lower() in ("true", "是", "yes")


def judge_point(essay, dialogue, asked_points=None, only_points=None):
    """判定这一轮该追哪个断裂点。返回 POINT_ORDER 里的一个词，失败返回 ""。

    asked_points：前面几轮已经追过的断裂点。必须告诉它，否则它会在同一个点上
    反复选（实测：学生答完“限定缺失”，它还接着选“限定缺失”，追问原地绕）。
    返回“元认知反思缺失”时，同时意味着该收尾（见 /chat 里的处理）。
    """
    # 新组开场时 tail 为空，此时只凭作文也能定切入点——以前这里直接返回空，
    # 于是每组第一个问题都没有卡片约束，模型又回到它最顺手的那一种问法。
    if not ZHIPU_API_KEY or not (dialogue or essay):
        return ""
    asked = "、".join([p for p in (asked_points or []) if p])
    if only_points:
        allowed = [p for p in only_points if p in POINT_DESC]
        # 已追过的列表必须**只列本截的**：原来把别组追过的点也一并摊给它看，
        # 它一数"已经追过四个了"，再配上"都补上就填元认知"这句，
        # 就直接判收尾——新组第一回合就收摊，这一截一个点都没问（实测第2组只跑2回合）。
        _done = [p for p in allowed if p in (asked_points or [])]
        _todo = [p for p in allowed if p not in (asked_points or [])]
        asked = (("【本组这几个点里，已经追过的】" + "、".join(_done) + "\n")
                 if _done else "")
        asked += ("**这一组只许从下面这几个里挑一个**：" + "/".join(allowed) + "。\n"
                 "其余的断裂点**这一组一概不碰** -- 那是别组的事。\n")
        if _todo:
            asked += ("**下面这几个本组一次都还没追过**：" + "、".join(_todo)
                      + "。next 必须从这几个里挑，**不许填「元认知反思缺失」**"
                      " -- 本组还没问过，谈不上收尾。\n")
        else:
            asked += ("这几个点本组都追过了；只有他在刚才的回答里确实说清楚了，"
                      "next 才填「元认知反思缺失」。\n")
    prompt = JUDGE_POINT_TEMPLATE.format(
        essay=(essay or "")[:1200],
        asked=("【前面几轮已经追过的断裂点】" + asked + "\n"
               "（这些点他要是在回答里已经给了自己的说法，就算补上了；"
               "但别因为它们被追过就当它们补上了——还是要看他到底说清楚没有。"
               "没补上的话可以再追，但要换个更小的切口。）\n") if asked else "",
        dialogue=dialogue[:2000])
    try:
        raw = call_zhipu(JUDGE_POINT_SYSTEM, [("user", prompt)],
                         json_mode=True, temperature=0)
    except Exception:
        return ""
    obj = parse_json_obj(raw)
    if not obj:
        return ""
    nxt = str(obj.get("next") or "").strip()
    # 硬校验：只认允许范围里的点。提示词里写"这一组只许从这几个里挑"拦不住它
    # ——实测第 1 组照样回了个「支撑薄弱」，于是主张层面那组跨到了支撑层面，
    # 按截分组等于没生效。所以后端必须再拦一道：不在候选里的一律不认，
    # 返回空后由 /chat 的兜底配额给一个还没追过的点。
    # 收尾信号「元认知反思缺失」任何一组都要能回，不受本截限制。
    if only_points:
        _ok = set(only_points) | {"元认知反思缺失"}
        cands = [p for p in POINT_ORDER if p in _ok]
    else:
        cands = list(POINT_ORDER)
    for p in cands:
        if p in nxt:
            return p
    return ""


DEMO_STRATEGY_CYCLE = STRATEGY_ORDER


def demo_respond(stage, history, last_user, round_num=1, is_opening=False, wrap_mode=""):
    """无 API key 时的演示应答，保证界面可跑通流程。

    注意 is_opening：跨轮次历史里带着上一轮的若干条 user 消息，如果不区分“这是本轮开场”，
    第2轮开场会被误判成“已经聊了好几轮”而直接收尾。
    """
    if stage == 1:
        if wrap_mode in ("now", "after") or (not is_opening and not wrap_mode and
                                             len([h for h in history if h[0] == "user"]) >= 3):
            return ("（演示模式·收尾）咱们这组问题聊到这儿。回头看一下：你一开始给的是一个比较"
                    "笼统的说法，中间我追着问了两轮，你才把「你的例子到底是怎么支持观点」这一层"
                    "讲清楚；反方会怎么说，你后来也想到了。现在你觉得自己的论证哪一部分变结实了？"
                    "如果重写这篇作文，你会改哪里？\n\n@@策略=回顾看@@\n@@收尾=是@@")
        # 阶段一策略全开，演示模式按序轮换
        from prompts import ROUND_POOL
        pool = ROUND_POOL.get(round_num, ROUND_POOL[1])
        turn = 0 if is_opening else len([h for h in history if h[0] == "user"])
        strat = pool[turn % len(pool)]
        q = {
            "退一步": "你提到的这个观点里，核心概念具体指什么？换一种说法你会怎么界定它？",
            "跳出去": "如果换一个完全不同的领域（比如生物演化的角度）来看，你这个结论还成立吗？",
            "对立面": "设想一个坚决反对你的人，他最可能从哪个角度反驳你？什么情况下你的观点站不住？",
            "环顾式": "支撑你论点的证据和你的主张之间，逻辑上的'为什么成立'是哪一步？中间还缺什么？",
            "进一步": "如果这个观点成立，会带来哪些更深层的后果或启示？",
            "回顾看": "回顾我们刚才的对话，你觉得自己的论证哪一部分变扎实了？重写会改哪里？",
        }[strat]
        return q + f"\n\n@@策略={strat}@@\n@@收尾=否@@"
    else:
        strat = "回顾看"
        m = re.search(r"\[学生使用策略：(.+?)\]", last_user or "")
        if m:
            strat = m.group(1).strip()
        return (f"（演示模式·逻辑顾问）我收到你用「{strat}」策略的提问了。"
                f"你能先自己说说：按这个角度，你现在的论证哪里还站不稳吗？\n\n@@策略={strat}@@\n@@收尾=否@@")


def generate_assistant(stage, history, last_user="", essay="", round_num=1, prev_essay="",
                       wrap_mode="", is_opening=False, next_point="", point_repeat=0,
                       topic="", textbook_hints=None, help_mode=False):
    """返回 (给学生的正文, 引导策略, 是否该收尾)。
    prev_essay：第2轮时传入该生第1轮的原稿，用于"对照升格稿再诊断"。
    topic：本次作文题的题干，注入系统提示词，作为"扣没扣题"的尺子。
    wrap_mode："" 正常追问 / "now" 这一回合收尾 / "after" 收尾后学生又多说的。
    is_opening：是否为本轮第一次追问（开场），此时不能说"针对他刚才那句"。"""
    api_history = list(history)
    if stage == 1:
        system_prompt = build_stage1_system(round_num, prev_essay, topic)
        if essay and essay.strip():
            if round_num == 2 and prev_essay and prev_essay.strip():
                intro = (
                    "【学生本轮提交的升格稿】\n" + essay.strip()
                    + "\n这是他在第1轮对话之后自己改出来的稿子。请对照上面附的第1轮原稿，"
                      "先判断上一轮追的那个断裂点补上了没有，再针对**仍然没补上（或新出现）**"
                      "的最突出断裂点，抛出本轮的第一个追问。"
                )
            else:
                intro = (
                    "【学生本轮提交的作文（本轮讨论的文本，以它为准）】\n" + essay.strip()
                )
            # 作文是**背景材料**，必须放在对话历史之前。
            # 踩过的坑：放在末尾时，模型会把「他最后这一句」误认成作文的末句，
            # 于是连续好几轮都在追问作文结尾的同一句话。
            api_history = [("user", intro)] + api_history
    else:
        system_prompt = STAGE2_SYSTEM
    # 本回合是这一组里的第几次追问（开场算第 0 次）——告诉模型轮数，它才收得住尾
    turn_no = len([1 for r, _ in history if r == "assistant"]) + 1
    # 关键：把当回合的即时指令放在对话最末尾（就近约束），
    # 并把前面已问过的问题逐条摊给它看（防止换句式重问同一个点）
    api_history = api_history + [("user", build_turn_nudge(
        stage, is_opening, last_user, wrap_mode, turn_no,
        collect_prev_questions(history), next_point, point_repeat,
        textbook_hints, help_mode))]
    # 1) 正文：纯文本生成（不要求模型附带标注——实测它会漏、且会污染正文风格）
    text = ""
    if ZHIPU_API_KEY:
        # 生成后确定性复检：不合规就带着"上一条作废 + 为什么作废"让它重写，最多 2 次。
        # 【2026-10-15】以前这里只生成一次，写完就发；三问过载、原地打转都靠提示词拦，
        # 而我们用 glm-4-flash 的实测是：提示词拦不住。所以改成"生成—检查—重写"的循环。
        for _attempt in range(3):
            try:
                _raw = call_zhipu(system_prompt, api_history)
            except Exception:
                break                      # 调用失败：用上一轮已经拿到的 text，别丢内容
            if not _raw:
                break
            _clean, _leak_s, _leak_w = parse_meta(_raw)
            _clean = clean_ai_tics(_clean, is_opening)
            bad, note = "", ""
            # (a) 原地打转：这一句和上一条 AI 回复太像，学生看到的就是同一个问题问两遍。
            #     判据是文本相似度，不看断裂点——断裂点判定失败、点换来换去都能绕开配额，
            #     但话写得像不像骗不了人。收尾回合豁免（梳理总结本来就要说几句相似的话）。
            #     阈值卡长度用 12 字而不是 30 字：一个问题通常二十来字，
            #     按 30 卡的时候"同上一条、只换两个词"的短问句整条漏网——
            #     而恰恰是这种短句，学生最一眼看出来"你又问了一遍"。
            _prev_ai = [c for r_, c in history if r_ == "assistant" and (c or "")]
            if wrap_mode not in ("now", "after") and _prev_ai and len(_clean) > 12:
                _ratio = difflib.SequenceMatcher(None, _prev_ai[-1][:400],
                                                 _clean[:400]).ratio()
                if _ratio >= REPLY_SIMILARITY_MAX:
                    bad = "与上一轮问法重合 %.0f%%" % (_ratio * 100)
                    note = ("【上一条回复作废】你这一轮问的和上一轮几乎是同一个问题（重合约 %.0f%%）。"
                            "学生已经答过它了——再问一遍，他只会觉得你没在听。"
                            "**换一个更小的切口**：换他作文里另外一句话来问、或者从另一个角度"
                            "接着往下走，都不许把同一个问题换个说法再抛回去。"
                            "如果他上一次确实没答上来（说不会、说不知道），这一轮就不要再把"
                            "同一个问题还给他，先往前推一步再说。"
                            % (_ratio * 100))
            # (b) 三问过载：一次甩三四个问句，他会挑最好答的那个搪塞过去，追问等于白问。
            #     收尾回合豁免（收尾要梳理 + 请他回看，本来就要问两句）。
            if not bad and wrap_mode not in ("now", "after"):
                _nq = count_questions(_clean)
                if _nq >= MAX_QUESTIONS_PER_TURN:
                    bad = "一次问了 %d 个问句" % _nq
                    note = ("【上一条回复作废】你这一句里连着问了 %d 个问题。\n"
                            "**一次只许问一个。** 学生看完三四个问句不知道该答哪个，"
                            "通常就挑最好答的那个搪塞过去，追问等于没问。\n"
                            "请重写：只留下**最要紧的那一个问题**，其余全部删掉，"
                            "不要留「换句话说」「再问一句」这种补问，也不要把同一个意思"
                            "换个说法再问一遍。整条回复就一问，说完就停。" % _nq)
            # (c) 引据要有出处：这一回合出现的书名／篇名，必须要么是教材索引里的篇目，
            #     要么是他自己作文里写的（实测：为了给抓手，它会拿一个篇名
            #     充当"你读过的某某"，学生根本没读过，一句话也接不上）。
            if not bad:
                _known = allowed_book_titles(essay, prev_essay)
                _unknown = [t for t in re.findall(r"《([^》]{1,16})》", _clean)
                            if t not in _known]
                if _unknown:
                    bad = "凭空举了一本书：《%s》" % _unknown[0]
                    note = ("【上一条回复作废】你提到的《%s》不在他学过的教材里，"
                            "也不在他这篇作文里——**是你自己编的**。"
                            "学生没读过这本书，你这么一提他就彻底答不上了，"
                            "而且让他以为那是老师给的材料。\n"
                            "请重写：**不许再出现任何书名、篇名、名人名言**，"
                            "除非是下面两种情况之一：① 系统给你的教材线索里的那一篇；"
                            "② 他自己作文（或他刚说的话）里写到的那一句。\n"
                            "没有可引的，就只拿他自己已经写下的原句当抓手。"
                            % _unknown[0])
            # (d) 收尾过早：还没问够 MIN_TURNS_BEFORE_WRAP 个来回，它自己就"收摊"了。
            #     实测 p2/p3 只跑 1 个回合就收尾——对话一短，六个断裂点走不到
            #     担保断裂，「环顾式」自然一次都出不来。收尾时机归系统管，不归它管。
            if not bad and wrap_mode not in ("now", "after") \
                    and turn_no < MIN_TURNS_BEFORE_WRAP and looks_like_wrapup(_clean):
                bad = "第 %d 个来回就收尾了（太早）" % turn_no
                note = ("【上一条回复作废】你这才第 %d 个来回就把这组问题收掉了——"
                        "学生还没来得及把自己的说法说清楚。**这一回合不要收尾**，"
                        "删掉「我们来回顾一下」「这组问题就聊到这里」「你已经做得很好了」"
                        "这类话，也不要替他总结。整条回复就是这一问：问他，然后等他答。"
                        % turn_no)
            # (e) 追问回合必须以问句收尾——没有问号就是它自己收掉了、或者在空转。
            if (not bad and wrap_mode not in ("now", "after")
                    and count_questions(_clean) == 0):
                bad = "这一回合没有提问"
                note = ("【上一条回复作废】你这一条里没有问题。"
                        "**这一回合只是追问，不是收尾**——收尾的时机由系统决定。"
                        "请重写：**整条回复就以一句问句结束**，"
                        "不许评价、不许总结、不许说“我们聊到这里”。"
                        "只问那一处他还没说清的地方。")
            text, leaked_strategy, leaked_wrap = _clean, _leak_s, _leak_w
            if not bad:
                break
            api_history = api_history + [("user", note)]
    if not text:
        raw = demo_respond(stage, history, last_user, round_num, is_opening, wrap_mode)
        return parse_meta(raw)
    # 重生成两次还是问一堆，就确定性截断到第一个问句——
    # 宁可短一点，也不能把三四个问题一起甩给学生（他只会挑最好答的那个）。
    if wrap_mode not in ("now", "after") and count_questions(text) >= MAX_QUESTIONS_PER_TURN:
        text = trim_to_one_question(text)
    # 收尾由后端判定驱动：这一回合就是收尾回合，策略固定记「回顾看」
    if wrap_mode in ("now", "after"):
        return text, "回顾看", True
    # 研究字段：策略**由断裂点决定**，不再让模型事后猜。
    # 以前这里让 judge_turn 读完 AI 的话再判"这像哪张卡"——可它看不见我们
    # 指定的断裂点，只凭印象贴标签：实测把绑给「担保断裂」的「环顾式」判成了
    # 「对立面」「退一步」，于是后台策略列里环顾式长期 0 次，
    # 看上去像"环顾式用不上"，其实是**日志贴错了标签**。
    # 现在：就近约束里指定了哪张卡，日志就记哪张卡
    # （研究者拿到的是"支架指派值"，正好也是自变量，口径更硬）。
    # 附带好处：省掉每回合一次判定调用，延迟从 3 次调用降到 2 次。
    _assigned = POINT_STRATEGY.get(next_point or "", "")
    if _assigned:
        strategy, judged_wrap = _assigned, False
    else:
        strategy, judged_wrap = judge_turn(last_user, text)
    if not strategy and leaked_strategy:
        strategy = leaked_strategy
    wrap = bool(judged_wrap or leaked_wrap)
    # 收尾下限：不足 MIN_TURNS_BEFORE_WRAP 个来回，判成"已收尾"一律作废。
    # 实测 p2/p3 才答 1 句 AI 就自己收摊了——对话一短，六个断裂点根本走不到
    # 担保断裂，「环顾式」再怎么绑也用不上。
    if turn_no < MIN_TURNS_BEFORE_WRAP:
        wrap = False
        if strategy == "回顾看":
            # 没问够就判成收尾：这个标签不作数（多半是模型自己收了摊），
            # 留空让研究者人工编码，对话照常往下走。
            strategy = ""
    # 判定失败时的保守兜底：正文里出现了明确的收尾话术，才认定已收尾
    if not wrap and not strategy and looks_like_wrapup(text) and \
            turn_no >= MIN_TURNS_BEFORE_WRAP:
        wrap, strategy = True, "回顾看"
    return text, strategy, wrap


def ensure_round_conversation(student_id, essay="", topic=""):
    """确保该生在当前轮次有**一组正在进行**的对话。返回 (conv_id, essay)。

    规则：
    · 本轮还没有对话 → 必须提交本轮作文才能新建；
    · 最新一组还没收尾 → 接着聊（沿用本组已存的作文/题干，不用重复粘）；
    · 最新一组已收尾 → **另开下一组**（同一篇作文，沿还没追过的断裂点继续深挖），
      直到六个点追遍、或达到 MAX_GROUPS_PER_ROUND。
      之前没有这一条，所以"聊过一次再登录"就永远停在"本组已结束"，不再追问——
      学生等于只有一次被追问的机会，后面几轮支架等于空转。
    """
    round_num = get_active_round()
    stage = ROUND_STAGE.get(round_num, 1)
    conn = get_db()
    conv = conn.execute(
        "SELECT id, essay, topic FROM conversations WHERE student_id=? AND round=? "
    "ORDER BY created_at DESC, id DESC LIMIT 1",
        (student_id, round_num)).fetchone()
    if conv:
        if not essay and conv["essay"]:
            essay = conv["essay"]
        if not topic and conv["topic"]:
            topic = conv["topic"] or ""
        # 这组还没收尾 → 接着聊
        if not round_conversation_wrapped(conv["id"]):
            conn.close()
            return conv["id"], essay
        # 已收尾：六个点都追遍了、或已到组上限 → 停在最后这组，不再开新的
        if round_fully_covered(student_id, round_num) \
                or count_round_conversations(student_id, round_num) >= MAX_GROUPS_PER_ROUND:
            conn.close()
            return conv["id"], essay
    # 当前轮次尚无对话：必须由学生提交本轮的作文才能新建。
    # 不再复用上一轮的旧稿——否则学生会拿前测作文重开第2轮，AI 只能问出一模一样的问题。
    if not essay:
        conn.close()
        return None, ""
    base_essay = essay
    # 稿次标记：第1轮＝原稿（前测作文），第2轮＝升格稿（第1轮对话后自行修改的同一篇）
    draft_type = DRAFT_TYPE_BY_ROUND.get(round_num, "")
    # 第2轮：先取第1轮原稿（写事务之前查，避免与未提交的写操作抢锁）
    prev_essay = get_prev_essay(student_id, round_num)
    # 组次 = 本轮已有组数 + 1（老库没有这一列时按 1 处理）
    group_no = count_round_conversations(student_id, round_num) + 1
    cur = conn.execute(
        "INSERT INTO conversations(student_id, round, stage, essay, draft_type, topic, "
        "group_no, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (student_id, round_num, stage, base_essay, draft_type, topic, group_no, now_str()))
    conv_id = cur.lastrowid
    # 【2026-10-15】开场白的历史上下文只取**本轮次及更早**的对话，并排除本条。
    # 原来只按 student_id 取，于是同一轮换题重开留下的旧对话、后续各轮的消息
    # 全被塞进来——模型一看"已经聊了这么久"，开场就直接收尾（实测第 2 篇起
    # 几乎都是一问就收），这组数据等于没问。
    # 保留 c.round<=round_num 是为了第2轮开场能对照第1轮原稿的对话。
    hist_rows = conn.execute(
        "SELECT m.role, m.content FROM messages m JOIN conversations c "
        "ON c.id=m.conversation_id WHERE c.student_id=? AND c.round<=? "
        "AND c.id<>? ORDER BY m.id",
        (student_id, round_num, conv_id)).fetchall()
    history_ctx = [(r["role"], r["content"]) for r in hist_rows]
    if stage == 1:
        # 【2026-10-15】开场先诊断这一组该从哪个断裂点切入，并把卡片绑上去。
        # 以前开场没有任何策略指向，于是每组的第一个问题永远是它最顺手的那一种。
        _asked = round_points_asked(student_id, round_num)
        _tail = "\n".join(
            ("AI：" if r == "assistant" else "学生：") + (c or "")[:200]
            for r, c in history_ctx[-6:])
        _grp_pts = POINT_GROUPS.get(group_no) or list(POINT_ORDER)
        _nxt = judge_point(base_essay, _tail, _asked, only_points=_grp_pts)
        if _nxt in ("", "元认知反思缺失"):
            _nxt = ""
        opening, strategy, _ = generate_assistant(
            1, history_ctx, essay=base_essay,
            round_num=round_num, prev_essay=prev_essay,
            topic=topic, is_opening=True, next_point=_nxt,
            point_repeat=_asked.count(_nxt) if _nxt else 0)
    else:
        opening = STAGE2_OPENING
        strategy = "回顾看"
    conn.execute(
        "INSERT INTO messages(conversation_id, role, content, strategy, created_at) VALUES(?,?,?,?,?)",
        (conv_id, "assistant", opening, strategy, now_str()))
    conn.commit()
    conn.close()
    return conv_id, base_essay


# ---------------- 路由：学生端 ----------------
@app.route("/")
def index():
    logged_in = "student_id" in session
    history_messages = []
    wrapped = False
    if logged_in and session.get("student_id"):
        # 打开/刷新页面即按教师当前设定的轮次加载或新建对应对话，
        # 不必重新提交登录表单，避免“切了轮次却只显示一个”的困惑。
        student_id = session["student_id"]
        conv_id, _ = ensure_round_conversation(student_id)
        if conv_id:
            session["conv_id"] = conv_id
            conn = get_db()
            rows = conn.execute(
                "SELECT role, content, strategy FROM messages "
                "WHERE conversation_id=? ORDER BY id",
                (conv_id,)).fetchall()
            conn.close()
            history_messages = [{"role": r["role"], "content": r["content"],
                                 "strategy": r["strategy"]} for r in rows]
            # 收尾与否完全由 AI 判断：它以【策略：回顾看】收尾即视为本组结束
            wrapped = any(m["strategy"] == "回顾看" for m in history_messages
                          if m["role"] == "assistant")
        # 若 conv_id 为 None（本轮尚未提交作文），保留登录框让用户粘贴，history_messages 为空
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
        draft_type=DRAFT_TYPE_BY_ROUND.get(get_active_round(), ""),
        min_essay_len=MIN_ESSAY_LEN,
        wrapped=wrapped,
        logged_in=logged_in,
        history_messages=history_messages,
    )


@app.route("/start", methods=["POST"])
def start():
    name = (request.form.get("name") or "").strip()
    sid = (request.form.get("sid") or "").strip()
    essay = (request.form.get("essay") or "").strip()
    # 本次作文题的题干（题目＋材料／写作要求），必填：
    # 没有它，AI 只看得到学生写了什么，看不出扣没扣题、关键词读准了没有。
    topic = (request.form.get("topic") or "").strip()
    if not name:
        return jsonify({"error": "请填写姓名"}), 400
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
    conn.commit()   # 必须提交学生记录，否则 students 表为空、后台看不到该生
    conn.close()

    # 本轮尚未建对话时，必须提交**本轮**的作文（不接受只写一个观点）
    round_num = get_active_round()
    if not has_round_conversation(student_id, round_num):
        if not topic:
            return jsonify({
                "error": "请先填写题干信息：把试卷上这道作文题抄上来"
                         "（题目、材料、写作要求）。只有题目那几个字，"
                         "苏格拉底看不出题目给了什么限制。"}), 400
        if not essay:
            return jsonify({"error": "请先粘贴你本轮的作文，再开始对话"}), 400
        if len(essay) < MIN_ESSAY_LEN:
            return jsonify({
                "error": "请粘贴完整的作文（不少于 %d 字）。只写一句观点不够——"
                         "AI 要读完整篇，才能找到你论证里的问题。" % MIN_ESSAY_LEN}), 400

    # 按轮次分对话：复用 ensure_round_conversation（与首页刷新同一条逻辑）
    conv_id, essay = ensure_round_conversation(student_id, essay, topic)
    if conv_id is None:
        return jsonify({"error": "请先粘贴你本轮的作文，再开始对话"}), 400

    conn = get_db()
    rows = conn.execute(
        "SELECT role, content, strategy FROM messages WHERE conversation_id=? ORDER BY id",
        (conv_id,)).fetchall()
    conn.close()
    history = [{"role": r["role"], "content": r["content"], "strategy": r["strategy"]}
               for r in rows]
    session["student_id"] = student_id
    session["student_name"] = name
    session["conv_id"] = conv_id
    stage = ROUND_STAGE.get(round_num, 1)
    return jsonify({"ok": True, "opening": None, "history": history,
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
        # group_no 必须一起取出来：少了它，下面 conv["group_no"] 会抛 KeyError，
        # 被兜底成 1 —— 于是**每一组都被当成第 1 组**，第 2 组照旧只追主张层面，
        # 支撑层面那一截永远走不到（实测第 2 组两回合就收尾）。
        "SELECT round, stage, essay, student_id, topic, group_no "
        "FROM conversations WHERE id=?",
        (conv_id,)).fetchone()
    round_num = conv["round"]
    stage = conv["stage"]
    essay = conv["essay"] or ""
    # 本次作文题的题干：每个来回都要带着，AI 才能一直拿它当"扣没扣题"的尺子
    conv_topic = (conv["topic"] or "").strip()
    # 第2轮：取第1轮原稿做对照（每个来回都注入，保证全程都能对照原稿判断断裂点是否补上）
    prev_essay = get_prev_essay(conv["student_id"], round_num) if stage == 1 else ""
    # 取历史
    rows = conn.execute(
        "SELECT role, content, strategy, point, help_type FROM messages "
        "WHERE conversation_id=? ORDER BY id", (conv_id,)).fetchall()
    history = [(r["role"], r["content"]) for r in rows]
    # 第2轮＝升格稿对照：把该生第1轮（原稿）的完整对话也带上。
    # 仅靠开场那一次注入不够——后续每个来回若丢掉上一轮对话，AI 就不知道
    # "上次追的是哪个断裂点"，对照会退化成重新诊断一遍。
    if stage == 1 and round_num == 2:
        prev_rows = conn.execute(
            "SELECT m.role, m.content FROM messages m JOIN conversations c "
            "ON c.id=m.conversation_id WHERE c.student_id=? AND c.round=1 "
            "ORDER BY m.id", (conv["student_id"],)).fetchall()
        if prev_rows:
            history = ([("user", "【以下是这位学生上一轮（第1轮·原稿）与你的完整对话记录，"
                                 "已结束，仅供你对照判断他这次补上了什么，不要直接复述给学生】")]
                       + [(r["role"], r["content"]) for r in prev_rows]
                       + [("user", "【上一轮对话记录到此结束。以下是第2轮·升格稿的对话】")]
                       + history)
    # 收尾判据：**完全由 AI 判断**——它认为本轮聚焦的 2-3 个断裂点已被学生补上时，
    # 主动用「回顾看」收尾（先替学生梳理本组讨论、再请他回看），不设轮数上限。
    already_wrapped = any(r["strategy"] == "回顾看" for r in rows if r["role"] == "assistant")
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
    # 收尾决策与追问方向：都交给独立判定（实测生成模型自己不肯收尾，会一路追问下去，
    # 而且会在同一个点上反复绕）。
    wrap_mode = "after" if already_wrapped else ""
    next_point = ""
    point_repeat = 0
    turns = len([r for r in rows if r["role"] == "user"]) + 1
    # 本组是这一轮的第几组 → 决定这一组只追哪一截断裂点
    group_no = 1
    try:
        group_no = int(conv["group_no"] or 1)
    except (IndexError, KeyError, TypeError, ValueError):
        group_no = 1
    only_points = POINT_GROUPS.get(group_no) or list(POINT_ORDER)
    if stage == 1 and not already_wrapped:
        tail = "\n".join(
            ("AI：" if r["role"] == "assistant" else "学生：") + (r["content"] or "")[:300]
            for r in rows[-6:])
        tail += "\n学生：" + user_text[:300]
        # 本组才刚开头时，把更早那几组的对话也接上——否则判定只看得到一句开场白，
        # 诊断不出"上一组已经追过、学生已经答明白的点"，又会绕回去重问。
        if len([r for r in rows if r["role"] == "assistant"]) < 2:
            _earlier = round_prev_history(conv["student_id"], round_num, conv_id)
            if _earlier:
                tail = ("【更早那几组（已结束）】\n" + _earlier
                        + "\n【本组开始】\n" + tail)
        # 已经追过哪些断裂点——**跨组汇总**（内部推进用，不参与研究编码）。
        # 只统计当前这一组的话，每开一组计数就归零，AI 会回头再追一遍
        # 上一组已经追过的点，学生看到的就是"又问一遍"。
        asked_points = round_points_asked(conv["student_id"], round_num)
        # 一次判定办两件事：这一轮追哪个断裂点 + 是否六个点都补上了（→ 该收尾）
        next_point = judge_point(essay, tail, asked_points, only_points=only_points)
        # 【2026-10-15】判定落空（返回空、或被后端硬校验挡下）时**先换点，再谈收尾**。
        # 上一版这里直接回落到 judge_wrap，而 judge_wrap 看得到"更早那几组"的完整
        # 对话，一瞧聊了那么久就判"该收了" —— 于是新组第一回合就收尾，
        # 支撑层面那一截一个点都没问就被判追遍了。
        if not next_point or next_point == "元认知反思缺失":
            _pool = [p for p in (only_points or POINT_ORDER) if p != "元认知反思缺失"]
            _left = [p for p in _pool if asked_points.count(p) < 2]
            if _left:
                _left.sort(key=lambda p: asked_points.count(p))
                next_point, wrap_mode = _left[0], ""
        point_repeat = asked_points.count(next_point) if next_point else 0
        if next_point == "元认知反思缺失" and turns >= MIN_TURNS_BEFORE_WRAP:
            wrap_mode = "now"
        elif not next_point and turns >= MIN_TURNS_BEFORE_WRAP and judge_wrap(tail, turns):
            # 追问方向判定失败、且本截确实没有可换的点了，才回落用收尾判定兜底
            wrap_mode = "now"
    # 他这一句是在向你要材料（卡住了），还是在做答？两条后果：
    # ① 卡住了就走求助结构（给抓手 + 缩小问题），不再只管追问；
    # ② 只有卡住时才检索教材——平时给，他会变成"每问必提《劝学》"的硬拉。
    help_mode = (stage == 1 and wrap_mode == ""
                 and looks_like_help_request(user_text))
    # 他在抱怨"你又问同一个"——这是最直白的抵触信号
    complained = (stage == 1 and looks_like_repeat_complaint(user_text))
    # ---- 【2026-10-15 统一兜底】不让它在同一处反复磨 ----
    # 原先"同一断裂点最多追两轮"这条配额只写在正常追问那一个分支里，求助结构
    # （help_mode）整个绕开了它，而学生答不出时恰恰最容易连续触发求助判定；
    # 判定失败、next_point 为空时更是直接放任，模型顺着惯性把同一个问题再问一遍。
    # 许总截图那一组：学生连说"我想不出来""怎么又问我重复的问题""我刚才说过了"，
    # AI 在同一处追了四轮——按我们自己的判据，这组对话的数据是废的。
    # 现在把配额提到所有分支之外统一执行，判据只看**已写进库的 point 计数**，
    # 不依赖这一回合的判定结果，任何分支都绕不过去。
    if stage == 1 and not already_wrapped:
        ai_pts = round_points_asked(conv["student_id"], round_num)
        helps = [r["help_type"] for r in rows if r["role"] == "assistant"]
        streak = 0                      # 历史末尾连续几次求助（本回合不计）
        for h in reversed(helps):
            if h:
                streak += 1
            else:
                break
        # 换点只在本组这一截里换：第1组只追主张层面，第2组只追支撑层面
        pool = [p for p in (only_points or POINT_ORDER) if p != "元认知反思缺失"]
        unused = next((p for p in pool if p not in ai_pts), "")
        # (A) 判定失败（没有 next_point）时不能放任——给它一个还没追过的点，
        #     否则它就顺着上一轮的惯性把同一个问题换个说法再问一遍。
        if wrap_mode != "now" and not next_point and unused:
            next_point = unused
        # (B) 同一个点最多追两轮，到数硬性换点；六个点都追遍了就收尾。
        #     这不是"追问上限"（不是到点就收），而是不许在同一处磨第三遍。
        if (wrap_mode != "now" and next_point and next_point != "元认知反思缺失"
                and ai_pts.count(next_point) >= 2):
            if unused:
                next_point, point_repeat, wrap_mode = unused, 0, ""
            else:
                next_point = "元认知反思缺失"
                if turns >= MIN_TURNS_BEFORE_WRAP:
                    wrap_mode = "now"
        # (C) 学生已经明确抱怨重复：他不会再认真答了，继续问只是消耗。
        if wrap_mode != "now" and complained and turns >= MIN_TURNS_BEFORE_WRAP:
            next_point, wrap_mode = "元认知反思缺失", "now"
        # (D) 连着第三次求助（本回合 + 历史末尾两次）：再给抓手只会更抵触，该收了。
        if wrap_mode != "now" and help_mode and streak >= 2 and turns >= MIN_TURNS_BEFORE_WRAP:
            next_point, wrap_mode = "元认知反思缺失", "now"
        point_repeat = ai_pts.count(next_point) if next_point else 0
    tb_hints = []
    # 收尾优先于求助：这一回合已经判定该收了，就不能再按求助模板"再给一个抓手"
    # ——那正是截图里连问四轮的成因（学生说不会→给抓手→再问→"你又问"→再给抓手……）。
    if wrap_mode:
        help_mode = False
    elif help_mode:
        tb_hints = pick_textbook_hints(
            " ".join([topic_head(conv_topic), (next_point or ""), user_text[:40]]),
            history, top_n=2)
    # 生成 AI 回复
    text, strategy, should_wrap = generate_assistant(stage, history, raw_user, essay,
                                                    round_num, prev_essay,
                                                    wrap_mode, next_point=next_point,
                                                    point_repeat=point_repeat,
                                                    topic=conv_topic,
                                                    textbook_hints=tb_hints,
                                                    help_mode=help_mode)
    conn.execute(
        "INSERT INTO messages(conversation_id, role, content, strategy, point, "
        "help_type, created_at) VALUES(?,?,?,?,?,?,?)",
        (conv_id, "assistant", text, strategy,
         next_point or ("元认知反思缺失" if wrap_mode == "now" else None),
         "help" if help_mode else None, now_str()))
    conn.commit()
    conn.close()
    # 收尾状态：模型判定该收尾（末尾标注 @@收尾=是@@）、或此前已收尾
    wrapped = bool(should_wrap or already_wrapped or strategy == "回顾看")
    return jsonify({"reply": text, "strategy": strategy, "wrapped": wrapped})


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
        "SELECT c.id, c.round, c.stage, c.draft_type, c.created_at, "
        "(SELECT content FROM messages WHERE conversation_id=c.id ORDER BY id LIMIT 1) "
        "AS first_msg FROM conversations c WHERE c.student_id=? "
        "ORDER BY c.created_at DESC, c.id DESC",
        (session["student_id"],)).fetchall()
    conn.close()
    out = [{"conv_id": r["id"], "round": r["round"], "stage": r["stage"],
            "draft_type": r["draft_type"] or "",
            "created_at": r["created_at"], "preview": (r["first_msg"] or "")[:36]}
           for r in rows]
    return jsonify({"list": out})


@app.route("/history/<int:cid>")
def view_history(cid):
    """学生端：查看某往期对话的全部消息（只读，附该轮提交的稿子，便于改稿时回看）。"""
    if "student_id" not in session:
        return jsonify({"history": []})
    conn = get_db()
    conv = conn.execute("SELECT id, essay, draft_type FROM conversations "
                        "WHERE id=? AND student_id=?",
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
    return jsonify({"history": h, "essay": conv["essay"] or "",
                    "draft_type": conv["draft_type"] or ""})


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
                           round_num=round_num, stage=stage, api_ok=api_ok,
                           min_essay_len=MIN_ESSAY_LEN)


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
    if request.form.get("round"):
        set_active_round(int(request.form.get("round")))
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/export")
def admin_export():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    rows = conn.execute(
        "SELECT s.name, s.sid, c.round, c.stage, c.draft_type, c.topic, "
        "c.created_at AS conv_time, "
        "m.role, m.strategy, m.content, m.created_at "
        "FROM messages m JOIN conversations c ON m.conversation_id=c.id "
        "JOIN students s ON c.student_id=s.id ORDER BY s.id, c.id, m.id").fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["姓名", "学号", "轮次", "稿次", "题干", "阶段", "对话开始时间", "角色",
                     "使用/引导策略", "图尔敏断裂点", "内容", "消息时间"])
    for r in rows:
        toulmin = TOULMIN_MAP.get(r["strategy"], "") if r["strategy"] else ""
        writer.writerow([r["name"], r["sid"] or "", r["round"], r["draft_type"] or "",
                         r["topic"] or "", r["stage"], r["conv_time"],
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
