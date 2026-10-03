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
out = os.path.join(BASE, "preview_start.html")
with open(out, "w", encoding="utf-8") as f:
    f.write(html)

checks = [
    ("含「带 * 的为必填项」说明", "的为必填项" in html),
    ("旧写法「（选填）」label 已清掉", "学号（选填）" not in html),
    ("旧写法「（<b>必填</b>）」已清掉", "（<b>必填</b>）" not in html),
    ("两个按钮各带需求清单", html.count('class="req-note"') == 2),
    ("分组徽标齐全", html.count('badge badge-both') == 1 and html.count('badge badge-chat') == 1),
    ("悬停高亮函数已注入", "function hlFields(" in html),
    ("缺失字段定位函数已注入", "function focusField(" in html),
]
print("status:", resp.status_code, "-> ", out)
for name, ok in checks:
    print(("  ok   " if ok else "  FAIL ") + name)
print("全部通过" if all(ok for _, ok in checks) else "有项目未通过，请检查")
