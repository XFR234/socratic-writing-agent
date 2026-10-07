#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把刚跑完的那一组对话过一遍红线，输出逐轮问题清单。

为什么要这个脚本：靠人眼看对话，容易只注意"说得好不好"，漏掉真正的硬伤
（安慰句、代写、标注泄漏、原地打转、不收尾）。这些都能用规则查，
查出来就是"确实有问题"，不是"我觉得"。

用法：
  python3 sim/check.py           # 检查当前模拟库里最后一组对话
"""
import os
import re
import sys
import json
import difflib

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM_DIR = os.path.join(BASE, "sim")
os.environ.setdefault("DB_PATH", os.path.join(SIM_DIR, "sim_conversations.db"))
sys.path.insert(0, BASE)
os.chdir(BASE)

from app import (app, get_db, REASSURANCE_RE, LEADIN_PHRASES, TIC_OPENERS,  # noqa: E402
                 EMPTY_PRAISE_RE, check_no_ghostwriting,
                 count_questions, MAX_QUESTIONS_PER_TURN)

# 锚定作文：这一问有没有落到学生自己的文章上（"你在作文里写的……"或直接出现"作文"）。
# 全组不出现＝AI 顺着抽象概念聊，这篇作文等于白交了（许总 10-07 实测指出的问题）。
ANCHOR_RE = re.compile(r"作文|你(?:在)?(?:这篇|那篇)?(?:文章|文中)|你写的")

# 研究字段泄漏：学生不该看到这些内部术语
LEAK_PATTERNS = [
    r"研究标注", r"【策略", r"策略[:：]", r"图尔明", r"图尔敏", r"Toulmin", r"toulmin",
    r"\bjson\b", r"\{\s*[\"']", r"断裂点", r"诊断点", r"判定", r"编码",
]
LEAK_RE = re.compile("|".join(LEAK_PATTERNS))


def load_last_conversation():
    st_path = os.path.join(SIM_DIR, "sim_state.json")
    conv_id = None
    if os.path.exists(st_path):
        with open(st_path, encoding="utf-8") as f:
            conv_id = json.load(f).get("conv_id")
    conn = get_db()
    try:
        if conv_id:
            row = conn.execute("SELECT id, round, stage, topic, material FROM conversations "
                               "WHERE id=?", (conv_id,)).fetchone()
        else:
            row = conn.execute("SELECT id, round, stage, topic, material FROM conversations "
                               "ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None, []
        msgs = conn.execute(
            "SELECT role, content, strategy, point FROM messages "
            "WHERE conversation_id=? ORDER BY id", (row["id"],)).fetchall()
        return row, msgs
    finally:
        conn.close()


def scan_reply(text, prev_text):
    """对一条 AI 回复做红线扫描，返回问题列表。"""
    issues = []
    if not text:
        return ["回复为空"]

    # 1) 安慰／铺垫
    if REASSURANCE_RE.search(text):
        issues.append("安慰句：「%s」" % REASSURANCE_RE.search(text).group(0)[:20])
    for p in LEADIN_PHRASES:
        if text.startswith(p) or ("，" + p) in text[:60]:
            issues.append("铺垫句：「%s」" % p[:16])
            break

    # 2) 套话开头
    for op in TIC_OPENERS:
        if text.startswith(op):
            issues.append("套话开头：「%s」" % op)

    # 3) 空泛表扬
    if EMPTY_PRAISE_RE.search(text):
        issues.append("空泛表扬：「%s」" % EMPTY_PRAISE_RE.search(text).group(0)[:16])

    # 4) 代写（替学生把论证说出来）
    bad = check_no_ghostwriting(text)
    if bad:
        issues.append("疑似代写：「%s」" % bad[:20])

    # 5) 研究字段泄漏
    m = LEAK_RE.search(text)
    if m:
        issues.append("内部术语泄漏：「%s」" % m.group(0))

    # 6) 一次问太多（三问过载——这是批评竞品的点，自己不能犯）
    q = count_questions(text)
    if q >= MAX_QUESTIONS_PER_TURN:
        issues.append("一次问了 %d 个问句（三问过载）" % q)

    # 7) 与上一轮高度相似（原地打转的观感信号）
    if prev_text and len(text) > 30:
        r = difflib.SequenceMatcher(None, prev_text, text).ratio()
        if r >= 0.55:
            issues.append("与上一轮相似度 %.0f%%（可能原地打转）" % (r * 100))

    # 8) 长度
    if len(text) > 260:
        issues.append("回复偏长（%d 字）" % len(text))
    return issues


def main():
    conv, msgs = load_last_conversation()
    if not conv:
        print("模拟库里没有对话。先跑：python3 sim/run.py start sim/cases/xx.txt")
        return

    print("=" * 68)
    print("对话 #%s ｜ 第 %s 轮 ｜ 阶段 %s ｜ 题目：%s"
          % (conv["id"], conv["round"], conv["stage"], conv["topic"] or "(无)"))
    print("=" * 68)

    ai_turns, points, prev = [], [], None
    wrapped = False
    for m in msgs:
        if m["role"] == "assistant":
            ai_turns.append(m)
            points.append(m["point"])
            wrapped = wrapped or (m["strategy"] == "回顾看")

    total = 0
    anchored = 0
    for i, m in enumerate(ai_turns):
        prev_text = ai_turns[i - 1]["content"] if i else None
        issues = scan_reply(m["content"], prev_text)
        tag = "第%d轮" % (i + 1)
        pt = ("  · 追的点：%s" % points[i]) if points[i] else ""
        if ANCHOR_RE.search(m["content"] or ""):
            anchored += 1
            pt += " 〔锚定作文〕"
        if issues:
            total += len(issues)
            print("\n[%s]%s" % (tag, pt))
            for s in issues:
                print("   ⚠ " + s)
            print("   原文：%s" % m["content"][:110].replace("\n", " "))
        else:
            print("\n[%s]%s  ✓" % (tag, pt))

    # 追问锚点是否在同一处打转
    seq = [p for p in points if p]
    runs, cur, n = [], None, 0
    for p in seq:
        if p == cur:
            n += 1
        else:
            if n >= 3:
                runs.append((cur, n))
            cur, n = p, 1
    if n >= 3:
        runs.append((cur, n))
    for p, n in runs:
        print("\n⚠ 「%s」连续追了 %d 轮（原地打转，这组数据会作废）" % (p, n))
        total += 1

    if not wrapped:
        print("\n⚠ 跑到最后也没收尾（学生不知道什么时候结束，也收不上笔记）")
        total += 1

    print("\n" + "-" * 68)
    print("AI 共 %d 轮，学生作答 %d 次，收尾：%s"
          % (len(ai_turns), len([m for m in msgs if m["role"] == "user"],),
             "是" if wrapped else "否"))
    print("锚定作文的轮次：%d / %d" % (anchored, len(ai_turns)))
    print("结论：" + ("全部通过 ✓" if total == 0 else "共 %d 处需要看" % total))
    print("（观感类问题可容忍；代写／泄漏／打转／不收尾这四类必须修）")


if __name__ == "__main__":
    main()
