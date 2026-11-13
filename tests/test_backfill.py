"""补采功能端到端测试：SQLite 批次/进度、续采重走分页、插入与失败场景。

运行方式：
    PYTHONPATH=/tmp/pylibs python3 -I tests/test_backfill.py
"""
from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

# -I 隔离模式下 PYTHONPATH 不生效，显式注入依赖目录
for candidate in ("/tmp/pylibs", "/tmp/pyroot/lib/python3.11/site-packages"):
    if Path(candidate).exists() and candidate not in sys.path:
        sys.path.insert(0, candidate)

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "webapp"))
sys.path.insert(0, str(REPO / "example-site"))

import serve  # noqa: E402  example-site/serve.py
from app.backfill import run_batch  # noqa: E402
from app.storage import Storage  # noqa: E402
import app.backfill as backfill_mod  # noqa: E402


def start_server():
    server = serve.make_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def assert_true(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)
    print(f"  ✓ {message}")


def assert_equal(actual, expected, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}：期望 {expected!r}，实际 {actual!r}")
    print(f"  ✓ {message}")


def main() -> int:
    server, base = start_server()
    tmp = tempfile.mkdtemp(prefix="backfill-test-")
    storage = Storage(str(Path(tmp) / "backfill.db"))
    entry = f"{base}/backfill/list/page_1.html"

    print("[1] 首次运行：末页详情 500 -> 未补齐，已成功页面保留")
    serve.STATE.reset()
    batch, created = storage.create_or_get_batch(entry, "2026-10-01", "2026-11-30")
    assert_true(created, "创建了新批次")
    outcome = run_batch(storage, batch["id"])
    assert_equal(outcome.status, "incomplete", "首次运行未补齐")
    assert_true("分页失败" in (outcome.reason or ""), f"原因是分页失败（{outcome.reason}）")
    assert_equal(len(storage.get_pages(batch["id"])), 1, "仅第 1 页进度被提交")
    anns = storage.get_announcements(batch["id"])
    assert_equal(len(anns), 3, "第 1 页 3 条记录已保留")
    assert_true(all(a["project_no"] != "BF-2026-001" for a in anns), "失败页的记录（BF-2026-001）未入库")
    saved = storage.get_batch(batch["id"])
    assert_equal(saved["status"], "incomplete", "批次状态落库为未补齐")
    assert_true("分页失败" in saved["incomplete_reason"], "未补齐原因落库")

    print("[2] 续采：从首页重走分页，已采详情按 URL 跳过，最终补齐")
    outcome = run_batch(storage, batch["id"])
    assert_equal(outcome.status, "completed", "第二次运行补齐")
    assert_equal(len(storage.get_announcements(batch["id"])), 4, "共 4 条记录")
    assert_equal(len(storage.get_pages(batch["id"])), 2, "两页进度都已提交")
    counts = serve.STATE.snapshot()["counts"]
    assert_equal(counts.get(f"/backfill/detail/{serve.FLAKY_ID}.html"), 2,
                 "偶发 500 的详情共被请求 2 次（失败 1 次 + 续采成功 1 次）")
    assert_equal(counts.get("/backfill/detail/notice_b02.html"), 1,
                 "已采详情在续采时未重复抓取")

    print("[3] 目录首页插入新公告：续采拾取新条目，后移旧条目不重复")
    serve.STATE.advance()
    outcome = run_batch(storage, batch["id"])
    assert_equal(outcome.status, "completed", "插入后续采仍补齐")
    anns = storage.get_announcements(batch["id"])
    urls = [a["url"] for a in anns]
    assert_equal(len(anns), 6, "共 6 条记录（含 2 条新插入）")
    assert_equal(len(set(urls)), 6, "无重复记录（按详情 URL 去重）")
    counts = serve.STATE.snapshot()["counts"]
    assert_equal(counts.get("/backfill/detail/notice_b02.html"), 1, "后移的旧公告仍未重复抓取")
    assert_equal(counts.get("/backfill/detail/notice_b05.html"), 1, "新插入公告被补采")
    in_range = storage.get_announcements(batch["id"], in_range_only=True)
    assert_equal(len(in_range), 6, "6 条都在批次区间内")

    print("[4] 窄区间批次：区间内过滤")
    batch2, _ = storage.create_or_get_batch(entry, "2026-10-01", "2026-10-31")
    outcome = run_batch(storage, batch2["id"])
    assert_equal(outcome.status, "completed", "窄区间批次补齐")
    assert_equal(len(storage.get_announcements(batch2["id"])), 6, "窄区间批次也采了全部 6 条")
    assert_equal(len(storage.get_announcements(batch2["id"], in_range_only=True)), 4,
                 "仅 4 条落在 10 月区间内")

    print("[5] 发布日期缺失：页面保留但批次未补齐")
    batch3, _ = storage.create_or_get_batch(f"{base}/backfill-dates/list/page_1.html",
                                            "2026-10-01", "2026-10-31")
    outcome = run_batch(storage, batch3["id"])
    assert_equal(outcome.status, "incomplete", "日期缺失批次未补齐")
    assert_true("日期缺失" in (outcome.reason or ""), f"原因是日期缺失（{outcome.reason}）")
    assert_equal(len(storage.get_announcements(batch3["id"])), 2, "2 条记录仍已保存")
    assert_equal(len(storage.get_pages(batch3["id"])), 1, "页面进度已提交")

    print("[6] 详情解析失败：整页不提交，已成功页面保留")
    batch4, _ = storage.create_or_get_batch(f"{base}/backfill-broken/list/page_1.html",
                                            "2026-10-01", "2026-10-31")
    outcome = run_batch(storage, batch4["id"])
    assert_equal(outcome.status, "incomplete", "解析失败批次未补齐")
    assert_true("解析失败" in (outcome.reason or ""), f"原因是解析失败（{outcome.reason}）")
    assert_equal(len(storage.get_pages(batch4["id"])), 1, "仅第 1 页进度被提交")
    anns4 = storage.get_announcements(batch4["id"])
    assert_equal(len(anns4), 2, "第 1 页 2 条记录保留")
    assert_true(all(a["project_no"] != "BF-2026-X03" for a in anns4),
                "失败页上本可解析的 BF-2026-X03 也随整页回滚")

    print("[7] 触及采集上限：保留已成功页面并说明原因")
    original = backfill_mod.MAX_LIST_PAGES
    backfill_mod.MAX_LIST_PAGES = 1
    try:
        batch5, _ = storage.create_or_get_batch(entry, "2026-11-01", "2026-11-30")
        outcome = run_batch(storage, batch5["id"])
        assert_equal(outcome.status, "incomplete", "触上限批次未补齐")
        assert_true("触及采集上限" in (outcome.reason or ""), f"原因是采集上限（{outcome.reason}）")
        assert_equal(len(storage.get_pages(batch5["id"])), 1, "上限内已成功的第 1 页保留")
    finally:
        backfill_mod.MAX_LIST_PAGES = original

    print("[8] 相同目录+区间复用同一批次")
    again, created = storage.create_or_get_batch(entry, "2026-10-01", "2026-11-30")
    assert_true(not created and again["id"] == batch["id"], "重复提交并入已有批次")

    server.shutdown()
    print("\n全部补采测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
