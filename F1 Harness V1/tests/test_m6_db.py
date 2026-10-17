from hermes import db


def test_m6_tables_and_seeds(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    base = {r["branch"]: r["value"] for r in conn.execute("SELECT * FROM baselines")}
    assert base == {"资讯": 60, "前瞻": 60, "复盘": 60, "科普": None}
    s = dict(conn.execute("SELECT key, value FROM settings WHERE key LIKE 'qa.%'").fetchall())
    assert s == {"qa.rework_rounds": "2", "qa.recalibrate_gap": "10", "qa.baseline_window": "5"}
    conn.execute("INSERT INTO reviews(kind, video_path, total, created_at) VALUES ('校准', '/x.mp4', 61, ?)", (db.now(),))
    conn.execute("INSERT INTO lessons(content, created_at) VALUES ('少用碎句', ?)", (db.now(),))
    assert conn.execute("SELECT state FROM lessons").fetchone()[0] == "pending"
