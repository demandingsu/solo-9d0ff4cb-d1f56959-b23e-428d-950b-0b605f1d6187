#!/usr/bin/env python3
"""示例站点服务器：静态文件 + 补采演示动态场景。

- 静态部分：serve 本目录下 generate_site.py 生成的 site/（与原 nginx 行为一致）；
- 动态部分：/backfill/ 下的目录与详情由内存状态驱动，用于演示
  “两次操作之间目录首页插入新公告（旧条目后移）”和“详情页偶发 500”：
    * 初始（第 1 批数据）：4 条公告分两页，notice_b01 在重置后的第一次抓取返回 500；
    * /backfill/_advance：插入 2 条新公告到首页，旧公告后移（第 2 批数据）；
    * /backfill/_reset ：回到第 1 批数据，并重新武装 notice_b01 的 500；
    * /backfill/_status：查看当前状态与请求计数（JSON）。
"""
from __future__ import annotations

import html
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

SITE_DIR = Path(__file__).resolve().parent / "site"
PORT = 80
PAGE_SIZE = 3

# (公告ID, 项目编号, 公告类型, 发布时间, 截止时间|None, 标题, 正文)
NOTICES_BASE = [
    ("notice_b01", "BF-2026-001", "招标公告", "2026-10-10 09:00:00", "2026-10-31 10:00:00",
     "职工食堂食材配送采购招标公告", "欢迎符合条件的供应商参加投标。"),
    ("notice_b02", "BF-2026-002", "招标公告", "2026-10-15 14:00:00", "2026-11-05 09:30:00",
     "办公大楼保洁服务采购招标公告", "本项目服务期一年，详见招标文件。"),
    ("notice_b03", "BF-2026-003", "更正公告", "2026-10-18 10:30:00", "2026-11-08 17:00:00",
     "会议系统升级改造采购更正公告", "因采购需求调整，投标截止时间延期至本公告载明的新时间。"),
    ("notice_b04", "BF-2026-004", "招标公告", "2026-10-20 08:30:00", "2026-11-10 14:00:00",
     "园区绿化养护服务采购招标公告", "欢迎合格供应商参与投标。"),
]
NOTICES_INSERTED = [
    ("notice_b05", "BF-2026-005", "招标公告", "2026-11-01 09:00:00", "2026-11-21 10:00:00",
     "档案库房恒温设备采购招标公告", "本条为两次采集之间新插入的公告。"),
    ("notice_b06", "BF-2026-006", "招标公告", "2026-11-03 11:00:00", "2026-11-25 09:30:00",
     "应急指挥车辆采购招标公告", "本条为两次采集之间新插入的公告。"),
]
FLAKY_ID = "notice_b01"  # 重置后第一次抓取返回 500，之后恢复


class DemoState:
    """演示场景的内存状态（容器重启即复位）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.stage = 1
        self.flaky_armed = True
        self.counts: dict[str, int] = {}

    def reset(self) -> None:
        with self._lock:
            self.stage = 1
            self.flaky_armed = True

    def advance(self) -> None:
        with self._lock:
            self.stage = 2

    def notices(self) -> list[tuple]:
        with self._lock:
            items = list(NOTICES_BASE) + (list(NOTICES_INSERTED) if self.stage >= 2 else [])
        return sorted(items, key=lambda n: n[3], reverse=True)

    def count(self, path: str) -> None:
        with self._lock:
            self.counts[path] = self.counts.get(path, 0) + 1

    def consume_flaky_failure(self) -> bool:
        """notice_b01 在重置后的第一次抓取返回 500；返回 True 表示本次应失败。"""
        with self._lock:
            if self.flaky_armed:
                self.flaky_armed = False
                return True
            return False

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "stage": self.stage,
                "flaky_armed": self.flaky_armed,
                "counts": dict(self.counts),
            }


STATE = DemoState()


def build_detail_page(notice: tuple) -> str:
    nid, project_no, notice_type, publish_time, deadline, title, body = notice
    rows = [
        ("项目编号", project_no),
        ("公告类型", notice_type),
        ("发布时间", publish_time),
    ]
    if deadline:
        rows.append(("投标截止时间", deadline))
    table = "\n      ".join(f"<tr><th>{k}</th><td>{html.escape(str(v))}</td></tr>" for k, v in rows)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="../../assets/style.css">
</head>
<body>
<div class="wrap">
  <p class="crumb"><a href="../list/page_1.html">返回公告列表</a></p>
  <h1>{html.escape(title)}</h1>
  <table class="fields">
      {table}
  </table>
  <div class="content">
    <p>{html.escape(body)}</p>
    <p>采购代理人请以本平台正式公告内容为准，按时完成投标文件递交。</p>
  </div>
</div>
</body>
</html>
"""


def build_list_page(page_no: int, page_items: list[tuple], has_next: bool) -> str:
    items_html = []
    for notice in page_items:
        items_html.append(
            f'    <li><a class="notice-link" href="../detail/{notice[0]}.html">'
            f'<span class="t">{html.escape(notice[5])}</span>'
            f'<span class="d">{notice[3]}</span></a></li>'
        )
    pager = (
        '<div class="pager">'
        f'<a class="next-page" href="page_{page_no + 1}.html">下一页</a>'
        "</div>"
        if has_next
        else '<div class="pager"><span class="disabled">下一页</span></div>'
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>补采演示目录 - 第{page_no}页</title>
<link rel="stylesheet" href="../../assets/style.css">
</head>
<body>
<div class="wrap">
  <h1>补采演示目录</h1>
  <ul class="notice-list">
{chr(10).join(items_html)}
  </ul>
{pager}
</div>
</body>
</html>
"""


def build_demo_index() -> str:
    snap = STATE.snapshot()
    stage = "第 2 批数据（首页已插入 2 条新公告，旧公告后移）" if snap["stage"] >= 2 else "第 1 批数据（4 条公告，2 页）"
    flaky = "已武装：notice_b01 下一次抓取将返回 500" if snap["flaky_armed"] else "已恢复：notice_b01 正常返回"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>补采演示场景</title>
<link rel="stylesheet" href="../assets/style.css">
</head>
<body>
<div class="wrap">
  <h1>补采演示场景（动态）</h1>
  <p>当前状态：{stage}；偶发故障：{flaky}。</p>
  <ul>
    <li>目录入口：<a href="list/page_1.html">/backfill/list/page_1.html</a>
        （在网页应用中填入 <code>http://example-site/backfill/list/page_1.html</code>）</li>
    <li><a href="_advance">插入新公告</a>：在目录首页插入 2 条新公告，旧条目后移；</li>
    <li><a href="_reset">重置场景</a>：回到第 1 批数据，并重新武装 notice_b01 的 500 故障；</li>
    <li><a href="_status">查看状态 JSON</a>。</li>
  </ul>
  <p>静态失败场景：
    <a href="../backfill-dates/list/page_1.html">日期缺失目录</a> ｜
    <a href="../backfill-broken/list/page_1.html">详情解析失败目录</a>
  </p>
  <p><a href="../index.html">← 返回站点首页</a></p>
</div>
</body>
</html>
"""


class Handler(SimpleHTTPRequestHandler):
    """动态路径优先，其余回退到静态文件。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def log_message(self, *args) -> None:  # 静默访问日志
        pass

    # -- 响应辅助 --
    def _send_html(self, content: str, status: int = 200) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    # -- 路由 --
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        STATE.count(path)
        if path in ("/backfill", "/backfill/"):
            return self._send_html(build_demo_index())
        if path == "/backfill/_reset":
            STATE.reset()
            return self._redirect("/backfill")
        if path == "/backfill/_advance":
            STATE.advance()
            return self._redirect("/backfill")
        if path == "/backfill/_status":
            return self._send_json(STATE.snapshot())
        if path.startswith("/backfill/list/page_"):
            return self._handle_list(path)
        if path.startswith("/backfill/detail/"):
            return self._handle_detail(path)
        return super().do_GET()

    def _handle_list(self, path: str) -> None:
        try:
            page_no = int(path.rsplit("page_", 1)[1].split(".")[0])
        except (ValueError, IndexError):
            return self._send_html("<h1>404</h1>", status=404)
        notices = STATE.notices()
        pages = [notices[i:i + PAGE_SIZE] for i in range(0, len(notices), PAGE_SIZE)]
        if not 1 <= page_no <= len(pages):
            return self._send_html("<h1>404</h1>", status=404)
        self._send_html(build_list_page(page_no, pages[page_no - 1], page_no < len(pages)))

    def _handle_detail(self, path: str) -> None:
        notice_id = Path(path).stem
        if notice_id == FLAKY_ID and STATE.consume_flaky_failure():
            return self._send_html(
                "<h1>500 Internal Server Error</h1><p>模拟的偶发故障，请稍后重试。</p>",
                status=500,
            )
        for notice in STATE.notices():
            if notice[0] == notice_id:
                return self._send_html(build_detail_page(notice))
        self._send_html("<h1>404</h1>", status=404)


def make_server(host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    server = make_server(host="", port=PORT)
    print(f"示例站点已启动：http://localhost:{PORT}（静态目录 {SITE_DIR}）")
    server.serve_forever()
