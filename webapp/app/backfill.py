"""按发布日期区间补采公告：每次运行都从目录首页重走分页，按页原子提交。

关键语义：
- 续采一律从目录首页重新沿“下一页”遍历，不按旧页码跳过——两次操作之间
  目录可能插入新公告，旧条目会后移；已采公告靠详情 URL 识别并跳过，
  与它当前出现在第几页无关；
- 一页之内只要有详情抓取或解析失败，该页的记录与进度都不提交（整页回滚），
  但此前已成功提交的页面保留；
- 只有从首页一路完整采到目录末页（无下一页、无失败、无日期缺失、未触上限），
  批次才标记为“已补齐”，否则保留原因供页面展示。
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from .crawler import (
    MAX_DETAIL_PAGES,
    MAX_LIST_PAGES,
    REQUEST_TIMEOUT,
    USER_AGENT,
    _extract_list_links,
)
from .models import Announcement
from .parser import parse_detail
from .storage import Storage
from .utils import parse_time, same_origin

STATUS_COMPLETED = "completed"    # 已补齐
STATUS_INCOMPLETE = "incomplete"  # 未补齐


@dataclass
class RunOutcome:
    status: str = STATUS_INCOMPLETE
    reason: str | None = None      # 未补齐原因；已补齐时为 None
    visited_pages: int = 0         # 本次运行遍历的列表页数
    pages_committed: int = 0       # 本次运行提交（含更新）的列表页数
    new_records: int = 0           # 本次运行新增的公告记录数


def _record_from(ann: Announcement, batch: dict, list_url: str) -> dict:
    """把解析结果整理成入库记录，顺带计算日期解析与区间归属。"""
    parsed = parse_time(ann.publish_time)
    publish_date = parsed.date().isoformat() if parsed else None
    in_range = bool(publish_date and batch["date_from"] <= publish_date <= batch["date_to"])
    return {
        "url": ann.url,
        "project_no": ann.project_no,
        "title": ann.title,
        "notice_type": ann.notice_type,
        "publish_time": ann.publish_time,
        "publish_date": publish_date,
        "deadline": ann.deadline,
        "declares_extension": 1 if ann.declares_extension else 0,
        "date_ok": 1 if publish_date else 0,
        "in_range": 1 if in_range else 0,
        "list_page_url": list_url,
    }


def run_batch(storage: Storage, batch_id: int) -> RunOutcome:
    """对批次执行一次完整采集尝试（新建与续采同一条路：从首页重走分页）。"""
    batch = storage.get_batch(batch_id)
    if batch is None:
        raise KeyError(f"批次不存在: {batch_id}")
    entry_url = batch["entry_url"]
    collected = storage.collected_urls(batch_id)

    outcome = RunOutcome()
    reason: str | None = None
    visited: set[str] = set()
    queue = [entry_url]
    page_index = 0
    new_details = 0

    with httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        while queue:
            if len(visited) >= MAX_LIST_PAGES:
                reason = f"触及采集上限：单次运行最多遍历 {MAX_LIST_PAGES} 个列表页"
                break
            list_url = queue.pop(0)
            if list_url in visited:
                continue
            visited.add(list_url)

            try:
                resp = client.get(list_url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                reason = f"分页失败：列表页抓取失败 {list_url}（{exc}）"
                break
            outcome.visited_pages += 1

            detail_urls, next_page = _extract_list_links(resp.text, str(resp.url))

            # 抓取本页尚未采集的详情；任一失败 -> 整页不提交，运行停止
            page_records: list[dict] = []
            page_failed = False
            for detail_url in detail_urls:
                if detail_url in collected:
                    continue  # 以详情 URL 识别：已采过的跳过，不论它现在第几页
                if not same_origin(entry_url, detail_url):
                    continue  # 只采同站详情
                if new_details >= MAX_DETAIL_PAGES:
                    reason = f"触及采集上限：单次运行最多新抓 {MAX_DETAIL_PAGES} 个详情页"
                    page_failed = True
                    break
                try:
                    detail_resp = client.get(detail_url)
                    detail_resp.raise_for_status()
                except httpx.HTTPError as exc:
                    reason = f"分页失败：详情页抓取失败 {detail_url}（{exc}）"
                    page_failed = True
                    break
                try:
                    ann = parse_detail(detail_resp.text, str(detail_resp.url))
                except ValueError as exc:
                    reason = f"分页失败：详情页解析失败 {detail_url}（{exc}）"
                    page_failed = True
                    break
                new_details += 1
                page_records.append(_record_from(ann, batch, list_url))

            if page_failed:
                break  # 该页记录与进度都不提交；此前已提交的页面保留

            page_index += 1
            storage.commit_page(batch_id, list_url, page_index, len(detail_urls), page_records)
            collected.update(rec["url"] for rec in page_records)
            outcome.pages_committed += 1
            outcome.new_records += len(page_records)

            if next_page:
                if not same_origin(entry_url, next_page):
                    reason = f"分页失败：下一页指向外站，无法继续跟随 {next_page}"
                    break
                if next_page not in visited:
                    queue.append(next_page)
            # 无下一页 => 已到目录末页，循环自然结束

    if reason is None:
        # 走完了分页，还要确认批次内没有“日期缺失”的公告，否则无法断言区间已补齐
        missing = storage.date_missing_urls(batch_id)
        if missing:
            sample = "、".join(missing[:3])
            reason = (
                f"日期缺失：{len(missing)} 条公告无法解析发布日期（如 {sample}），"
                "无法确认其是否属于该区间"
            )

    if reason is None:
        outcome.status = STATUS_COMPLETED
    else:
        outcome.reason = reason
    storage.finish_run(batch_id, outcome.status, outcome.reason)
    return outcome
