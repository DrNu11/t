import news_sources


def test_news_sources_legacy_settings_without_news_columns(tmp_path, monkeypatch):
    import sqlite3
    import config

    db_file = tmp_path / "legacy_settings.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE paper_trading_settings ("
        "id INTEGER PRIMARY KEY, is_running INTEGER NOT NULL DEFAULT 0,"
        " tracks TEXT NOT NULL DEFAULT '[]', started_at TEXT, updated_at TEXT)"
    )
    conn.execute("INSERT INTO paper_trading_settings (id) VALUES (1)")
    conn.execute(
        "CREATE TABLE raw_news (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " source TEXT, content TEXT, timestamp TEXT, status TEXT, is_noise INTEGER DEFAULT 0)"
    )
    conn.commit()
    settings = news_sources.get_settings(conn)
    assert settings["enabled"] is True
    assert settings["daily_target"] == 300
    assert settings["remaining"] == 300
    assert news_sources.allow_ingest("TechFlow 深潮", conn) is True
    conn.close()


def test_news_sources_default_all_on(temp_db):
    settings = news_sources.get_settings(temp_db)
    assert settings["enabled"] is True
    assert settings["daily_target"] == 300
    assert settings["sources"] == {
        "financialjuice": True,
        "tree_news": True,
        "techflow": True,
        "eastmoney": True,
        "blockbeats": True,
        "jin10": True,
    }
    assert settings["today_count"] == 0
    assert settings["remaining"] == 300


def test_jin10_source_aliases_are_normalized():
    assert news_sources._normalize_source("金十") == "jin10"
    assert news_sources._normalize_source("Jin10 Open Data") == "jin10"


def test_news_sources_toggle_and_quota(temp_db):
    news_sources.set_settings(enabled=True, daily_target=2, sources={"eastmoney": False}, connection=temp_db)
    assert news_sources.allow_ingest("东方财富", temp_db) is False
    assert news_sources.allow_ingest("TechFlow 深潮", temp_db) is True

    temp_db.execute(
        "INSERT INTO raw_news (source, content, timestamp, status) VALUES (?,?,?,?)",
        ("TechFlow 深潮", "a", f"{news_sources._today()} 10:00:00", "PENDING"),
    )
    temp_db.execute(
        "INSERT INTO raw_news (source, content, timestamp, status) VALUES (?,?,?,?)",
        ("TechFlow 深潮", "b", f"{news_sources._today()} 11:00:00", "PENDING"),
    )
    temp_db.commit()
    assert news_sources.allow_ingest("TechFlow 深潮", temp_db) is False

    news_sources.set_settings(enabled=False, connection=temp_db)
    assert news_sources.allow_ingest("WS:financialjuice", temp_db) is False
