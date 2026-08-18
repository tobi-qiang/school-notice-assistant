from datetime import date

from app.application import SyncCoordinator
from app.config_store import ConfigStore
from app.database import Database
from app.models import ListEntry, NoticeDocument, SourceConfig
from app.web import create_app


class Client:
    def __init__(self): self.items = []
    def fetch_list(self, _source, page): return self.items if page == 1 else []
    def fetch_detail(self, source, item):
        return NoticeDocument(
            source.key, item.article_id, item.title, item.url, item.published_date,
            f"{item.title}正文", f"<p>{item.title}正文</p>", site_key=source.site_key,
            categories=(source.category,),
        )


def item(source, article_id, day):
    return ListEntry(source.key, article_id, f"公告{article_id}", f"https://school/{article_id}",
                     date(2026, 8, day), day)


def test_real_database_sync_flask_and_share_flow(tmp_path):
    database = Database(tmp_path / "notices.db"); database.initialize()
    source = SourceConfig("source", "学校", "https://school", "/list.htm", "通知", site_key="school")
    client = Client()
    coordinator = SyncCoordinator(database, [source], client_factory=lambda *_args: client)

    client.items = [item(source, "baseline", 1)]
    assert coordinator.run_sync()["status"] == "completed"
    assert database.get_share_candidates()["items"] == []
    client.items = [item(source, "new", 2), item(source, "baseline", 1)]
    assert coordinator.run_sync()["new_count"] == 1

    app = create_app(database, coordinator, ConfigStore(tmp_path / "config.json"))
    app.config.update(TESTING=True)
    web = app.test_client()
    assert web.get("/").status_code == 200
    snapshot = web.get("/api/share/candidates").get_json()
    assert [entry["official_title"] for entry in snapshot["items"]] == ["公告new"]
    entry = snapshot["items"][0]
    response = web.post("/api/share/complete", json={
        "through_run_id": snapshot["through_run_id"],
        "items": [{"id": entry["id"], "selected": True, "title": "新公告", "url": entry["original_url"]}],
        "copied_text": f"【新公告】{entry['original_url']}",
    })
    assert response.status_code == 200
    assert database.get_share_candidates()["items"] == []
    assert database.share_history()[0]["items"][0]["status"] == "shared"
    database.close()
