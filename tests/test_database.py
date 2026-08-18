from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pytest

from app.database import Database
from app.models import AIEnrichment, AIStatus, NoticeDocument


@pytest.fixture
def db(tmp_path):
    store = Database(tmp_path / "notices.db")
    store.initialize()
    yield store
    store.close()


def document(article_id: str, *, source="future_notices", category="通知公告", title=None, day=1):
    return NoticeDocument(
        source_key=source,
        site_key="future_suda",
        article_id=article_id,
        official_title=title or f"公告 {article_id}",
        original_url=f"https://future.suda.edu.cn/art_{article_id}.htm",
        published_date=date(2026, 8, day),
        body_text=f"这是 {article_id} 的大学生创新创业训练计划正文",
        body_html=f"<p>这是 {article_id} 的正文</p>",
        attachment_names=(f"附件-{article_id}.pdf",),
        categories=(category,),
    )


def sync(db, *documents):
    run_id = db.begin_sync()
    for position, doc in enumerate(documents, 1):
        db.stage_notice(run_id, doc, f"来源-{doc.source_key}", list_position=position)
    result = db.commit_sync(run_id)
    return run_id, result


def test_failed_batch_never_reaches_formal_store(db):
    run_id = db.begin_sync()
    db.stage_notice(run_id, document("100"), "学院")
    assert db.is_known("future_suda", "100") is False
    db.fail_sync(run_id, "列表解析失败", stop_position="future_notices:2")
    assert db.list_notices() == []
    assert db.begin_sync() > run_id


def test_only_one_running_batch(db):
    run_id = db.begin_sync()
    with pytest.raises(RuntimeError):
        db.begin_sync()
    db.fail_sync(run_id, cancelled=True)


def test_cross_column_dedup_keeps_multiple_categories(db):
    run_id = db.begin_sync()
    db.stage_notice(run_id, document("200", source="future_notices", category="通知公告"), "学院通知")
    db.stage_notice(run_id, document("200", source="future_aid", category="奖惩助贷"), "学院奖助")
    result = db.commit_sync(run_id)
    assert result["new_count"] == 1
    notices = db.list_notices()
    assert len(notices) == 1
    assert notices[0]["categories"] == ["通知公告", "奖惩助贷"]
    assert db.is_known("future_suda", "200")


def test_first_success_is_share_baseline_and_second_run_is_candidate(db):
    first, _ = sync(db, document("old"))
    assert db.get_share_candidates() == {"through_run_id": first, "items": []}
    assert db.dashboard_summary()["last_share"] is None

    second, _ = sync(db, document("new", day=2))
    snapshot = db.get_share_candidates()
    assert snapshot["through_run_id"] == second
    assert [item["article_id"] if "article_id" in item else item["official_title"] for item in snapshot["items"]] == ["公告 new"]


def test_fixed_share_snapshot_does_not_skip_later_sync(db):
    sync(db, document("baseline"))
    through, _ = sync(db, document("second", day=2))
    snapshot = db.get_share_candidates()
    later, _ = sync(db, document("third", day=3))

    item = snapshot["items"][0]
    batch_id = db.complete_share(
        through,
        [{"id": item["id"], "selected": False, "title": "已审阅", "url": item["original_url"]}],
        "",
    )
    history = db.share_history()
    assert history[0]["id"] == batch_id
    assert history[0]["items"][0]["status"] == "reviewed_unshared"
    next_snapshot = db.get_share_candidates()
    assert next_snapshot["through_run_id"] == later
    assert [item["official_title"] for item in next_snapshot["items"]] == ["公告 third"]


def test_share_records_selected_and_unselected(db):
    sync(db, document("baseline"))
    through, _ = sync(db, document("a", day=2), document("b", day=3))
    items = db.get_share_candidates()["items"]
    payload = [
        {"id": item["id"], "selected": index == 0, "title": f"最终{index}", "url": item["original_url"]}
        for index, item in enumerate(items)
    ]
    db.complete_share(through, payload, "复制内容")
    history = db.share_history()[0]
    assert history["copied_text"] == "复制内容"
    assert {item["status"] for item in history["items"]} == {"shared", "reviewed_unshared"}
    assert db.get_share_candidates()["items"] == []


def test_share_rejects_items_outside_fixed_snapshot(db):
    sync(db, document("baseline"))
    through, _ = sync(db, document("only", day=2))
    with pytest.raises(ValueError):
        db.complete_share(through, [], "")


def test_share_batches_migrate_and_old_history_gets_new_defaults(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE share_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            through_run_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            copied_text TEXT NOT NULL
        )"""
    )
    conn.execute(
        "INSERT INTO share_batches(through_run_id,created_at,copied_text) VALUES(?,?,?)",
        (7, "2026-08-18T10:00:00+08:00", "旧分享"),
    )
    conn.commit()
    conn.close()

    store = Database(path)
    store.initialize()
    columns = {
        row["name"] for row in store._db.execute("PRAGMA table_info(share_batches)")
    }
    assert {"selection_mode", "date_start", "date_end"} <= columns
    history = store.share_history()[0]
    assert history["selection_mode"] == "new"
    assert history["date_start"] == ""
    assert history["date_end"] == ""
    store.close()


def test_range_candidates_are_inclusive_descending_and_exclude_later_snapshot(db):
    first, _ = sync(db, document("baseline", day=1))
    second, _ = sync(db, document("middle", day=2))
    sync(db, document("later", day=3))

    snapshot = db.get_share_candidates("range", "2026-08-01", "2026-08-02")
    assert snapshot["selection_mode"] == "range"
    assert snapshot["date_start"] == "2026-08-01"
    assert snapshot["date_end"] == "2026-08-02"
    assert snapshot["through_run_id"] == second + 1
    assert [item["official_title"] for item in snapshot["items"]] == [
        "公告 middle", "公告 baseline"
    ]

    # The range snapshot is the current max completed run, so the later item
    # is excluded only when the range itself excludes its date.
    snapshot = db.get_share_candidates("range", "2026-08-01", "2026-08-03")
    assert [item["official_title"] for item in snapshot["items"]] == [
        "公告 later", "公告 middle", "公告 baseline"
    ]
    assert [item["published_date"] for item in snapshot["items"]] == sorted(
        [item["published_date"] for item in snapshot["items"]], reverse=True
    )

    assert all(item["first_sync_run_id"] <= snapshot["through_run_id"] for item in snapshot["items"])


def test_range_completion_records_filter_and_does_not_advance_checkpoint(db):
    first, _ = sync(db, document("baseline", day=1))
    second, _ = sync(db, document("in-range", day=2))
    candidates = db.get_share_candidates("range", "2026-08-01", "2026-08-02")
    items = [
        {"id": item["id"], "selected": True, "title": item["official_title"], "url": item["original_url"]}
        for item in candidates["items"]
    ]

    batch_id = db.complete_share(
        candidates["through_run_id"], items, "范围分享",
        selection_mode="range", date_start="2026-08-01", date_end="2026-08-02",
    )
    checkpoint = db._db.execute(
        "SELECT through_run_id,last_share_at FROM share_checkpoint WHERE id=1"
    ).fetchone()
    assert checkpoint["through_run_id"] == first
    assert checkpoint["last_share_at"] is None
    history = db.share_history()[0]
    assert history["id"] == batch_id
    assert history["selection_mode"] == "range"
    assert history["date_start"] == "2026-08-01"
    assert history["date_end"] == "2026-08-02"
    assert history["copied_text"] == "范围分享"

    new_candidates = db.get_share_candidates("new")
    assert [item["official_title"] for item in new_candidates["items"]] == ["公告 in-range"]


def test_range_completion_uses_fixed_snapshot_for_item_set(db):
    sync(db, document("baseline", day=1))
    second, _ = sync(db, document("at-snapshot", day=2))
    snapshot = db.get_share_candidates("range", "2026-08-01", "2026-08-02")
    sync(db, document("after-snapshot", day=2))
    current = db.get_share_candidates("range", "2026-08-01", "2026-08-02")
    items = [
        {"id": item["id"], "selected": False}
        for item in snapshot["items"]
        if item["first_sync_run_id"] <= second
    ]
    later_item = next(item for item in current["items"] if item["first_sync_run_id"] > second)
    with pytest.raises(ValueError):
        db.complete_share(
            second, items + [{"id": later_item["id"], "selected": False}], "",
            selection_mode="range", date_start=snapshot["date_start"], date_end=snapshot["date_end"],
        )

    db.complete_share(
        second, items, "", selection_mode="range",
        date_start=snapshot["date_start"], date_end=snapshot["date_end"],
    )
    assert {item["final_title"] for item in db.share_history()[0]["items"]} == {
        "公告 baseline", "公告 at-snapshot"
    }


@pytest.mark.parametrize("start,end", [
    ("2026-8-01", "2026-08-02"),
    ("2026-02-30", "2026-03-01"),
    ("2026-08-03", "2026-08-02"),
    ("", "2026-08-02"),
])
def test_range_requires_valid_inclusive_date_interval(db, start, end):
    with pytest.raises(ValueError):
        db.get_share_candidates("range", start, end)


def test_ai_ready_and_pending_round_trip(db):
    sync(db, document("ai"))
    notice_id = db.list_notices()[0]["id"]
    enrichment = AIEnrichment(
        short_title="2026 大创申报", summary="摘要", audience="本科生", materials=["申请表"],
        tags=["大创", "申报"], requires_external_system=True, status=AIStatus.READY,
        model="qwen", generated_at=datetime.now(timezone.utc),
    )
    assert db.update_ai(notice_id, enrichment)
    notice = db.get_notice(notice_id)
    assert notice["short_title"] == "2026 大创申报"
    assert notice["materials"] == ["申请表"]
    assert notice["requires_external_system"] is True
    assert db.list_pending_ai() == []
    assert db.mark_ai_pending(notice_id, "稍后重试")
    assert db.list_pending_ai()[0]["id"] == notice_id


def test_share_summary_cache_round_trip(db):
    sync(db, document("share-cache"))
    notice_id = db.list_notices()[0]["id"]
    assert db.get_notice(notice_id)["share_summary"] == ""
    assert db.update_share_summary(notice_id, "  本科生创新项目开始申报  \n不应保存")
    assert db.get_notice(notice_id)["share_summary"] == "本科生创新项目开始申报"
    assert db.list_missing_share_summaries() == []
    assert db.list_share_summary_candidates()[0]["id"] == notice_id
    assert db.update_share_summary(notice_id, "")
    assert db.get_notice(notice_id)["share_summary"] == ""


def test_legacy_notice_table_adds_share_summary_column(tmp_path):
    path = tmp_path / "legacy-notices.db"
    store = Database(path)
    store.initialize()
    store._db.execute("ALTER TABLE notices DROP COLUMN share_summary")
    store.close()

    migrated = Database(path)
    migrated.initialize()
    columns = {row["name"] for row in migrated._db.execute("PRAGMA table_info(notices)")}
    assert "share_summary" in columns
    migrated.close()


def test_search_source_year_and_snippet(db):
    sync(db, document("search", title="创新项目申报"))
    assert len(db.list_notices(q="大学生创新创业")) == 1
    assert "大学生创新创业" in db.list_notices(q="大学生创新创业")[0]["snippet"]
    assert len(db.list_notices(source_key="future_notices")) == 1
    assert db.list_notices(source_key="jwc_notices") == []
    assert len(db.list_notices(year=2026)) == 1
    assert db.list_notices(year=2025) == []


def test_lightweight_pages_continue_after_fixed_fifteen_without_gaps(db):
    sync(db, *(document(f"page-{index}") for index in range(36)))
    first = db.list_notice_page(page=1, page_size=15)
    second = db.list_notice_page(page=2, page_size=20)
    third = db.list_notice_page(page=3, page_size=20)

    ids = [item["id"] for item in first["items"] + second["items"] + third["items"]]
    assert len(ids) == 36
    assert len(set(ids)) == 36
    assert first["total"] == second["total"] == third["total"] == 36
    assert first["has_more"] is True and second["has_more"] is True
    assert third["has_more"] is False
    assert "body_text" not in first["items"][0]


def test_notice_page_uses_semantic_search_preview_and_safe_match_segments(db):
    sync(db, document("semantic", title="关于大创项目申报的通知"))
    notice_id = db.list_notices()[0]["id"]
    db._db.execute(
        "UPDATE notices SET body_text=?,summary=? WHERE id=?",
        (
            "苏大教〔2026〕12号文件已经发布。请各位同学按要求准备材料。"
            "本次大学生创新创业训练计划面向全体本科生，截止日期为八月三十日。"
            "逾期不再受理。",
            "AI 摘要优先显示",
            notice_id,
        ),
    )
    normal = db.list_notice_page(page=1, page_size=15)["items"][0]
    assert normal["preview"] == "AI 摘要优先显示"

    searched = db.list_notice_page(q="大学生创新创业", page=1, page_size=15)["items"][0]
    assert "本次大学生创新创业训练计划面向全体本科生" in searched["preview"]
    assert any(part["match"] and part["text"] == "大学生创新创业"
               for part in searched["preview_fragments"])
    assert searched["preview"].startswith("……")


def test_dashboard_reports_sync_and_pending_share(db):
    first, _ = sync(db, document("base"))
    summary = db.dashboard_summary()
    assert summary["sync_status"] == "completed"
    assert summary["pending_share_count"] == 0
    second, _ = sync(db, document("pending", day=2))
    summary = db.dashboard_summary()
    assert summary["new_count"] == 1
    assert summary["pending_share_count"] == 1
    assert first < second


def test_compact_embeddings_are_model_scoped_and_searchable(db):
    sync(db, document("vector-a"), document("vector-b", day=2))
    notices = {item["article_id"]: item for item in db.list_notices()}
    assert db.store_embedding(notices["vector-a"]["id"], "embed-v1", 3, [1, 0, 0])
    assert db.store_embedding(notices["vector-b"]["id"], "embed-v1", 3, [0, 1, 0])
    assert [item["article_id"] for item in db.semantic_search([0.9, 0.1, 0], "embed-v1")] == [
        "vector-a", "vector-b"
    ]
    assert db.semantic_search([1, 0, 0], "other-model") == []
    with pytest.raises(ValueError):
        db.store_embedding(notices["vector-a"]["id"], "embed-v1", 2, [1])


def test_unavailable_article_is_deduplicated_and_not_a_notice(db):
    db.record_unavailable(
        "future_suda", "gone", "future_notices", "失效公告",
        "https://future.suda.edu.cn/gone", 410, "gone",
    )
    db.record_unavailable(
        "future_suda", "gone", "future_notices", "失效公告（更新）",
        "https://future.suda.edu.cn/gone", 410, "still gone",
    )
    assert db.is_unavailable("future_suda", "gone") is True
    assert db.is_unavailable("future_suda", "other") is False
    assert db.list_notices() == []
    row = db._db.execute("SELECT * FROM unavailable_articles").fetchone()
    assert row["title"] == "失效公告（更新）"
    assert row["error"] == "still gone"
