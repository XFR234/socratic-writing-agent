# -*- coding: utf-8 -*-
"""线上冒烟：直接打 https://xfr.pythonanywhere.com 的真实路由。

为什么要有它：本地测试全绿 ≠ 线上就是那个版本。PythonAnywhere 上要 `git pull`
再 Reload 才生效，只推到 GitHub 不算上线。本地测试（sim/test_switch_topic.py 等）
用的是 demo 模式、独立库，看不到"线上跑的到底是哪个版本"。

本脚本用真实账号名走一遍许总自测的路径，覆盖两件事：
  1. 版本校验：登录后的顶栏徽标必须带《当前题目》（3d8717f 的特征）
  2. 换题重开：审题入口 / 作文入口 换一道题 → 返回 need_switch → 确认后另开一条

会真实调用 GLM-4-Flash（/chat 与 /start 建对话时生成开场），约 15-20 秒一次。
数据写进**线上库**，用的是专门的测试姓名，正式收数据前要清库。

用法：/usr/bin/python3 sim/live_check.py
"""
import http.cookiejar
import json
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://xfr.pythonanywhere.com"
NAME = "许老师自测D"

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
checks = []


def ck(name, ok, extra=""):
    checks.append((name, ok))
    print(("  ok   " if ok else "  FAIL ") + name + (("  | " + extra) if extra else ""))


def get(path):
    with op.open(BASE + path, timeout=60) as r:
        return r.read().decode("utf-8")


def post(path, data):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=body)
    with op.open(req, timeout=120) as r:
        return r.read().decode("utf-8")


def badge(html):
    """抓顶栏 stage-badge 的文字（去掉标签）。"""
    m = re.search(r'<span class="stage-badge">(.*?)</span>', html, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""


def jpost(path, data):
    return json.loads(post(path, data))


def jget(path):
    return json.loads(get(path))


T0 = time.time()


def step(msg):
    print("\n=== %s  (+%.0fs) ===" % (msg, time.time() - T0))


# ---------------------------------------------------------------- 审题入口
step("1. 审题：提交《学以成人》")
d = jpost("/start_prompt", {
    "name": NAME, "topic": "学以成人",
    "material": "有人说，学习是一个人从自然人走向社会人的过程；也有人说，学习只是为了谋生。"
                "对此你有怎样的思考？请写一篇文章。",
    "idea": "学习让我们成为真正的人。",
})
ck("首次审题成功", bool(d.get("ok")), str(d.get("error", "")))
ck("没有误报换题", not d.get("need_switch"))

step("2. 刷新首页：顶栏应写着在审哪道题")
h = get("/")
b = badge(h)
print("     顶栏：%r" % b)
ck("顶栏带上了当前题目《学以成人》", "学以成人" in b,
   "（若 FAIL：线上还是旧版，去 PA 的 Bash 跑 git pull 再 Reload）")

step("3. 追一轮：AI 应围着「学」「成人」问，不是别的题")
r = jpost("/chat", {"message": "我觉得就是要多读书，读得越多越像个人。"})
reply = r.get("reply", "")
print("     AI：%s" % reply[:150].replace("\n", " "))
ck("有回复", bool(reply))
ck("回复只问了一个问题（无三问过载）", reply.count("？") + reply.count("?") <= 2,
   "问号数=%d" % (reply.count("？") + reply.count("?")))
ck("没有安慰套话", not re.search(r"没关系|别急|已经很(好|不错)|你的感受", reply))

step("4. 换一道题再提交《兼听则明》（不点确认，先问一句）")
d2 = jpost("/start_prompt", {
    "name": NAME, "topic": "兼听则明",
    "material": "古人云：兼听则明，偏信则暗。请据此谈谈你的看法。",
    "idea": "要多听别人的意见。",
})
ck("返回 need_switch", bool(d2.get("need_switch")))
ck("提示里带回了上一道题", d2.get("old_topic") == "学以成人", str(d2.get("old_topic")))
ck("提示里带回了新题", d2.get("new_topic") == "兼听则明", str(d2.get("new_topic")))
ck("未确认时不新建", not d2.get("ok"))

step("5. 点「另开一篇」确认（new_conv=1）")
d3 = jpost("/start_prompt", {
    "name": NAME, "topic": "兼听则明", "new_conv": "1",
    "material": "古人云：兼听则明，偏信则暗。请据此谈谈你的看法。",
    "idea": "要多听别人的意见。",
})
ck("确认后新建成功", bool(d3.get("ok")), str(d3.get("error", "")))

step("6. 刷新：顶栏应切到新题")
h = get("/")
b = badge(h)
print("     顶栏：%r" % b)
ck("顶栏现在是《兼听则明》", "兼听则明" in b)

step("7. 在新那条里追一轮")
r2 = jpost("/chat", {"message": "兼听就是多听几个人的意见，听得越多越好。"})
reply2 = r2.get("reply", "")
print("     AI：%s" % reply2[:150].replace("\n", " "))
ck("追的是「兼听」这道题", bool(re.search(r"兼听|偏信|听", reply2)))
ck("没有串回上一道题", "成人" not in reply2)

step("8. 往期对话：两条都在")
lst = jget("/history_list").get("list", [])
topics = [x.get("topic") for x in lst]
print("     往期题目：%s" % topics)
ck("两条审题对话都能回看", topics.count("学以成人") >= 1 and topics.count("兼听则明") >= 1)

# ---------------------------------------------------------------- 作文入口
step("9. 换一个干净会话，走作文入口：提交《学以成人》作文")
cj.clear()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
E1 = ("学以成人，是古人留给我们的训诫。人并非生而知之者，唯有通过学习，"
      "才能脱离蒙昧，成为一个真正意义上的人。学习让我们懂得礼义，"
      "知道什么该做什么不该做；学习让我们看清世界，不再被表象迷惑。"
      "古有孔子韦编三绝，今有无数寒门学子苦读，皆为成人二字。"
      "然而今天也有人把学习当成谋生的工具，只求一纸文凭，"
      "这便失了学习的本意。我以为，学习的终极目的，是成己成人。" * 3)
d4 = jpost("/start", {
    "name": NAME + "·作文", "sid": "TEST", "essay": E1,
    "topic": "学以成人",
    "material": "有人说，学习是一个人从自然人走向社会人的过程；也有人说，"
                "学习只是为了谋生。对此你有怎样的思考？",
})
ck("首次提交作文成功", bool(d4.get("ok")), str(d4.get("error", "")))
ck("有开场白", bool(d4.get("history")))
h = get("/")
b = badge(h)
print("     顶栏：%r" % b)
ck("顶栏显示《学以成人》", "学以成人" in b)

step("10. 换题再交一篇《兼听则明》（不确认）")
E2 = ("兼听则明，偏信则暗，这是古人留给我们的智慧。一个人若只听一面之词，"
      "便如盲人摸象，难见全貌。齐桓公不计一箭之仇，任用管仲，"
      "遂成春秋霸业；唐太宗虚心纳谏，魏征直言，乃有贞观之治。"
      "反观那些拒谏饰非的君王，终致国破家亡。故曰：为政者当广开言路，"
      "为学者亦当博采众长。我以为，兼听是一种胸襟，更是一种能力。" * 3)
d5 = jpost("/start", {
    "name": NAME + "·作文", "sid": "TEST", "essay": E2,
    "topic": "兼听则明", "material": "古人云：兼听则明，偏信则暗。",
})
ck("返回 need_switch", bool(d5.get("need_switch")))
ck("带回了上一篇题目", d5.get("old_topic") == "学以成人", str(d5.get("old_topic")))

step("11. 确认另开（new_conv=1）")
d6 = jpost("/start", {
    "name": NAME + "·作文", "sid": "TEST", "essay": E2,
    "topic": "兼听则明", "material": "古人云：兼听则明，偏信则暗。",
    "new_conv": "1",
})
ck("确认后新建成功", bool(d6.get("ok")), str(d6.get("error", "")))
h = get("/")
b = badge(h)
print("     顶栏：%r" % b)
ck("顶栏切到《兼听则明》", "兼听则明" in b)
lst = jget("/history_list").get("list", [])
topics = [x.get("topic") for x in lst]
print("     往期题目：%s" % topics)
ck("两篇作文对话都能回看", topics.count("学以成人") >= 1 and topics.count("兼听则明") >= 1)

# ---------------------------------------------------------------- 汇总
bad = [n for n, ok in checks if not ok]
print("\n" + "=" * 60)
print("共 %d 项，通过 %d，失败 %d" % (len(checks), len(checks) - len(bad), len(bad)))
if bad:
    for n in bad:
        print("  FAIL  " + n)
    print("\n若第 2 步（顶栏带题目）失败，说明线上还没同步——")
    print("PA 的 Bash 里跑：cd ~/socratic-writing-agent && git pull")
    print("再去 Web 页点 Reload。")
    sys.exit(1)
print("线上全流程通过 ✅")
