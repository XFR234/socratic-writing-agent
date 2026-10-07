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
    build_stage1_system, ROUND_STAGE, TOULMIN_MAP, DRAFT_TYPE_BY_ROUND,
    POINT_ORDER, POINT_DESC, build_stage0_system, STAGE0_OPENING,
    STAGE0_WRAP_BLOCK, STAGE0_POINTS, STAGE0_POINT_DESC, STAGE0_POINT_MAP,
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
    # 作文题目与材料（老师需求①第一版）：AI 没有这两个字段就不知道自己在审什么题，
    # 只能从作文里反推题意，容易把"跑题"当成论证断裂点去追，而不是指出跑题本身。
    ensure_column(conn, "conversations", "topic", "TEXT")
    ensure_column(conn, "conversations", "material", "TEXT")
    # 求助回合标记：这一回合是不是在回应学生的求助（direct / repeat）。
    # **只用于描述性统计与事后复核，不参与断裂点编码**（编码仍由研究者人工完成）。
    ensure_column(conn, "messages", "help_type", "TEXT")
    # 阶段 0（审题）独立成一轮：round=0，题目材料存在这一轮里
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

# 句子开头的"这一点的难点"式铺垫。实测 glm-4-flash 很爱用，一整组对话里
# 连着七八轮都拿这句开头（"这个点确实挺抽象的""这个点确实挺难把握的"），
# 读起来像复读机。它不是废话内容，但放在开头会把真正的追问压到后面，
# 而且让每轮看起来都差不多。所以整句删掉，只留下后面那个实质问题。
# 逐个短语贪心吃掉（可重复），吃干净为止。
LEADIN_PHRASES = [
    # 【2026-10-03 重写】只收实测出现过的**完整短语**，不再收"这""这确实"这类
    # 过短通用词——它们会咬到正文里的"这个想法""你提到过的那句话"，
    # 造出"个想法""过的那句话"这种残缺（实测踩过两次）。
    # 代价是漏掉没见过的变体，但漏删只是观感问题，误删是语病问题——宁可漏。
    "哎呀这个点确实挺难想的", "哎呀这个确实挺难想的", "哎呀这个点确实挺难",
    "这个点确实挺难想的", "这个点确实挺难把握的", "这个点确实挺难琢磨的", "这个点确实挺难讲的",
    "这个点确实挺难说清", "这个点确实挺难抽象", "这个点确实挺难懂",
    "这个点确实挺难把握的", "这个点确实挺难理解的", "这个点确实挺难界定",
    "这个点确实挺难", "这个点确实有点抽象", "这个点确实挺抽象的",
    "这个点确实挺重要的", "这个点确实挺关键的", "这个点确实",
    "这一点确实挺难想的", "这一点确实挺难的", "这一点确实挺难",
    "这一点确实挺抽象的", "这一点确实有点抽象", "这一点确实",
    "这个问题确实挺难想的", "这个问题确实挺难的", "这个问题确实挺难",
    "这个问题确实挺抽象", "这个问题有点抽象", "这个问题确实",
    "这个概念确实挺难想的", "这个概念确实挺难理解的", "这个概念确实挺难",
    "这个说法确实挺难", "这个想法确实挺难",
    "卡在这儿很正常", "卡在这儿挺正常", "卡住很正常",
    "遇到瓶颈很正常", "遇到难题很正常", "遇到困难很正常",
    "理解起来有难度", "理解起来有点难度", "理解起来不太容易",
    "说起来有点抽象", "说清楚不太容易", "不太好说",
    "这个点有点抽象", "这一点有点抽象", "有点抽象", "挺抽象的",
    "有点难懂", "不太好懂", "不太容易理解", "不容易说清", "不太好说清",
    "这个点挺难", "这一点挺难", "确实挺难", "挺难把握", "难以把握",
    # 【2026-10-03 补】实测漏网：学生**根本没求助**，AI 却拿"这个确实挺难的"开头。
    # 旧表只收"这个点确实挺难的"（带"点"），生成的是不带"点"的变体，整句没删掉。
    "这个确实挺难的", "这个确实挺难", "这个确实有点难", "这个确实不太容易",
    "挺重要", "很重要", "挺关键", "很关键", "挺抽象",
]
# 短语表必须按长度降序使用，否则短的那个会先命中、只吃掉一半。
# 这不是洁癖：实测"这个概念确实挺难"和"这个概念确实挺难理解的"同时在表里时，
# 短的先命中就只吃掉一半，剩下"理解的。"变成语病，连修三轮都在处理它的残留。
_LEADIN_SORTED = sorted(LEADIN_PHRASES, key=len, reverse=True)

# 整句删除用的模式：这些是**完整的安抚句**，要吃掉整句（含后面的逗号/句号），
# 而短语表只吃句首片段。实测 88% 的回复带这类句，不删的话追问被压到第三四句才出现。
# 【2026-10-03 补】新增"遇到这样的问题很正常""咱们慢慢来"等实测漏网变体——
# 学生没求助也没受挫，这类安慰一句都不该有（许总当场指出的问题）。
REASSURANCE_RE = re.compile(
    r"(?:卡在这儿(?:挺|很)?正常|卡住(?:了)?(?:很|挺)?正常"
    r"|遇到(?:了)?(?:瓶颈|难题|困难)(?:很|挺)?正常"
    r"|遇到(?:了)?(?:这样|这种|这类)(?:的)?(?:问题|情况|事|题目)?(?:很|挺|都)?正常"
    r"|这(?:也)?(?:是)?(?:很|挺)正常(?:的)?(?:事|情况)?|这样(?:很|挺)正常"
    r"|咱们(?:慢慢来|慢慢想|别急|不着急|不急|一步一步来)"
    r"|这个(?:确实)?(?:很|挺|有点)(?:难|难说|难想|难懂|抽象))(?:的)?[，,。]?"
)

# 空洞的正面评价：说了等于没说，还会把后面的追问压成"补充说明"。
# 竞品整程都是"你提到XX，很好/这是很关键的判断"，学生因此感受不到被追问
# ——只夸不顶，追问就退化成采访。报告附录一是策略原文、不改，这类空话在这里单独清。
EMPTY_PRAISE_RE = re.compile(
    r"^(?:嗯+|哦+|啊+|唉+|哎呀+|哎+|诶+|噢+)[，,]?"
    r"|(?:这话说得|你说的这话|你这句话)[^。！？]{0,12}(?:挺|很|真)?(?:有见地|不错|很好|到位|准确)[，,]?"
    r"|^(?:不错|很好|很好啊|说得好|说得对|有道理)[，,]"
)


# 语病清理：模型偶尔会写出"不过过""所以所以"这类叠字。
DUP_WORD_RE = re.compile(r"(不过|所以|但是|而且|然后|其实|就是|这个|一个|真的)\1+")


def strip_leadin(text):
    """删掉句首的"这一点的难点"式铺垫（贪心吃短语，重复吃）。

    教训（2026-10-03）：这一块试过加"悬空助词清理"、加审题阶段变体，
    每次都能造出新语病（"。不过，…，的。""…提到的"，。"）——
    **用正则改中文自然语言本身脆弱，越修越坏**。所以这里保持最小实现：
    只按短语表吃掉句首铺垫 + 长度安全阀兜底。剩下的措辞问题不动。
    """
    if not text:
        return text
    out = text.lstrip()
    orig = out
    # 必须按长度降序匹配：短语表里"这个概念确实挺难"和"这个概念确实挺难理解的"
    # 同时存在时，短的先命中就只吃掉一半、剩下"理解的。"这种碎片。
    for _ in range(5):
        before = out
        for p in _LEADIN_SORTED:
            if out.startswith(p):
                out = out[len(p):]
                break
        out = out.lstrip("，,、。：:；; ")
        if out == before:
            break
    for _ in range(3):
        before = out
        for c in ("不过", "但是", "所以", "因此", "而且", "其实", "但"):
            if out.startswith(c):
                out = out[len(c):].lstrip("，,、。：:；; ")
                break
        if out == before:
            break
    # 安全检查：删完必须还剩实质内容，且不能把整句吃光
    if len(out) < 12:
        return orig
    return out


def clean_ai_tics(text, is_opening=False, keep_leadin=False):
    """清掉追问里的套话。

    三类分开处理，因为它们出现的规律不同：
    · 引用式（"你提到""你刚才说"）只出现在开头，只在开头清；
    · 垫话式（"我想了解一下""那么，我想知道"）会接在复述学生那句话之后，
      位置飘忽，所以改成**在第一句问号之前整段里清**——这段里的垫话删掉都不影响语义；
    · 难点铺垫式（"这个点确实挺抽象的…"）在正常追问里要清（一整组对话连用七八轮
      就像复读机），但**求助回合要保留**——那里它是设计好的安抚句。

    keep_leadin=True 时保留难点铺垫（求助回合专用）。
    """
    if not text:
        return text
    out = text.lstrip()
    out = DUP_WORD_RE.sub(r"\1", out)
    # 去掉句首的空洞表扬（"这话说得挺有见地""不错，"）：它会把追问压成补充说明
    for _ in range(3):
        new = EMPTY_PRAISE_RE.sub("", out, count=1)
        new = new.lstrip("，,、。：:；; ")
        if new == out or len(new) < 12:
            break
        out = new
    # 先去掉句首的难点铺垫（求助回合保留安抚句）
    if not is_opening and not keep_leadin:
        out = strip_leadin(out)
        # 整句删安抚（"卡在这儿很正常"）——它常在铺垫之后、追问之前，
        # 只吃句首片段删不掉。实测 75% 的回复都带它，追问被压到第三四句才出现。
        # 求助回合必须保留：那里的安抚句是设计好的。
        #
        # 独立循环、不设长度安全阀：删一个从句不会把句子删空，实测之前的
        # len<12 判断会让这里失效（删完还剩 20 字才继续，导致反复删不到）。
        for _ in range(4):
            new = REASSURANCE_RE.sub("", out, count=1)
            new = new.lstrip("，,。：:；; ")
            if new == out:
                break
            out = new
        out = fix_dangling_question(out)
    if is_opening:
        for t in sorted(OPENING_SAY_OPENERS, key=len, reverse=True):
            if out.startswith(t):
                rest = out[len(t):].lstrip("，,、：: ")
                if rest[:1] in ("“", "‘", "\""):
                    return "你在作文里写" + rest
                break
    # 引用式：只在开头这一小段里找（正文中间的"你提到的那个例子"是正常指代，不动）
    for t in sorted(TIC_OPENERS, key=len, reverse=True):
        idx = out.find(t, 0, 40)
        if idx < 0:
            continue
        after = out[idx + len(t):idx + len(t) + 1]
        # 保护：命中处后面紧跟虚词时，多半是"你提到过的""你说过的话"这类
        # 正常指代，不是套话（实测把"你提到过的那句话"吃成"过的那句话"）。
        if after in ("过", "的", "那", "呢"):
            continue
        # 【2026-10-07】旧版把"你在作文中提到，"也一并放过了（after=="，"就 continue），
        # 结果它成了最高频的漏网套话——9 篇实测里重复出现。
        # 但又不能直接删：删掉"这是他作文里写的"这个归属，追问就脱离了那篇作文
        # （正是同一轮实测里刚修的"滑出作文"）。所以**改写成"你在作文里写"**。
        if after in ("，", ",", "：", ":") and ("作文" in t or "文中" in t
                                               or t.startswith("你提到")
                                               or t.startswith("你在文中说")):
            rest = out[:idx] + "你在作文里写" + out[idx + len(t):]
        else:
            rest = out[:idx] + out[idx + len(t):]
        rest = rest.lstrip("，,、：: ")
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
    return fix_dangling_question(seg + tail)


def fix_dangling_question(text):
    """修掉"比如？""对不对？"这类没内容的空问句尾巴。

    实测求助回合出现过「你可以从这句话里挑出两三项来说说看，比如？或者……」
    ——"比如？"单独成问句，读者不知道要回答什么。

    安全阀：**删完整句如果一个问号都不剩，就不删**。句末那种"你怎么看，行不行？"
    虽然啰嗦，但它是真正的问句，删了就变成陈述句、对话直接断掉。
    """
    if not text:
        return text
    orig = text
    out = text
    # 形如「……吧，比如？或者……」：空问句夹在正文中间，后面还有内容。
    # 后面只要还有内容（"或者"也算）就说明真正的问句在后面那个。
    out = re.sub(r"[，,]\s*(?:比如|譬如|对不对|是吗|对吧|是不是|好不好|行不行|可以吗)\s*[？?]\s*"
                 r"(?=(?:或者|或是|还是|接下来|下一步|你|请)[^。！？]{0,40})",
                 "。", out)
    # 句末孤立的空问句
    out = re.sub(r"[。；;，,]\s*(?:比如|对不对|对吧|行不行|可以吗)\s*[？?]\s*$", "。", out)
    out = out.replace("。。", "。")
    out = out.strip()
    # 安全阀一：原文只有一个问号时，说明那很可能就是唯一的真问句，不能删
    if out.count("？") == 0 and orig.count("？") <= 1:
        return orig
    # 安全阀二：删完剩下的太短
    if len(out) < 10:
        return orig
    return out


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


def build_turn_nudge(stage, is_opening=False, last_user="", wrap_mode="", turn_no=1,
                     prev_questions=None, next_point="", point_repeat=0):
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
    asked = ""
    if prev_questions:
        asked = ("你已经问过他下面这几轮问题，**这一回合绝对不要重复它们**——"
                 "换个说法再问一遍是重复，把同一件事从另一个角度绕回来也是重复：\n"
                 + "\n".join("· " + q for q in prev_questions) + "\n")
    focus = ""
    if next_point and next_point in POINT_DESC:
        focus = ("**这一轮只盯住这一个断裂点——" + next_point + "**：" + POINT_DESC[next_point] + "\n")
        if next_point == "关键词解读偏差":
            # 这一处最容易被带偏：模型会顺手去追作文里第一个抓眼的比喻或例子，
            # 实测它开口就问"地基指什么"——而那属于另外五个断裂点。
            # 题目里明明有两三个关键词，一个字都没问，追问就白费了。
            focus += ("**硬要求：这一句必须问题目里的关键词，不能问作文里的话。**"
                      "不要引用他作文里的比喻、例子、名言去追问——"
                      "先问题目那个词在这个题目里指什么。\n")
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
            + focus +
            asked +
            "1. 第一句话就是你的问题本身，只问一个问题，不要连续甩好几个问题。\n"
            "2. 不许先分析、不要列“诊断清单”、不要评价他的作文；不要用“你的作文很有意思”"
            "这类寒暄。要点名他作文里的哪句话时，直接把原句引出来就行，不要写“你提到”三个字。\n"
            "3. 这个问题必须逼他解释（具体指什么／为什么／凭什么／这中间差哪一步），"
            "不要问“是否……”“是不是……”这类答“是／否”就能应付过去的问题，"
            "也不要问“是 A 还是 B”这种让他二选一的问题。\n"
            "4. 回复说完就完了，不要附任何标注、括号说明、JSON 或记号。）"
        )
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
    return head + asked + focus + opener_line + (
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
        "5. 不许用“你提到”“我想问一下”“能否具体说明”“换句话说”“从某种程度上”这类套话开头。"
        "**作文里的原句可以引，但只能当这一问的靶子**（写成：你在作文里写的「……」），"
        "不许拿它当寒暄式的开场。\n"
        "6. **这一问要落回他自己的作文**：他这句如果只是泛泛讲道理、没提自己写的东西，"
        "你就挑出他作文里的那一句，就着那一句问——把他拽回自己的文章。"
        "**不许连续两轮都在抽象概念上打转**：离开这篇作文的追问，对他改这篇作文没用。\n"
        "7. 回复说完就完了，不要附任何标注、括号说明、JSON 或记号。）")


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
   - 环顾式：追问例子与观点之间凭什么成立，为什么这个例子能证明他的观点；
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
WRAPUP_HINTS = ("这组问题到这儿", "聊到这儿", "这组聊完", "已经聊完", "咱们就到这",
                "就聊到这儿", "这组问题就到这里", "本组聊完")


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
{topic_block}七个断裂点（next 只能从这七个词里选一个原样填）：
- 关键词解读偏差：**他对题目里关键词的理解偏了**——不是跑题，是解读不到位。
  典型：题目"学以成人"，他把"成人"理解成生理成熟，题目要的是学问+人格；
  题目"功用"，他理解成"有用"，材料讲的是不可替代的价值。
  **如果他作文的核心词跟材料里说的不是一回事，就是这个点。**
- 主张模糊：他核心的那句话本身就含糊，关键词含义不清、主张范围不明；
- 担保断裂：他摆了例子或名言，但说不上这些材料凭什么能支持他的观点；
- 支撑薄弱：他的理由背后缺少更根本的依据，或者只孤零零举了一个例子；
- 限定缺失：他把话说得太满，没交代什么条件下成立、什么情况下不成立；
- 反驳缺席：他从没想过有人会反对，也没交代反方会怎么说；
- 元认知反思缺失：上面几处他都已经用自己的话说清楚了（哪怕不够严谨），该收尾让他回看了。

请判断两点：
1. covered —— 学生**已经在回答里用自己的话说清楚**的断裂点（他给出了解释、让步或反例就算，哪怕不完美；只是重申观点、只是给词下定义、只是又举一个例子，不算）。
2. next —— **这一轮最该追的那一个**断裂点。不要选 covered 里的，**也不要再选前几轮已经反复追过的**——同一个点追两轮他还答不上来，就换下一个点，别原地打转。
{kw_rule}
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

# 阶段 0（审题）的收尾下限，比阶段一小：审题只聊概念辨析，来回太少就收
# 会让这一阶段变成走过场（实测学生第一句就被收、后面在原地打转）。
MIN_TURNS_STAGE0 = 2


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


def judge_point(essay, dialogue, asked_points=None, topic="", material="",
                first_round=False):
    """判定这一轮该追哪个断裂点。返回 POINT_ORDER 里的一个词，失败返回 ""。

    asked_points：前面几轮已经追过的断裂点。必须告诉它，否则它会在同一个点上
    反复选（实测：学生答完"限定缺失"，它还接着选"限定缺失"，追问原地绕）。

    topic/material：作文题目与材料。**2026-10-03 加**——之前这个函数只拿到作文
    和对话历史，压根不知道题目要求什么，所以"学生对题目关键词的理解偏了"
    这类问题永远不会被判出来。题目是"学以成人"、学生把"成人"理解成生理成熟，
    工具却一路在追"例子凭什么支持观点"——**追得越勤，偏得越远**。

    first_round：本轮是不是这一组的第一次追问。关键词解读偏差**只在第一次
    有意义**（一旦开始追论证，就回不去了），所以只在第一轮参与判定。

    返回"元认知反思缺失"时，同时意味着该收尾（见 /chat 里的处理）。
    """
    # 注意：dialogue 为空**不能**直接返回""——开场那一句还没有对话记录，
    # 实测那样会让开场的 point 恒为 None，「关键词解读偏差」永远出不来
    # （AI 于是直接去追"立德指什么"，而真正该问的是"学"和"成人"读准没）。
    if not ZHIPU_API_KEY or not essay:
        return ""
    asked = "、".join([p for p in (asked_points or []) if p])
    # 题面块：没有题目就不给——**不猜**。猜出来的题意不可靠，还会污染数据
    # （许总 2026-10-03 定：学生没填题目就跳过这个诊断，只做原来六项）。
    topic_block = ""
    kw_rule = ""
    if topic and topic.strip():
        topic_block = "【这道作文题的题目】" + topic.strip() + "\n"
        if material and material.strip():
            topic_block += "【题目下的材料／写作要求】\n" + material.strip() + "\n"
        topic_block += "\n"
        if first_round:
            kw_rule = ("**这一轮是第一次追问**：先判断他对题目关键词的理解对不对——"
                       "如果他作文里那个核心词的意思跟题目／材料里说的不是一回事，"
                       "就选「关键词解读偏差」（这个点必须在第一次追，"
                       "一旦开始追论证就回不去了）。理解没问题才选其他六个。\n")
        else:
            kw_rule = ("关键词解读偏差只在第一次追问时判；这一轮不要选它。\n")
    else:
        kw_rule = ("【注意】学生没有填题目，你不知道他要写什么。"
                   "**不要**猜题目、**不要**选「关键词解读偏差」——"
                   "只在他写出来的论证内部判断。\n")
    prompt = JUDGE_POINT_TEMPLATE.format(
        essay=(essay or "")[:1200],
        asked=("【前面几轮已经追过的断裂点】" + asked + "\n"
               "（这些点他要是在回答里已经给了自己的说法，就算补上了；"
               "但别因为它们被追过就当它们补上了——还是要看他到底说清楚没有。"
               "没补上的话可以再追，但要换个更小的切口。）\n") if asked else "",
        topic_block=topic_block,
        kw_rule=kw_rule,
        dialogue=("（这是这一组追问的第一句，之前还没有对话。）\n" + dialogue[:2000]
                  if not (dialogue or "").strip()
                  else dialogue[:2000]))
    try:
        raw = call_zhipu(JUDGE_POINT_SYSTEM, [("user", prompt)],
                         json_mode=True, temperature=0)
    except Exception:
        return ""
    obj = parse_json_obj(raw)
    if not obj:
        return ""
    nxt = str(obj.get("next") or "").strip()
    for p in POINT_ORDER:
        if p in nxt:
            # 没题目、或者不是第一轮，关键词解读偏差一律不算
            if p == "关键词解读偏差" and not (topic and topic.strip() and first_round):
                continue
            return p
    return ""


DEMO_STRATEGY_CYCLE = STRATEGY_ORDER


# ---- 防代写：生成后确定性检查（2026-10-03） ----
# 动机：竞品实测第 4 轮，学生放弃后它直接说"你可以进一步说明……比如书本带来
# 认知框架、实践带来价值体认"——把学生该想的内容自己说了，违背它自己的
# "不代写"声明。提示词里禁过很多次，glm-4-flash 照写不误（跟"你提到"一个道理，
# 是训练先验），所以只能在生成后做确定性检查。
# 判据：出现"替他把话说出来"的话术特征就判违规，重新生成（限次数）。
VIOLATION_PATTERNS = [
    # 一、AI 主动提供材料的话术
    r"你可以(这样|这样写|写|加|举|用|分)[^？]{0,24}例子",
    r"比如(举)?个?例子[^？]{0,8}[：:]",                   # "比如：爱因斯坦……"
    r"我可以(给你|为你|帮你)(写|提供|列)",
    r"以下是(一个|几个)[^？]{0,10}(例子|素材|范文)",
    # 带引号的直接引用（无论中文引号还是英文引号，都是"把话替他说出来"）
    r"你可以用[“\"][^”\"]{2,20}[”\"]",
    r"建议(你)?[^\n]{0,14}(用|写|举|加)[^\n]{0,10}[\"“][^\"”]{2,24}[\"”]",
    r"(就像|正如|比如说)(爱因斯坦|居里夫人|司马迁|苏武|荀子|孔子|钱学森|屠呦呦)[^？]{0,40}[。！？]",
    # 典型的"我来给你"话术
    r"我(来)?给你(列|举|举出|提供|找)[^\n]{0,10}(例子|素材|材料|范文)",
    r"我(来)?给(你)?(举|列)[^\n]{0,6}(个|几|两)[^\n]{0,6}例子",
    r"比如[：:][^\n]{0,4}(爱因斯坦|居里夫人|司马迁|苏武|荀子|孔子|钱学森|屠呦呦|居里)",
    r"(建议|推荐)(你)?(在|于)[^\n]{0,10}(用|举|写)[^\n]{0,10}(例子|素材|材料|人物)",
    # 人物名 + 例子/素材出现在同一句里，就是 AI 在替他挑材料
    r"(爱因斯坦|居里夫人|司马迁|苏武|荀子|孔子|钱学森|屠呦呦|欧阳修|苏轼)[^\n]{0,14}(的例子|这个例子|的事|的故事)[^\n]{0,30}[。！？]",
    # 二、AI 自造材料（竞品第 4 轮的原话形态：把因果直接替他讲出来）
    r"带来[^，。？]{0,6}认知框架",
    r"(建议|可以)(先)?(把|用)[^。？]{0,16}(写|说|讲)成",
    # 三、AI 直接把概念的定义说出来了（求助回合最常见的破功方式）。
    # 实测：学生说"你直接告诉我吧"，它答"精湛学识可能指的是他们在生化领域的
    # 专业知识和技能"——这就是替他把答案说了，学生不用再想。
    r"(可能|应该|大概|其实|就是|指的是|意思是|所谓)[^。？]{2,24}(指的是|是指|意思是|就是|即为)[^。？]{2,40}",
    r"(就是|便是|即)[^。？]{2,20}(的意思|的含义)",
    r"所谓[“\"][^”\"]{2,16}[”\"][，,]?[^。？]{0,6}(是|指)",
]
# 例外：这些是**引用他自己写的内容**，属于合法抓手，不能误伤
_SELF_QUOTE_OK = ("你在作文里写", "你自己写的", "你刚才说", "你提到", "你的作文",
                  "你在文中", "你写的", "你能在作文中找到", "这句话", "这句")


# 求助回合专用的"给答案"形态（比常规追问严格）。
# 必须定义在 check_no_ghostwriting **之前**（函数体里引用它）。
HELP_ANSWER_PATTERNS = [
    # 直接替学生定义概念："X 指的是……" "X 就是……"
    # 注意 "可能是指""应该是说"这种贴得很近的形态（实测漏过一次）
    r"(可能|应该|大概|其实|就是|指的是|意思是|所谓)[^。？]{0,6}(指的是|是指|意思是|即为|是)",
    r"你(说的|写的|提到的)[^。？]{0,12}[“\"][^”\"]{2,14}[”\"][^。？]{0,6}(就是|指的是|意思是)",
    # "你可以从……入手" 这种把路径也替他铺好的
    r"你可以从[^。？]{2,30}(入手|开始|出发)",
    r"(试着|尝试)(着)?具体(化|说清|描述)",
    # AI 自己抛提示性例子："比如它是不是……" —— 例子必须学生自己举，
    # AI 一给例子，边界就成了它划的（审题阶段实测高频）。
    r"[，,。]\s*比如[，,]?\s*(它|他|这|那)?(是|算|要|得|会)",
    r"比如[，,]?\s*(它|他|这|那)(是|算|要|得|会)不",
]


def check_no_ghostwriting(text, help_mode=False):
    """检查 AI 有没有替学生把论证说出来。返回违规话术的片段，没违规返回 ""。

    误伤的代价（把合法的抓手判成代写）比漏检小，所以只在**特征很明显**时判违规：
    要求命中的是"AI 主动提供材料"的话术，引用学生自己写的不算。

    help_mode=True 时（求助回合）标准更严：学生正在说"我不知道、直接告诉我"，
    这时候任何"替他定义概念"的话都是重灾区——**学生的诉求没被满足，他会觉得
    被敷衍**。实测这里最容易破功，所以额外查一组"给答案"的形态。
    """
    if not text:
        return ""
    body = text
    for pat in VIOLATION_PATTERNS:
        m = re.search(pat, body)
        if not m:
            continue
        # 命中的片段如果同时是在引用学生自己的话，放行
        ctx = body[max(0, m.start() - 40):m.end() + 10]
        if any(k in ctx for k in _SELF_QUOTE_OK):
            continue
        return m.group(0)
    if help_mode:
        for pat in HELP_ANSWER_PATTERNS:
            m = re.search(pat, body)
            if not m:
                continue
            ctx = body[max(0, m.start() - 40):m.end() + 10]
            if any(k in ctx for k in _SELF_QUOTE_OK):
                continue
            return m.group(0)
    return ""


# 审题收尾（阶段 0）专查的代写形态。
# 收尾回合在其他检查里是豁免的（梳理对话时引用学生的话不算代写），
# 但审题收尾有它自己的红线：**笔记必须学生自己写**。实测它会顺手把
# 三行笔记的省略号填上内容（"核心概念是学与成人，它不是……"），
# 还会替学生下定义（"'成人'则是指成为一个有品德、有修养的人"）——
# 那样学生交上来的笔记就是 AI 写的，这一组数据直接作废。
_STAGE0_WRAP_GHOST_RE = re.compile(
    r"[“\"][^”\"]{2,14}[”\"]\s*(?:则)?(?:指的是|就是指|指的就是|是指|的意思是)"
    r"|(?:所谓|也就是说)[^。？\n]{0,12}(?:是|指)"
    r"|核心概念(?:是|就是)[^。？\n，,]{2,}"
)


def _stage0_wrap_ghost(text):
    """审题收尾里有没有"替学生下定义 / 替他填笔记"。返回命中片段，没有返回 ""。"""
    if not text:
        return ""
    m = _STAGE0_WRAP_GHOST_RE.search(text)
    return m.group(0) if m else ""


# 一回合最多允许几个问句。超过就是"三问过载"——学生不知道该答哪个，
# 通常挑最好答的那个搪塞，追问等于白问。允许 2 个：一个主问 + 一个贴身的限定追问
# （"兼听指什么？是仅仅指多听吗？"这种还读得通），到第 3 个就散了。
MAX_QUESTIONS_PER_TURN = 3


def count_questions(text):
    """数这一条回复里有几个问句（中英文问号都算）。"""
    if not text:
        return 0
    return text.count("？") + text.count("?")


def trim_to_one_question(text):
    """兜底：把连珠炮式的追问砍成第一个问句。

    只在"重生成两次还是问了一堆"时动刀——截断会丢内容，但比甩三四个问题给学生强。
    截完太短（<15 字）就放弃，免得切出一个光秃秃的残句。
    """
    if not text:
        return text
    pos = min([i for i in (text.find("？"), text.find("?")) if i >= 0] or [-1])
    if pos < 0:
        return text
    kept = text[:pos + 1].strip()
    return kept if len(kept) >= 8 else text


# 阶段 0 的断点判定：这一轮该追概念辨析的哪一处。
# 为什么要单独判定而不是关键词数点：许总已定「审题对话纳入研究编码」，
# 编码要能落到 STAGE0_POINTS 这五个维度之一，就必须有一致的判定依据。
# 沿用阶段一的老办法：短提示词独立调用、temperature=0、返回不了就留空由研究者人工判。
JUDGE_S0_SYSTEM = (
    "你是教学对话分析助手，负责判断一段高中作文「审题对话」中 AI 这一轮该追问什么。"
    "只输出一个 JSON 对象（json），不要输出任何解释文字，也不要用代码块包裹。"
)

JUDGE_S0_TEMPLATE = """学生在做**审题**，还没写作文。AI 正在帮他把题目读懂、把概念定准。

【题目】
{topic}
{material}

【这组对话已经进行到这里】
{dialogue}

【学生这一句】
{last_user}

请判断 AI 这一回合最该追的是哪一处（next 只能从下面五个词里选一个原样填）：
- 概念未界定：题目里那个关键概念他没有给出定义，或给的是另一个意思；
- 概念外延不明：他能说清是什么，但说不出"不是什么"、哪些东西看着像其实不是；
- 概念关系未辨：题目里两个概念的关系（手段与目的／必要条件／并列）没搞清；
- 题意误读：他理解错了题目在问什么，或只盯着材料某一句；
- 审题已清晰：概念、边界、关系他都用**自己的话说清楚了** → 该收束，请他写审题笔记。

**判定要宽，别拿教科书标准卡高中生**（许总 2026-10-03 定）：概念本来就没有唯一正确的表述，
他只要**大意和方向对**就算说清楚了——不要求精准定义、不要求用术语、不要求面面俱到。
他用自己的话说出"这个概念大概指什么、跟题目怎么扣上"，就该判「审题已清晰」。
反过来说：**不要因为"他没能给出严谨定义""说得不够完整"就判他没说清而继续追问**——
那是把审题变成考词典，学生只会越答越没底。

判断时优先追"还没说清楚的那一处"，不要因为前面已经聊过就重复。
注意：如果他只是在重申同一个意思、并且方向已经对了，不要再判"概念未界定"——
换个角度追一遍也是原地打转。真的没别的好问了，就判「审题已清晰」。

输出格式：{{"next": "概念外延不明"}}"""


def judge_stage0_point(topic, material, dialogue, last_user):
    """判定审题对话这一轮该追哪一处。返回 STAGE0_POINTS 里的一个词，失败返回 ""。

    返回"审题已清晰"即表示该收束（见 /chat 里的处理）。
    """
    if not ZHIPU_API_KEY or not dialogue:
        return ""
    mat = ("\n【题目下的材料】\n" + material.strip()) if (material or "").strip() else ""
    prompt = JUDGE_S0_TEMPLATE.format(
        topic=(topic or "(学生没写题目)")[:300],
        material=mat[:800],
        dialogue=dialogue[-1600:],
        last_user=(last_user or "")[:400])
    try:
        raw = call_zhipu(JUDGE_S0_SYSTEM, [("user", prompt)],
                         json_mode=True, temperature=0)
    except Exception:
        return ""
    obj = parse_json_obj(raw)
    if not obj:
        return ""
    nxt = str(obj.get("next") or "").strip()
    for p in STAGE0_POINTS:
        if p in nxt:
            return p
    return ""


# 阶段 0 收尾判据（判定失败时的保守兜底）：学生是否把"是什么/不是什么"说出来了。
# 为什么要确定性判断而不是判定调用：这是流程性判断（他有没有做这件事），
# 用关键词更稳、也省一次 API；判错的代价只是收早或收晚，不影响数据性质。
# 【2026-10-03 放宽】许总定：概念**大意和方向对就行**，本来就没有唯一正确表述。
# 旧阈值（≥25 字且命中 2 个标记词）实测把"诸子百家对'人'有不同定义，我该明确一种
# 符合当下社会要求的'人'"这种**方向完全正确**的回答判为未说清，导致该收不收。
_STAGE0_MARKERS = ("不是", "不算", "不包括", "区别", "边界", "算不算", "而是指",
                   "反例", "比如", "只是", "而非", "并不", "不能算", "不构成", "意思是", "指的是")


def _stage0_concepts_clear(user_text):
    t = (user_text or "")
    if len(t) < 20:
        return False          # 太短，多半只是敷衍
    # 命中 1 个标记词（说到"不是什么/区别/意思是"）即可；
    # 说到两个或以上说明他确实在辨析，直接算清晰。
    return sum(1 for k in _STAGE0_MARKERS if k in t) >= 1


# ---- 求助回合：识别与回应（2026-10-03） ----
# 问题来源：真实 API 实测，学生连说三次求助（"你直接告诉我""你还没回答我"
# "能给我个例子吗"），系统三次都当没听见——因为 stage==1 分支完全按断裂点推进，
# 学生那句话被当作"对上一问的回答"，不作为"诉求"被识别。
# 理论依据：Wood/Bruner/Ross (1976) 脚手架六功能之「受挫控制」；
#           van de Pol 等 (2010)「应变性」——支架强度须随学生受阻状态浮动。

# 确定性前置规则：只处理"抱怨没被回答"这一类，特征极明显，不需要模型。
# 为什么加这层：实测 judge_help_request 对「你还没回答我。我真的不知道 X」
# 这类"抱怨+不会"的混合句**稳定误判成 direct**（连测 3 次全是 direct）——
# 句子里的"我真的不知道"把它带偏了。而这类句式恰恰是最典型的 repeat，
# 判错的代价是它按"回补上一轮"的方式去处理一个"要答案"的诉求。
# 规则优先、模型兜底：特征明显的用规则，剩下的才交给模型。
REPEAT_COMPLAINTS = (
    "你没回答", "你还没回答", "你还没有回答", "你根本没回答", "你没理",
    "你还没理", "你根本没说", "你还没说", "你没讲", "你还没讲",
    "我刚不是说了", "我刚才不是说了", "我刚说了", "我说了你",
    "你没听到", "你没看", "你绕开", "你躲", "你一直在绕", "别绕",
    "你没有正面", "你没正面", "答非所问", "你根本没讲",
)


def _is_repeat_complaint(text):
    """确定性地判「重复未答」。只认抱怨词，不做语义推断。"""
    t = (text or "")
    return any(k in t for k in REPEAT_COMPLAINTS)


# 直接求助的确定性词表。分两类：
#   要东西 = 明确在向 AI 要答案／材料
#   承认不会 = 明确说自己没招了（但要成句，单独的"不知道"是敷衍作答）
_DIRECT_ASKS = ("你直接告诉我", "直接告诉我", "告诉我吧", "你告诉我", "帮我写",
                "给我写", "帮我看一下", "给个开头", "给个例子", "举个例子",
                "给我个例", "给点素材", "给点材料", "提供素材", "能不能给",
                "你能不能直接", "帮我看看怎么", "我不知道该怎么写", "不知道怎么写")
_DIRECT_GIVEUP = ("我不太会", "我真的不会", "我完全不会", "我卡住了", "我卡在",
                  "没思路", "我想不出来", "我写不出来", "我不敢写", "无从下手",
                  "太难了", "好难", "我放弃", "我懵了", "我没头绪", "没想法")


def _is_direct_help(text):
    """确定性地判「直接求助」。要求"要东西"或成句的"承认不会"，
    避免把敷衍作答（单独的"不知道"、两字回答）误判成求助。"""
    t = (text or "")
    if any(k in t for k in _DIRECT_ASKS):
        return True
    if len(t) >= 8 and any(k in t for k in _DIRECT_GIVEUP):
        return True
    return False


JUDGE_HELP_SYSTEM = (
    "你是教学对话分析助手，负责判断学生在苏格拉底式追问对话里这一句是不是在求助。"
    "只输出一个 JSON 对象（json），不要输出任何解释文字，也不要用代码块包裹。"
)

JUDGE_HELP_TEMPLATE = """这是一段高中议论文写作的追问对话。AI 扮演"只问不答"的追问者。

【最近几轮对话】
{dialogue}

【学生这一句】
{last_user}

请判断学生这一句属于哪一类：

1. "repeat"：**他在抱怨你没理他上一次的问题**。特征是他提到了"上一次""刚才""还没"这类时间指向，
   或者明确说"你没回答""你没理我""我问的是刚才那个"。
   例子：「你还没回答我」「你还没说」「我刚不是说了吗」「你根本没讲」「你绕开我的问题了」
   「你问的那个我没答」

2. "direct"：**他在向你要东西**——要答案、要例子、要材料，或者说自己不会、卡住了、太难了。
   例子：「你直接告诉我」「给我一个开头」「举个例子」「我不太会」「我不知道」「这个好难」
   注意：如果他这句话里**同时**有"你没回答我"这种抱怨，算 repeat（他在抱怨，
   他要的不是新答案，是把上一个问题说清楚）。

3. "none"：**不是求助**。他是在回答你上一个问题（哪怕答得短、含糊、不对题）。
   特别注意：只是回答很短（"是手段""不知道"）算 none，不算 direct——那是敷衍作答；
   第一次问某个概念是什么意思，也不算求助，那是在回应你的追问。

判断要保守：拿不准就判 none。只有他**明确在要东西**或**明确在抱怨你没理他**，才算 direct / repeat。

输出格式：{{"help": "none"}}"""


def judge_help_request(last_user, dialogue_tail=""):
    """判断学生这一句是不是求助。返回 "none" / "direct" / "repeat"。

    必须放在 judge_point **之前**：实测中只要判定了断裂点、就近约束就会把
    追问方向绑死，学生说什么都影响不了它——那就还是"装聋"。

    两层：确定性规则先看「重复未答」（特征明显、模型实测会误判），
    其余交给模型；模型也判 direct 时，再用直接求助的词表兜一层。
    """
    if not last_user:
        return "none"
    if _is_repeat_complaint(last_user):
        return "repeat"
    # 直接求助的确定性词表：这些也是明确特征，不必浪费一次判定调用。
    # 但要排除"只是回答很短"的情况（"不知道"单独出现时是敷衍作答，不是求助），
    # 所以要求句子里还有"要东西"或"承认不会"的表达。
    if _is_direct_help(last_user):
        return "direct"
    if not ZHIPU_API_KEY:
        return "none"
    prompt = JUDGE_HELP_TEMPLATE.format(
        last_user=last_user[:400],
        dialogue=(dialogue_tail or "")[-800:])
    try:
        raw = call_zhipu(JUDGE_HELP_SYSTEM, [("user", prompt)],
                         json_mode=True, temperature=0)
    except Exception:
        return "none"
    obj = parse_json_obj(raw)
    if not obj:
        return "none"
    h = str(obj.get("help") or "none").strip().lower()
    return h if h in ("direct", "repeat") else "none"


def build_help_nudge(help_type, focus_point="", prev_question="", last_user="",
                     textbook_hints=None, prev_questions=None):
    """求助回合的就近约束。direct 与 repeat 的回应结构不同，要分开写。

    两种都守同一条红线：**给抓手，不给答案**；抓手只能来自"他自己写过的"
    或"教材篇目级线索"，AI 不得现造材料。
    """
    quote = (last_user or "").strip()
    if len(quote) > 120:
        quote = quote[:120] + "……"
    head = ("（以上是你们的对话。学生刚才说的是：“" + quote + "”\n"
            "**他不是在回答你的问题，他是在向你求助。**（本轮只针对这一件件事）\n")
    # 把前几轮说过的抓手原样摊开：实测它会在连续几轮里复读同一段
    #（"你可以从这句话里挑出两三项…或者先挑最明显的一项"连着两轮几乎一字不差），
    # 光禁"不许重复"压不住，得把原句摆给它看。
    asked = ""
    if prev_questions:
        asked = ("\n【你已经用过下面这些抓手——这一轮**必须换一个**，"
                 "换个句子、换个角度、换更小的一步都可以，但别把它们再说一遍】\n"
                 + "\n".join("· " + q for q in prev_questions) + "\n")
    common = (
        "\n【红线——违反这一条就等于替他写】\n"
        "1. **绝对不能给出他想要的那个答案**：不要定义概念、不要写开头或段落、"
        "不要举例证、不要报出课文内容。\n"
        "2. **必须给他一个具体抓手**——他下一次能立刻接住、能说出话的那种。\n"
        "3. 抓手只能来自两处：①他自己作文里已经写下的句子或他自己刚说过的话；"
        "②教材的篇名（只提篇目，让他自己去回忆内容）。\n"
        "4. **不许只有安慰没有抓手**。安慰一句就够，剩下的话必须把他推回他的作文。\n"
        "5. 不要用“你提到”“我想了解一下”这类套话开场。\n"
    )
    if help_type == "repeat":
        target = ("（他抱怨的是**你上一个问题没答到他**——注意：不是要你给答案，"
                  "而是你上一轮问得太抽象、没落到他能接住的地方。）"
                  if prev_question else
                  "（他要的是你把上一个问题说清楚、说具体，不是要答案。）")
        asked_note = ""
        if prev_question:
            asked_note = ("\n【你上一轮实际问的是】“" + prev_question[:160] + "”\n"
                          "**这一回合必须和它不一样**：不许再把那个问法重说一遍、不许重复你上一轮"
                          "给过的抓手、不许再说“这个我确实没答到”之后就重复原来的内容。\n")
        return head + target + asked_note + (
            "\n按这个顺序说三件事：\n"
            "1. **先认一句错**（一句就够，不要反复道歉）："
            "「这个我确实没答到」／「是我问得太绕了」——你确实问得不够清楚，这是事实；\n"
            "2. **把上一个问题重新说一遍，这次要小得多、具体得多**，"
            "让他一看就知道该答什么（比如把一个抽象的大问题，换成从他作文里挑两三个词这种小任务）；\n"
            "3. **最后必须以一个问句收尾**，而且只问一个他能一口答上来的小问题。\n"
            "**这一回合绝对不许出现「X 指的是……」这种把答案说出来的话。**\n"
            "这一回合**不要**抛新的追问方向、不要再追别的断裂点，"
            "也**绝对不要**在这一回合收尾（他还没补上，不许收摊）。\n"
        ) + common
    # direct
    focus = ""
    if focus_point and focus_point in POINT_DESC:
        focus = ("**这一轮仍是在追同一个点——" + focus_point + "**，但难度要降下来：\n"
                 + POINT_DESC[focus_point] + "\n"
                 "他现在答不上来，所以别再抛那个大问题。\n")
    tb = ""
    if textbook_hints:
        tb = ("\n" + textbook.format_textbook_hints(textbook_hints) + "\n")
    return head + focus + (
        "\n按这个顺序说三件事：\n"
        "1. **一句安抚**（只一句）：承认这个点确实难、卡在这儿很正常。"
        "不许说“你很有潜力”“你已经很好了”这种空话；\n"
        "2. **给一个具体抓手**：把他自己作文里已经写下的、跟这个点相关的那句话指出来"
        "（或按上面的教材线索提一个篇目），让他看出答案其实就在他自己写的东西里。"
        "**只指路，不解释**——你一说清楚，答案就变成你给的了；\n"
        "3. **然后把问题缩到他答得上**：不要再问“这个概念是什么意思”这种大问题，"
        "要问一个具体的、能一口答上来的小问题——"
        "比如让他从自己写过的那句话里挑出两三项、或者先说其中最明显的一项。"
        "**最后必须以一个问句收尾。**\n"
        "**这一回合绝对不许出现「X 指的是……」这种把答案说出来的话。**\n"
        "这一回合**不要**收尾（他还没补上，不许收摊），"
        "也**不要**换成另一个断裂点。\n"
    ) + tb + common


def build_stage0_help_nudge(help_type, focus_point="", prev_question="", last_user="",
                            prev_questions=None):
    """阶段 0（审题）的求助回合——必须和写作轮那套分开写。

    为什么不能沿用 build_help_nudge（2026-10-03 实测踩到，许总当场指出）：
    · 它让学生"看自己作文里已经写下的那句话"——可审题阶段学生**根本还没写作文**，
      实测 AI 于是对着一个还没动笔的人说"你作文里有没有提到过类似的问题？
      比如你之前写过的某个观点"。牛头不对马嘴。
    · 它的第一步是"一句安抚"（承认这个点确实难、卡在这儿很正常）。审题时学生说
      "什么叫 X"根本不是受挫，是**没听懂 AI 上一句话**。实测学生答得方向完全正确，
      AI 反倒回他"这个确实挺难的，遇到这样的问题很正常，咱们慢慢来"——安慰得莫名其妙。
    · 它的第 2 步允许"引教材、给抓手"，而审题阶段的红线是**一个字都不解释**。

    所以审题的求助只做一件事：**把刚才那个问题，换成一个大白话的、小得多的问题重问一遍。**
    """
    quote = (last_user or "").strip()
    if len(quote) > 120:
        quote = quote[:120] + "……"
    head = ("（以上是审题对话的记录。学生刚才说的是：“" + quote + "”\n"
            "**他不是在回答你的问题，他是在说「我没听懂你刚才问的那个词／那句话」。**"
            "（这一回合只处理这件事，不追新方向）\n")
    prev = ""
    if prev_question:
        prev = "\n【你上一轮实际问的是】“" + prev_question[:160] + "”\n"
    focus = ""
    if focus_point and focus_point in STAGE0_POINT_DESC:
        focus = ("\n【这一轮依然要解决这一点】" + focus_point + "："
                 + STAGE0_POINT_DESC[focus_point] + "\n")
    asked = ""
    if prev_questions:
        asked = ("\n【你前面已经问过这些——这一轮必须换一个，别再把它们说一遍】\n"
                 + "\n".join("· " + q for q in prev_questions) + "\n")
    return head + prev + focus + asked + (
        "\n按这个顺序说两件事：\n"
        "1. **一句大白话的过渡**，把话说成是自己的问题（例如「是我刚才问得太绕了」），"
        "一句就够；\n"
        "2. **把刚才那个问题问得更小、更口语，但必须还落在这道题上**——"
        "他听不懂的是那个词，就把问题缩到这道题里最具体的一处（题目里的某两个字、"
        "材料里的哪一句话、或者他觉得难的那个词本身），用平时说话的口气问他，"
        "让他随口就能接一句。\n"
        "\n【红线——违反任何一条，这一回合就白做了】\n"
        "1. **绝对不许解释**：不许解释那个词是什么意思，不许解释材料里写了什么，"
        "不许出现「所谓……就是指……」「X 指的是……」「它的意思是……」这类话。\n"
        "2. **绝对不许安慰**：他一没说自己不会、二没受挫，"
        "不要说「这个确实挺难的」「遇到这样的问题很正常」「慢慢来」「别急」。\n"
        "3. **不许提他的作文**：这是审题阶段，他现在**还没写作文**，"
        "不要说「你作文里提到过……」「你以前写过的……」这类话。\n"
        "4. **不许跑到题目外面去**：不要举跟这道题无关的生活类比"
        "（实测它会讲「比如你去超市扫码支付」「比如图书馆借阅」，然后让学生辨析那个跟作文毫无关系的词"
        "——审题就变成了逻辑练习课）。你举的任何例子、用的任何说法，都必须落在这道作文题上。\n"
        "5. 只问一个问题，**第一句话就是你的问题**，不要铺垫、不要分析、不要列清单。\n"
        "6. 这一回合**不要收尾**，也不要换成另一个追问方向。"
    )


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
                       help_type="", topic="", material="", prev_question="",
                       stage0_wrap=False):
    """返回 (给学生的正文, 引导策略, 是否该收尾)。
    prev_essay：第2轮时传入该生第1轮的原稿，用于"对照升格稿再诊断"。
    wrap_mode："" 正常追问 / "now" 这一回合收尾 / "after" 收尾后学生又多说的。
    is_opening：是否为本轮第一次追问（开场），此时不能说"针对他刚才那句"。
    help_type："direct" / "repeat" / ""（非求助）。非空时改走求助回合的回应结构。
    topic/material：作文题目与材料（老师需求①第一版）。
    stage0_wrap：阶段 0 审题对话的收尾。"""
    api_history = list(history)
    if stage == 0:
        # 阶段 0（审题）：没有作文，只有题目与材料
        system_prompt = build_stage0_system(topic, material)
    elif stage == 1:
        system_prompt = build_stage1_system(round_num, prev_essay, topic, material)
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
    # 当回合的即时指令放在对话最末尾（就近约束）。
    # 求助回合走另一套结构（build_help_nudge），它要求"不追新点、只解决求助"，
    # 与常规追问的约束直接冲突，不能混用。
    if help_type and stage == 0:
        # 审题阶段的求助走单独一套：不引教材（还没写作文，谈不上"联系课文"）、
        # 不提他的作文（他还没写）、不安慰、不解释。见 build_stage0_help_nudge。
        api_history = api_history + [("user", build_stage0_help_nudge(
            help_type, next_point, prev_question, last_user,
            collect_prev_questions(history)))]
    elif help_type:
        hints = None
        if help_type == "direct":
            # 教材线索：只在 direct（明确要材料）时给，repeat 问的是"你没答我"，跟教材无关
            q = (last_user or "") + " " + " ".join(
                (essay or "").split()[:120])
            try:
                hints = textbook.search_textbook(q, top_n=2)
            except Exception:
                hints = None
        api_history = api_history + [("user", build_help_nudge(
            help_type, next_point, prev_question, last_user, hints,
            collect_prev_questions(history)))]
    elif stage0_wrap:
        api_history = api_history + [("user", STAGE0_WRAP_BLOCK)]
    elif stage == 0:
        # 审题回合的 focus：把判定选中的那个概念维度绑死，否则模型会自己乱飘。
        focus = ""
        if next_point and next_point in STAGE0_POINT_DESC and next_point != "审题已清晰":
            focus = ("**这一轮只盯住这一点——" + next_point + "**："
                     + STAGE0_POINT_DESC[next_point] + "\n"
                     "整个回复就围绕它问一个问题，不要同时问别的。\n")
        # 前几轮问过的原话摊开，防止它换问法重复（和阶段一同一手法）
        asked = ""
        qs = collect_prev_questions(history)
        if qs:
            asked = ("\n【你前面已经问过下面这些，**这一轮不许重复**——换个说法再问一遍也算重复】\n"
                     + "\n".join("· " + x for x in qs) + "\n")
        api_history = api_history + [("user",
            "（以上是审题对话的记录。这是**第 " + str(turn_no) + " 轮追问**。"
            "他刚才说的是：“" + (last_user or "")[:160].strip() + "”）\n"
            + focus + asked +
            "要求：第一句话就是你的问题本身，只问一个问题；不要分析、不要列清单；"
            "不要用“你提到”“我想了解一下”这类套话开场；"
            "也不要评价他的理解对不对。\n"
            "**这一回合绝对不许出现「X 指的是……」这种把答案说出来的话**——"
            "概念必须由他自己界定。\n"
            "也**不许自己举例子**（「比如它是不是……」这种也不行）："
            "例子必须由他举，你一给例子，边界就变成你划的了。\n"
            "**不许把材料里的内容讲给他听**：既不要复述材料，也不要把材料里的观点替他数一遍"
            "（实测它会把材料里「儒家说……老庄说……墨家说……」整个讲给学生，等于替他读题）。"
            "材料要他自己读。\n"
            "**不许跑到题目外面去**：不要举跟这道题无关的生活类比"
            "（超市扫码、图书馆借阅这类），你说的话、举的东西都必须落在这道作文题上。\n"
            "**他没说「我不会」「太难了」「想不懂」之前，一个字都不要有安慰**："
            "不说「这个确实挺难」「遇到这样的问题很正常」「慢慢来」，第一句就是你的问题。\n"
            "另外：他现在还没写作文，**不要追问论点、论据、结构、分论点**，"
            "那不是这一阶段的事。")]
    else:
        api_history = api_history + [("user", build_turn_nudge(
            stage, is_opening, last_user, wrap_mode, turn_no,
            collect_prev_questions(history), next_point, point_repeat))]
    # 1) 正文：纯文本生成（不要求模型附带标注——实测它会漏、且会污染正文风格）
    text = ""
    if ZHIPU_API_KEY:
        try:
            text = call_zhipu(system_prompt, api_history)
        except Exception:
            text = ""
    if not text:
        raw = demo_respond(stage, history, last_user, round_num, is_opening, wrap_mode)
        return parse_meta(raw)
    # 万一模型自己附了标注（旧格式），先剥掉，确保不泄漏到学生端
    text, leaked_strategy, leaked_wrap = parse_meta(text)
    # 清掉开头套话（“你提到”“我想问一下”等），提示词压不住，生成后确定性替换
    # keep_leadin 只留给求助回合（那里安抚句是设计好的）；审题阶段不保留——
    # 它不是求助回合，"这个概念确实挺难把握的"这类铺垫只会让每轮看起来一样。
    text = clean_ai_tics(text, is_opening, keep_leadin=bool(help_type) and stage != 0)
    # 2) 防代写：命中就重生成（最多 2 次）。提示词禁不住，这是唯一的硬约束。
    #    求助回合标准更严（学生正要说"我不知道"，此时替他定义＝敷衍）。
    #    常规收尾不管——梳理对话时提到学生的句子不算代写，误伤代价更大。
    #    但**审题收尾要管**：它必须把笔记留给学生写，实测会替学生填好（见下）。
    if not (wrap_mode in ("now", "after")):
        for _ in range(2):
            if stage0_wrap:
                bad = _stage0_wrap_ghost(text)
                note = ("【上一条回复作废】你在收尾里替学生把话说了（%s）。\n"
                        "收尾只能做两件事：引他自己说过的话做梳理、请他写下**自己的**审题笔记。"
                        "定义必须由他给，笔记的每一行都必须由他写："
                        "不许把三行笔记的内容填好，不许出现「X 指的是……」「它的意思是……」"
                        "这种下定义的话。" % bad[:20])
            else:
                bad = check_no_ghostwriting(text, help_mode=bool(help_type))
                note = ("【上一条回复作废】它里面出现了替学生提供材料或直接给答案的话术（%s）。"
                        "学生答不出他的论证，是因为你没把话说出来；他说“我不知道”，是因为你真的说清楚了。"
                        "请重写这一回合：**只给抓手，不给答案**——"
                        "抓手只能是他自己作文里写下的句子，或者教材的篇名。"
                        "不许出现任何具体的定义、例子、名句、情节、范文。" % bad[:20])
                # 三问过载：提示词里禁过（"只问一个问题"），glm-4-flash 照犯不误，
                # 实测 10 组里 9 组出现——这正是我们批评竞品的毛病，不能只靠提示词。
                # 代写优先处理（性质更严重），没有代写再数问号。
                if not bad:
                    n_q = count_questions(text)
                    if n_q >= MAX_QUESTIONS_PER_TURN:
                        bad = "一次问了 %d 个问句" % n_q
                        note = ("【上一条回复作废】你这一句里连着问了 %d 个问题。\n"
                                "**一次只许问一个。** 学生看完三四个问句不知道该答哪个，"
                                "通常就挑最好答的那个搪塞过去，追问等于没问。\n"
                                "请重写：只留下**最要紧的那一个问题**，其余全部删掉，"
                                "不要留「换句话说」「再问一句」这种补问，也不要把同一个意思"
                                "换个说法再问一遍。整条回复就一问，说完就停。" % n_q)
            if not bad:
                break
            api_history = api_history + [("user", note)]
            try:
                text = call_zhipu(system_prompt, api_history)
            except Exception:
                break
            text, _, _ = parse_meta(text)
            text = clean_ai_tics(text, is_opening, keep_leadin=bool(help_type) and stage != 0)
    # 重生成两次还问一堆，就确定性截断到第一个问句——
    # 宁可短一点，也不能把三四个问题一起甩给学生（他只会挑最好答的那个）。
    if not (wrap_mode in ("now", "after")) and count_questions(text) >= MAX_QUESTIONS_PER_TURN:
        text = trim_to_one_question(text)
    # 收尾由后端判定驱动：这一回合就是收尾回合，策略固定记「回顾看」
    if wrap_mode in ("now", "after") or stage0_wrap:
        return text, "回顾看", True
    # 求助回合不该收尾：学生还没补上，判定说收也不收（防止他喊一声不会就跳过整组对话）
    if help_type:
        return text, "", False
    # 研究字段：独立判定本回合的引导策略与是否收尾
    strategy, judged_wrap = judge_turn(last_user, text)
    if not strategy and leaked_strategy:
        strategy = leaked_strategy
    wrap = bool(judged_wrap or leaked_wrap)
    # 判定失败时的保守兜底：正文里出现了明确的收尾话术，才认定已收尾
    if not wrap and not strategy and looks_like_wrapup(text):
        wrap, strategy = True, "回顾看"
        # 收尾轮也要过代写检查：它是在梳理学生说过的话，不该替他补内容。
        # （收尾由判定在生成之后决定，所以上面那次检查覆盖不到它。）
        bad = check_no_ghostwriting(text)
        if bad:
            retry = api_history + [(
                "user",
                "【上一条回复作废】它替学生补了内容（命中：%s）。"
                "收尾时**只梳理他自己说过的话**，不要替他解释、替他举例、"
                "也不要给他一个思考方向。重写这一回合。" % bad[:20])]
            try:
                t2 = call_zhipu(system_prompt, retry)
                t2, _, _ = parse_meta(t2)
                t2 = clean_ai_tics(t2, is_opening)
                if t2 and not check_no_ghostwriting(t2):
                    text = t2
            except Exception:
                pass
    return text, strategy, wrap


def ensure_round_conversation(student_id, essay="", topic="", material=""):
    """确保该生在当前轮次有一个对话：有则复用，无则按规则新建（含自动开场白）。
    返回 (conv_id, essay)。本轮**必须由学生提交本轮的作文**才能新建；未提交则返回 (None, '')。

    topic/material：作文题目与材料（老师需求①第一版）。只在该轮首次建对话时写入。
    """
    round_num = get_active_round()
    stage = ROUND_STAGE.get(round_num, 1)
    conn = get_db()
    conv = conn.execute(
        "SELECT id, essay FROM conversations WHERE student_id=? AND round=? "
        "ORDER BY created_at DESC LIMIT 1",
        (student_id, round_num)).fetchone()
    if conv:
        if not essay and conv["essay"]:
            essay = conv["essay"]
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
    cur = conn.execute(
        "INSERT INTO conversations(student_id, round, stage, essay, draft_type, "
        "topic, material, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (student_id, round_num, stage, base_essay, draft_type,
         topic, material, now_str()))
    conv_id = cur.lastrowid
    hist_rows = conn.execute(
        "SELECT role, content FROM messages m JOIN conversations c "
        "ON c.id=m.conversation_id WHERE c.student_id=? ORDER BY m.id",
        (student_id,)).fetchall()
    history_ctx = [(r["role"], r["content"]) for r in hist_rows]
    open_point = ""
    if stage == 1:
        # 开场这一句也要先判断裂点：不然「关键词解读偏差」永远出不来
        # （实测：开场走的是 is_opening 分支，不判定，point 恒为 None，
        #  AI 就会直接去追"立德具体指什么"，而真正该问的是"学"和"成人"读准没）。
        open_point = ""
        try:
            open_point = judge_point(base_essay, "", [],
                                     topic, material, first_round=True)
        except Exception:
            open_point = ""
        opening, strategy, _ = generate_assistant(1, history_ctx, essay=base_essay,
                                                  round_num=round_num,
                                                  prev_essay=prev_essay,
                                                  is_opening=True,
                                                  topic=topic, material=material,
                                                  next_point=open_point)
    else:
        opening = STAGE2_OPENING
        strategy = "回顾看"
    conn.execute(
        "INSERT INTO messages(conversation_id, role, content, strategy, point, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (conv_id, "assistant", opening, strategy, open_point or None, now_str()))
    conn.commit()
    conn.close()
    return conv_id, base_essay


# ---------------- 路由：学生端 ----------------
def _resume_stage0_conversation(student_id, force=False):
    """阶段 0（审题）进行中的对话 id；不存在则返回 None。

    审题必须能"刷新载回"：学生填完题目材料点「先做审题」，页面切到审题界面；
    只要他一刷新（手机端切后台回来就会刷新），若没有这条逻辑就掉回首页，
    刚才那一屏对话看不见了，他还以为白聊了。

    force=True：学生**主动**又点了一次「先做审题」，即使上次已写过审题笔记，
    也让他回到那条记录接着说（他想再琢磨一遍题目，这是允许的）。
    自动恢复（刷新）时不 force——写完笔记就该回正常首页，别把他一直按在审题界面。
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM conversations WHERE student_id=? AND round=0 "
            "ORDER BY id DESC LIMIT 1", (student_id,)).fetchone()
        if not row:
            return None
        if force:
            return row["id"]
        # 已经写过审题笔记＝审题结束，不该再把他拉回审题界面
        done = conn.execute(
            "SELECT 1 FROM messages WHERE conversation_id=? AND role='user' "
            "AND content LIKE ? LIMIT 1",
            (row["id"], "%【我的审题笔记】%")).fetchone()
        return None if done else row["id"]
    finally:
        conn.close()


@app.route("/")
def index():
    logged_in = "student_id" in session
    history_messages = []
    wrapped = False
    stage0 = False        # 是否正处于「阶段 0 · 审题对话」
    conv_id = None
    if logged_in and session.get("student_id"):
        student_id = session["student_id"]
        # 审题未结束 → 优先回到审题界面：这一屏只干一件事，别和写作表单混在一起
        if session.get("stage0"):
            conv_id = _resume_stage0_conversation(
                student_id, force=bool(session.get("stage0_force")))
            if conv_id:
                stage0 = True
            else:
                session.pop("stage0", None)
                session.pop("stage0_force", None)
        if not stage0:
            # 打开/刷新页面即按教师当前设定的轮次加载或新建对应对话，
            # 不必重新提交登录表单，避免“切了轮次却只显示一个”的困惑。
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
            # 收尾与否完全由 AI 判断：它以【策略：回顾看】收尾即视为本组结束。
            # 审题轮同一条判据——收尾后该让学生写他自己的审题笔记。
            wrapped = any(m["strategy"] == "回顾看" for m in history_messages
                          if m["role"] == "assistant")
        # 若 conv_id 为 None（本轮尚未提交作文），保留登录框让用户粘贴，history_messages 为空
    return render_template(
        "student.html",
        student_name=session.get("student_name", ""),
        round_num=get_active_round(),
        stage=get_active_stage(),
        stage0=stage0,
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
    topic = (request.form.get("topic") or "").strip()      # 作文题目（老师需求①）
    material = (request.form.get("material") or "").strip()  # 材料／写作要求
    if not name:
        return jsonify({"error": "请填写姓名"}), 400
    # 【2026-10-03 许总定】两个入口各自把材料交齐：
    # 点"开始对话"＝作文入口 → 必须有题目（诊断"解读不到位"的前提）＋作文；
    # 点"先做审题" ＝审题入口 → 必须有题目（/start_prompt 已校验）。
    # 为什么题目必填：断裂点判定要看题面才知道学生有没有读准关键词，
    # 没题面时它只能跳过这个诊断（不猜，猜出来的题意会污染数据）。
    if not topic:
        return jsonify({"error": "请填写作文题目——苏格拉底要靠题目才知道"
                                 "你这篇该不该扣题、你把关键词读准了没有。"}), 400
    if not material:
        return jsonify({"error": "请把题目的材料／写作要求贴上来"
                                 "（没有材料就贴写作要求）。"
                                 "有题面他才能判断你写的东西回应的是不是题目真正问的那件事。"}), 400
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
        if not essay:
            return jsonify({"error": "请先粘贴你本轮的作文，再开始对话"}), 400
        if len(essay) < MIN_ESSAY_LEN:
            return jsonify({
                "error": "请粘贴完整的作文（不少于 %d 字）。只写一句观点不够——"
                         "AI 要读完整篇，才能找到你论证里的问题。" % MIN_ESSAY_LEN}), 400

    # 按轮次分对话：复用 ensure_round_conversation（与首页刷新同一条逻辑）
    conv_id, essay = ensure_round_conversation(student_id, essay, topic, material)
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


@app.route("/start_prompt", methods=["POST"])
def start_prompt():
    """阶段 0 · 审题对话（老师需求①第二版）：只交题目＋材料＋立意，不用写作文。

    独立于 /start 的一条入口：阶段 0 存在 round=0，stage=0，与写作轮次分开，
    这样第1轮的前测作文仍然干净（审题过程不会污染第1轮的对话记录）。
    """
    name = (request.form.get("name") or "").strip()
    topic = (request.form.get("topic") or "").strip()
    material = (request.form.get("material") or "").strip()
    idea = (request.form.get("idea") or "").strip()
    if not name:
        return jsonify({"error": "请填写姓名"}), 400
    if not topic:
        return jsonify({"error": "请填写作文题目——审题得先有题目"}), 400
    # 审题入口也要求材料（许总 2026-10-03 定：两个入口各自把材料交齐）。
    # 审题最要紧的就是"题目在回应什么困境"，只看题目没有材料，等于凭空猜。
    if not material:
        return jsonify({"error": "请把题目的材料／写作要求贴上来——"
                                 "审题要看的正是材料里有什么矛盾、困境，"
                                 "只有题目四个字没法审。"}), 400
    conn = get_db()
    stu = conn.execute("SELECT id FROM students WHERE name=?", (name,)).fetchone()
    if stu:
        student_id = stu["id"]
    else:
        cur = conn.execute("INSERT INTO students(name, created_at) VALUES(?,?)",
                           (name, now_str()))
        student_id = cur.lastrowid
    conn.commit()
    # 阶段 0 每人只建一次：已有记录就直接载回，不重复建
    conv = conn.execute(
        "SELECT id FROM conversations WHERE student_id=? AND round=0 LIMIT 1",
        (student_id,)).fetchone()
    if conv:
        conv_id = conv["id"]
    else:
        cur = conn.execute(
            "INSERT INTO conversations(student_id, round, stage, essay, draft_type, "
            "topic, material, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (student_id, 0, 0, "", "审题", topic, material, now_str()))
        conv_id = cur.lastrowid
        opening = STAGE0_OPENING
        conn.execute(
            "INSERT INTO messages(conversation_id, role, content, strategy, created_at) "
            "VALUES(?,?,?,?,?)",
            (conv_id, "assistant", opening, "", now_str()))
        # 学生的初始立意作为第一条消息存下来（AI 要看到，才知道他自己的理解偏在哪）
        if idea:
            conn.execute(
                "INSERT INTO messages(conversation_id, role, content, strategy, created_at) "
                "VALUES(?,?,?,?,?)",
                (conv_id, "user", idea, "", now_str()))
        conn.commit()
    conn.close()
    session["student_id"] = student_id
    session["student_name"] = name
    session["conv_id"] = conv_id
    session["stage0"] = True
    # 主动进来的：即使上次已写过审题笔记，也让他回到那条记录接着说
    session["stage0_force"] = True
    conn2 = get_db()
    rows = conn2.execute(
        "SELECT role, content, strategy FROM messages WHERE conversation_id=? ORDER BY id",
        (conv_id,)).fetchall()
    conn2.close()
    return jsonify({"ok": True, "stage": 0, "round": 0, "topic": topic,
                    "history": [{"role": r["role"], "content": r["content"],
                                 "strategy": r["strategy"]} for r in rows]})


@app.route("/finish_prompt", methods=["POST"])
def finish_prompt():
    """结束审题、回到写作轮次。学生带着他自己的审题笔记进入第1轮。"""
    if "student_id" not in session:
        return jsonify({"error": "请先填写姓名开始对话"}), 400
    note = (request.form.get("note") or "").strip()
    if len(note) < 10:
        return jsonify({"error": "先把审题笔记写下来（不少于 10 字）——"
                                 "这一步得你自己写，我不替你填"}), 400
    sid = session["student_id"]
    conn = get_db()
    conn.execute("INSERT INTO messages(conversation_id, role, content, strategy, created_at) "
                 "VALUES(?,?,?,?,?)",
                 (session.get("conv_id"), "user", "【我的审题笔记】" + note, "", now_str()))
    conn.commit()
    conn.close()
    session.pop("stage0", None)
    session.pop("stage0_force", None)
    return jsonify({"ok": True, "next": "/"})


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
        "SELECT round, stage, essay, student_id, topic, material FROM conversations WHERE id=?",
        (conv_id,)).fetchone()
    round_num = conv["round"]
    stage = conv["stage"]
    essay = conv["essay"] or ""
    topic = conv["topic"] or ""
    material = conv["material"] or ""
    # 第2轮：取第1轮原稿做对照（每个来回都注入，保证全程都能对照原稿判断断裂点是否补上）
    prev_essay = get_prev_essay(conv["student_id"], round_num) if stage == 1 else ""
    # 取历史
    rows = conn.execute(
        "SELECT role, content, strategy, point FROM messages WHERE conversation_id=? ORDER BY id",
        (conv_id,)).fetchall()
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
    help_type = ""
    prev_question = ""
    stage0_wrap = False
    tail = ""
    # ---- 求助判定**必须最先做** ----
    # 踩过的坑：只要先判定了断裂点、就近约束就会把追问方向绑死，学生说什么都
    # 影响不了它——实测学生连说三次求助（"你直接告诉我""你还没回答我""举个例子"），
    # 系统三次都当没听见。所以求助判定排在 judge_point 之前。
    if not already_wrapped:
        tail = "\n".join(
            ("AI：" if r["role"] == "assistant" else "学生：") + (r["content"] or "")[:300]
            for r in rows[-6:])
        tail += "\n学生：" + user_text[:300]
        _h = judge_help_request(user_text, tail)
        # 【2026-10-03 根因修复】judge_help_request 判"不是求助"时返回的是字符串
        # "none"，而 Python 里 "none" 是**真值**。此前所有 `if help_type:` 判断因此
        # **全部成立**，后果是：
        #   · 写作轮每一轮都按"求助回合"模板生成（每轮一句安抚 + 拉学生自己的话 + 缩小问题），
        #     而不是正常追问；
        #   · keep_leadin=bool(help_type) 恒为 True，**安抚句清理器从来就没生效过**
        #     ——这就是长期困扰的"安慰模板 88%"的真正来源；
        #   · 审题轮同理（许总 10-03 截图里学生明明没求助，AI 却安慰他）。
        # 之前一直在补词表治症状，根因在这里：三值必须归一成空串。
        help_type = "" if _h in ("", "none") else _h
    if help_type == "repeat":
        # 重复未答：抓手给**上一轮**那个点，point 沿用上一轮。
        # 不消耗"同点已追两轮"的配额——他没补上，不是我追多了。
        prev_qs = [r["content"] for r in rows
                   if r["role"] == "assistant" and r["content"]]
        prev_question = prev_qs[-1] if prev_qs else ""
        last_pts = [r["point"] for r in rows if r["role"] == "assistant" and r["point"]]
        next_point = last_pts[-1] if last_pts else ""
        # 连续两个 repeat 都指向同一个点 → 强制换点，避免又变成原地打转
        if len(last_pts) >= 2 and last_pts[-1] == last_pts[-2]:
            for p in POINT_ORDER:
                if p == "元认知反思缺失" or p not in last_pts:
                    next_point = p
                    break
        wrap_mode = ""      # 求助回合一律不收尾
        point_repeat = 0     # 不消耗配额
    elif help_type == "direct":
        # 直接求助：不追新断裂点，但仍要知道"当前在追哪个点"——抓手要指向那里。
        # point 照常写入、照常计入"同点已追两轮"（防永久降档：降低的是这一回合
        # 的问题难度，不是整个对话的难度）。
        turns = len([r for r in rows if r["role"] == "user"]) + 1
        asked_points = [r["point"] for r in rows if r["point"]]
        # 求助回合不是"第一次追问"，关键词解读偏差不参与
        next_point = judge_point(essay, tail, asked_points, topic, material,
                                first_round=False)
        if next_point == "元认知反思缺失":
            next_point = asked_points[-1] if asked_points else ""
        wrap_mode = ""
        point_repeat = asked_points.count(next_point) if next_point else 0
    elif stage == 0:
        # 阶段 0（审题）：许总已定纳入研究编码，所以这一轮追什么要落到
        # STAGE0_POINTS 的某一维度上，判定说"审题已清晰"就收束产审题笔记。
        # 还要加一道下限：概念辨析至少来回两次才收，否则学生第一句就被收、
        # 审题变成走过场（实测第三轮已在原地打转，说明收得偏晚）。
        s0_turns = len([r for r in rows if r["role"] == "user"]) + 1
        s0_point = judge_stage0_point(topic, material, tail, user_text)
        # 【许总 2026-10-03 定：每个概念"基本是正确方向的想法"就行——概念本来就没有
        # 唯一正确的表述。所以兜底判据不再给判定结果打折扣：判定说"还没清晰"、
        # 但他的话里已经出现"不是/区别/意思是"这类辨析痕迹时，照收。
        # 旧写法是 `(not s0_point and _stage0_concepts_clear(...))`——要求判定**失败**
        # 才用兜底，于是判定一直答"概念外延不明"（他没举反例），对话就永远收不了
        # （实测四轮都没收，学生早就说对了）。
        clear = (s0_point == "审题已清晰") or _stage0_concepts_clear(user_text)
        if clear and s0_turns >= MIN_TURNS_STAGE0:
            stage0_wrap = True
        next_point = s0_point
    elif stage == 1 and not already_wrapped:
        turns = len([r for r in rows if r["role"] == "user"]) + 1
        # 已经追过哪些断裂点（内部推进用，不参与研究编码）
        asked_points = [r["point"] for r in rows if r["point"]]
        # 一次判定办两件事：这一轮追哪个断裂点 + 是否六个点都补上了（→ 该收尾）
        # first_round：有没有 AI 说过话（开场那一条不算"学生答过一次"）
        ai_told = any(r["role"] == "assistant" and r["content"] for r in rows)
        next_point = judge_point(essay, tail, asked_points, topic, material,
                                first_round=not ai_told)
        # 同一个断裂点最多追两轮。判定要是还想追第三轮，强制换一个没追过的点；
        # 都追遍了就收尾。这不是"追问上限"（不是到点就收），而是不让它在同一处反复磨——
        # 实测判定会连选同一处（学生已经举了例子，它还接着要例子），追问原地打转。
        if next_point in ("", "元认知反思缺失"):
            pass
        elif next_point == "关键词解读偏差" and asked_points.count(next_point) >= 1:
            # 这个点只在第一次有效，第二次就不该再追了——强制换点
            for p in POINT_ORDER:
                if p in ("元认知反思缺失", "关键词解读偏差") or p in asked_points:
                    continue
                next_point = p
                break
        elif asked_points.count(next_point) >= 2:
            for p in POINT_ORDER:
                if p == "元认知反思缺失" or p in asked_points:
                    continue
                next_point = p
                break
            else:
                next_point = "元认知反思缺失"
        point_repeat = asked_points.count(next_point) if next_point else 0
        if next_point == "元认知反思缺失" and turns >= MIN_TURNS_BEFORE_WRAP:
            wrap_mode = "now"
        elif not next_point and judge_wrap(tail, turns):
            # 追问方向判定失败时才回落用收尾判定兜底，省一次调用
            wrap_mode = "now"
    # 生成 AI 回复
    text, strategy, should_wrap = generate_assistant(
        stage, history, raw_user, essay, round_num, prev_essay, wrap_mode,
        next_point=next_point, point_repeat=point_repeat,
        help_type=help_type, topic=topic, material=material,
        prev_question=prev_question, stage0_wrap=stage0_wrap)
    # 求助回合的 point 记录口径：
    #   direct = 本回合追的点照常写入（计入配额，防永久降档）
    #   repeat = 沿用上一轮的点（他没补上，不是我追多了）——但仍要写，方便复核
    # 求助回合 strategy 留空：它不是六策略里的任何一种，是教学应变。
    conn.execute(
        "INSERT INTO messages(conversation_id, role, content, strategy, point, "
        "help_type, created_at) VALUES(?,?,?,?,?,?,?)",
        (conv_id, "assistant", text, strategy,
         next_point or ("元认知反思缺失" if wrap_mode == "now" else None),
         help_type or None, now_str()))
    conn.commit()
    conn.close()
    # 收尾状态：模型判定该收尾（末尾标注 @@收尾=是@@）、或此前已收尾
    wrapped = bool(should_wrap or already_wrapped or strategy == "回顾看")
    return jsonify({"reply": text, "strategy": strategy, "wrapped": wrapped,
                    "help": help_type})


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
        "SELECT s.name, s.sid, c.round, c.stage, c.draft_type, c.created_at AS conv_time, "
        "c.topic, c.material, "
        "m.role, m.strategy, m.content, m.created_at, m.help_type, m.point "
        "FROM messages m JOIN conversations c ON m.conversation_id=c.id "
        "JOIN students s ON c.student_id=s.id ORDER BY s.id, c.id, m.id").fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["姓名", "学号", "轮次", "稿次", "阶段", "作文题目", "材料/写作要求",
                     "对话开始时间", "角色", "使用/引导策略", "图尔明编码维度",
                     "本回合追的诊断点", "是否求助回合", "内容", "消息时间"])
    # 编码列：阶段一/二用策略映射（TOULMIN_MAP），阶段 0 审题用概念维度映射。
    # 求助回合（direct/repeat）不是六策略里的任何一种，策略列留空，单独一列记录，
    # **不参与编码**——它只用于描述性统计，断裂点编码仍由研究者人工按图尔明六要素完成。
    help_label = {"direct": "是·要答案/说不会", "repeat": "是·未被回答"}
    for r in rows:
        if r["role"] != "assistant":
            code = ""
        elif r["stage"] == 0:
            code = STAGE0_POINT_MAP.get(r["point"] or "", "")
        else:
            code = TOULMIN_MAP.get(r["strategy"] or "", "")
        writer.writerow([r["name"], r["sid"] or "", r["round"], r["draft_type"] or "",
                         r["stage"], r["topic"] or "", r["material"] or "",
                         r["conv_time"],
                         "学生" if r["role"] == "user" else "AI",
                         r["strategy"] or "", code, r["point"] or "",
                         help_label.get(r["help_type"] or "", ""),
                         r["content"], r["created_at"]])
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
