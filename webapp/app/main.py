"""FastAPI 入口：列表页地址录入、抓取归并、结果展示、页面代理、按区间补采。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .backfill import run_batch
from .crawler import REQUEST_TIMEOUT, USER_AGENT, crawl
from .merger import merge_announcements
from .storage import Storage

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="招标延期核对助手")

storage = Storage()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, url: str | None = None) -> HTMLResponse:
    context = {
        "request": request,
        "entry_url": url or "",
        "report": None,
        "results": None,
    }
    if url and url.strip():
        report = crawl(url.strip())
        context["report"] = report
        context["results"] = merge_announcements(report.announcements)
    return templates.TemplateResponse("index.html", context)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------- 按日期区间补采 ----------

def _valid_date(text: str) -> bool:
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return True


@app.get("/backfill", response_class=HTMLResponse)
def backfill_home(request: Request, error: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        "backfill.html",
        {"request": request, "batches": storage.list_batches(), "error": error},
    )


@app.post("/backfill")
def backfill_create(
    entry_url: str = Form(...),
    date_from: str = Form(...),
    date_to: str = Form(...),
) -> RedirectResponse:
    """新建（或并入已有）补采批次，并立即执行一次采集。"""
    entry_url = entry_url.strip()
    parsed = urlparse(entry_url)
    error: str | None = None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        error = "目录地址必须是 http(s) 开头的完整 URL。"
    elif not (_valid_date(date_from) and _valid_date(date_to)):
        error = "发布日期区间格式应为 YYYY-MM-DD。"
    elif date_from > date_to:
        error = "开始日期不能晚于结束日期。"
    if error:
        return RedirectResponse(f"/backfill?error={quote(error)}", status_code=303)

    batch, _created = storage.create_or_get_batch(entry_url, date_from, date_to)
    run_batch(storage, batch["id"])
    return RedirectResponse(f"/backfill/{batch['id']}", status_code=303)


@app.get("/backfill/{batch_id}", response_class=HTMLResponse)
def backfill_detail(request: Request, batch_id: int, all: int = 0) -> HTMLResponse:
    batch = storage.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return templates.TemplateResponse(
        "backfill_detail.html",
        {
            "request": request,
            "batch": batch,
            "pages": storage.get_pages(batch_id),
            "announcements": storage.get_announcements(batch_id, in_range_only=not all),
            "show_all": bool(all),
            "date_missing": storage.date_missing_urls(batch_id),
        },
    )


@app.post("/backfill/{batch_id}/run")
def backfill_run(batch_id: int) -> RedirectResponse:
    """续采：从目录首页重走分页，补齐缺失的公告。"""
    if storage.get_batch(batch_id) is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    run_batch(storage, batch_id)
    return RedirectResponse(f"/backfill/{batch_id}", status_code=303)


def _validate_proxy_url(target: str) -> str | None:
    parsed = urlparse(target)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return target


@app.get("/view", response_class=HTMLResponse)
def view(url: str) -> Response:
    """代理抓取目标 HTML 并重写链接，供宿主机浏览器查看容器内站点的公告。"""
    target = _validate_proxy_url(url)
    if not target:
        return HTMLResponse("仅支持 http(s) 地址", status_code=400)
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": USER_AGENT}) as client:
            resp = client.get(target)
    except httpx.HTTPError as exc:
        return HTMLResponse(f"代理抓取失败：{exc}", status_code=502)

    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type.lower():
        return Response(content=resp.content, media_type=content_type or "application/octet-stream")

    soup = BeautifulSoup(resp.text, "html.parser")
    # 页面内资源走 /asset，跳转链接走 /view
    for tag, attr in (("a", "href"), ("link", "href"), ("script", "src"), ("img", "src")):
        for element in soup.find_all(tag, attrs={attr: True}):
            absolute = urljoin(str(resp.url), element[attr])
            if urlparse(absolute).scheme not in ("http", "https"):
                continue
            if tag == "a":
                element["href"] = f"/view?url={absolute}"
                element.attrs.pop("target", None)
            else:
                element[attr] = f"/asset?url={absolute}"

    note = soup.new_tag(
        "div",
        style="background:#fff7e6;border:1px solid #ffd591;padding:8px 12px;"
              "font-size:13px;color:#ad4e00;margin-bottom:12px;",
    )
    note.string = f"代理视图 · 原始地址：{resp.url}"
    if soup.body:
        soup.body.insert(0, note)
    return HTMLResponse(str(soup))


@app.get("/asset")
def asset(url: str) -> Response:
    target = _validate_proxy_url(url)
    if not target:
        return HTMLResponse("仅支持 http(s) 地址", status_code=400)
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": USER_AGENT}) as client:
            resp = client.get(target)
    except httpx.HTTPError as exc:
        return HTMLResponse(f"资源抓取失败：{exc}", status_code=502)
    return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/octet-stream"))
