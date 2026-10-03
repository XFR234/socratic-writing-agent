# -*- coding: utf-8 -*-
"""教材素材索引（老师需求②）：只用于**给篇目级线索**，不给课文内容。

红线（对标竞品第 4 轮的失败）：它把"书本带来认知框架、实践带来价值体认"
这种学生该自己想的内容直接说了出来，等于代写。所以本模块只负责
"他学过的《劝学》里有没有讲过……"这一层提示，绝不把素材内容送到学生眼前。

检索用纯 Python 关键词匹配，不用向量库——PythonAnywhere 免费账户跑不动，
GLM-4-Flash 的上下文也塞不下几本教材。索引本身只有几十条，够用。
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "data", "textbook_index.json")

_INDEX_CACHE = None


def load_textbook_index():
    """读取教材素材索引。文件不存在或损坏时返回空列表（功能降级，不影响主流程）。"""
    global _INDEX_CACHE
    if _INDEX_CACHE is None:
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            _INDEX_CACHE = data if isinstance(data, list) else []
        except Exception:
            _INDEX_CACHE = []
    return _INDEX_CACHE


# 检索用的停用词：这些词在议论文里到处都是，命中它们等于没命中
STOPWORDS = {
    "的", "了", "是", "在", "和", "与", "也", "就", "都", "而", "及", "或",
    "一个", "我们", "他们", "自己", "这个", "那个", "什么", "怎么", "为什么",
    "可以", "不能", "不是", "就是", "因为", "所以", "如果", "这样", "那样",
    "学生", "作文", "议论文", "观点", "论证", "问题", "意思", "认为", "觉得",
}


def _tokenize(text):
    """极简切词：中文按 2-gram + 整词，英文数字按词。够用且无需依赖。"""
    text = (text or "")
    out = set()
    # 中文双字片段（覆盖"积累""坚持""家国"这类两词素）
    for i in range(len(text) - 1):
        frag = text[i:i + 2]
        if frag in STOPWORDS:
            continue
        out.add(frag)
    # 单字（补充双字没覆盖到的，如"钱""权"）
    for ch in text:
        if "一" <= ch <= "鿿" and ch not in STOPWORDS:
            out.add(ch)
    return out


def search_textbook(query, top_n=3):
    """按关键词重合度找最相关的课文。

    返回 [(篇目, 册次, 适用话题, 核心思想), ...]，最多 top_n 条。
    匹配权重：适用话题 > 核心思想 > 素材点——因为学生最容易用"我想讲什么话题"
    来检索，而素材点里的措辞太偏，他不会用同样的词去搜。
    """
    index = load_textbook_index()
    if not index or not query:
        return []
    q = _tokenize(query)
    if not q:
        return []
    scored = []
    for item in index:
        topics = item.get("适用话题") or []
        think = item.get("核心思想") or ""
        mat = item.get("素材点") or ""
        title = item.get("篇目") or ""
        # 话题命中权重最高：他几乎总是用"话题"来检索
        s = 3 * len(q & _tokenize(" ".join(topics)))
        s += 2 * len(q & _tokenize(think))
        s += 1 * len(q & _tokenize(mat))
        s += 4 if (title and (title[:2] in query)) else 0
        if s > 0:
            scored.append((s, item))
    scored.sort(key=lambda x: -x[0])
    out = []
    for s, item in scored[:top_n]:
        out.append({
            "篇目": item.get("篇目", ""),
            "册次": item.get("册次", ""),
            "适用话题": item.get("适用话题", []),
            "核心思想": item.get("核心思想", ""),
        })
    return out


def format_textbook_hints(hints):
    """把检索结果拼成注入就近约束的一段话。

    注意这里的措辞：只给篇目、册次、话题，**不给素材点、不给名句、不给课文内容**。
    素材点和名句留在索引里但**不注入**——它们是"抓手"层面的东西，
    给了学生就不用自己去回忆了，正是我们要防的代写。
    """
    if not hints:
        return ""
    lines = []
    n_zy = 0
    for h in hints:
        t = "、".join(h.get("适用话题", [])[:5])
        bk = h.get("册次", "")
        # 自读课文（册次里标了·自读）学生未必精读，提示时语气要弱一些——
        # 宁可少提，也不要引他其实没读过的课文，反而显得 AI 在编。
        if "自读" in bk:
            bk = bk.replace("·自读", "") + "（自读课文）"
            n_zy += 1
        lines.append("《%s》（%s）——可能对得上：%s" % (h.get("篇目", ""), bk, t))
    extra = ""
    if n_zy:
        extra = ("\n　　· 其中标了「自读课文」的，他多半只是略读——"
                 "提这一篇时用「你读过的那篇里有没有讲过……」，"
                 "**不要**用「你学过的《X》里……」这种笃定说法。")
    return (
        "【教材线索（可选用，但有硬约束）】\n"
        + "\n".join("· " + x for x in lines)
        + "\n用法：**只提篇目，让他自己去回忆课文里有没有相关内容。**\n"
          "　　· 可以说：「你读过的《X》里，有没有讲过跟这有关的？你记不记得它是怎么说的？」\n"
          "　　· **不许**把素材点的内容、名句、或者任何具体情节讲出来——那是他该自己想出来的。\n"
          "　　· 跟当前追问无关，就当没这条线索，别硬拉。"
        + extra
    )
