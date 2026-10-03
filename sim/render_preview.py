"""把学生端开始页渲染成静态 HTML，用于本地肉眼核对版式（不连数据库、不调模型）。
用法：cd socratic-writing-agent && /usr/bin/python3 sim/render_preview.py
产物：preview_start.html（项目根目录）
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DB_PATH", "/tmp/preview_only.db")
os.environ.setdefault("ZHIPU_API_KEY", "")

import app as A  # noqa: E402

client = A.app.test_client()
resp = client.get("/")
html = resp.get_data(as_text=True)

# 预览专用：把后端接口换成假的，这样点按钮能真走到"校验 → 界面切换"，
# 但不会写库、不调模型。只注入到预览产物里，真实模板不受影响。
MOCK = """
<script>
/* ===== 预览专用 mock（不连后端、不写库、不调模型）===== */
(function () {
  const J = (o) => Promise.resolve({ json: () => Promise.resolve(o) });
  const AI_OPEN = '（预览示例回复）你在作文里写「学以成人」。我想先问一句：这里的“学”，'
    + '你指的是读书求知，还是也包括学做人这件事？';
  window.fetch = function (url, opt) {
    const u = String(url || '');
    if (u.indexOf('/start_prompt') === 0) {
      return J({ history: [{ role: 'assistant', content:
        '（预览示例）题目是「学以成人」。你先说说，“成人”这个“成”，'
        + '在你看来是长到十八岁就算，还是得做成点什么事才算？' }] });
    }
    if (u.indexOf('/start') === 0) {
      return J({ history: [{ role: 'assistant', content: AI_OPEN, strategy: '退一步' }] });
    }
    if (u.indexOf('/chat') === 0) {
      return J({ reply: '（预览示例）那我换个更小的问法：如果一个人读了很多书，'
        + '但对身边的人始终刻薄，按你的定义，他算不算“成人”了？', wrapped: false });
    }
    if (u.indexOf('/finish_prompt') === 0) { return J({ next: '/' }); }
    if (u.indexOf('/history_list') === 0) {
      return J({ list: [{ round: 1, stage: 1, draft_type: '原稿',
        created_at: '2026-10-03 10:00', conv_id: 1, preview: '（预览示例）学以成人……' }] });
    }
    if (u.indexOf('/history/') === 0) {
      return J({ essay: '（预览示例）学以成人，贵在……', draft_type: '原稿',
        history: [{ role: 'assistant', content: AI_OPEN }] });
    }
    return J({});
  };
})();
</script>
"""
html = html.replace("</body>", MOCK + "\n</body>")
out = os.path.join(BASE, "preview_start.html")
with open(out, "w", encoding="utf-8") as f:
    f.write(html)

checks = [
    ("含「带 * 的为必填项」说明", "的为必填项" in html),
    ("旧写法「（选填）」label 已清掉", "学号（选填）" not in html),
    ("旧写法「（<b>必填</b>）」已清掉", "（<b>必填</b>）" not in html),
    ("两个按钮各带需求清单", html.count('class="req-note"') == 2),
    ("「只有「开始对话」要填」徽标保留", html.count('badge badge-chat') == 1),
    ("「两个按钮都要填」徽标已删", "两个按钮都要填" not in html),
    ("第②项下方那段提示语已删", "题目里往往藏着" not in html),
    ("悬停高亮函数已注入", "function hlFields(" in html),
    ("缺失字段定位函数已注入", "function focusField(" in html),
]
print("status:", resp.status_code, "-> ", out)
for name, ok in checks:
    print(("  ok   " if ok else "  FAIL ") + name)
print("全部通过" if all(ok for _, ok in checks) else "有项目未通过，请检查")
