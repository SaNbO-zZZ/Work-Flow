import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config

# 追加新迁移只能往列表末尾加，已发布的条目不可改
MIGRATIONS = [
    """
    CREATE TABLE episodes (
        id              INTEGER PRIMARY KEY,
        code            TEXT UNIQUE,              -- 期号：20260929-复盘-巴林大奖赛-01；闲聊会话为空
        branch          TEXT CHECK (branch IN ('资讯','前瞻','复盘','科普')),
        kind            TEXT,                     -- 期型：P/P12/Q/P3Q/S/SQ+S/R、单条/合集
        series_no       INTEGER,                  -- 科普系列号（只用于标题/封面）
        title           TEXT NOT NULL DEFAULT '新会话',
        gp_label        TEXT,                     -- 大奖赛名 / 夏休 / 冬歇 / 发车区
        state           TEXT NOT NULL DEFAULT '闲聊',
        stage_run       INTEGER NOT NULL DEFAULT 0, -- 每进入一次工作阶段 +1，区分打回后的重做
        paused_from     TEXT,
        folder          TEXT,
        target_seconds  INTEGER,
        deadline_at     TEXT,
        jump_at         TEXT,                     -- 插队时间，非空即排到前面
        ad_info         TEXT,
        tr_authorized   TEXT,
        codex_thread_id TEXT,
        created_at      TEXT NOT NULL,
        queued_at       TEXT,
        updated_at      TEXT NOT NULL
    );
    CREATE TABLE messages (
        id          INTEGER PRIMARY KEY,
        episode_id  INTEGER NOT NULL REFERENCES episodes(id),
        role        TEXT NOT NULL CHECK (role IN ('user','assistant','system','card')),
        content     TEXT NOT NULL DEFAULT '',
        model       TEXT,
        effort      TEXT,
        attachments TEXT,
        card        TEXT,
        created_at  TEXT NOT NULL
    );
    CREATE TABLE tasks (
        id            INTEGER PRIMARY KEY,
        episode_id    INTEGER NOT NULL REFERENCES episodes(id),
        stage         TEXT NOT NULL,              -- 所属状态，如 选题与资料
        step          TEXT NOT NULL,              -- 具体步骤，如 D1资料
        stage_run     INTEGER NOT NULL,
        phase         INTEGER NOT NULL,           -- 同一阶段内按顺序执行的批次，批内并行
        role          TEXT NOT NULL,
        model         TEXT NOT NULL,
        effort        TEXT NOT NULL,
        model_override INTEGER NOT NULL DEFAULT 0,
        needs_jianying INTEGER NOT NULL DEFAULT 0,
        state         TEXT NOT NULL DEFAULT 'queued'
                      CHECK (state IN ('queued','running','succeeded','failed','cancelled','waiting_quota')),
        attempts      INTEGER NOT NULL DEFAULT 0,
        not_before    TEXT,
        input         TEXT,
        output        TEXT,
        error         TEXT,
        prompt_version TEXT,
        codex_thread_id TEXT,
        message_id    INTEGER REFERENCES messages(id),
        created_at    TEXT NOT NULL,
        started_at    TEXT,
        finished_at   TEXT
    );
    CREATE TABLE gates (
        id          INTEGER PRIMARY KEY,
        episode_id  INTEGER NOT NULL REFERENCES episodes(id),
        gate        TEXT NOT NULL CHECK (gate IN ('G0','G1','G2','G3')),
        state       TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','approved','rejected')),
        feedback    TEXT,
        user_score  INTEGER,
        created_at  TEXT NOT NULL,
        decided_at  TEXT
    );
    CREATE TABLE facts (
        id          INTEGER PRIMARY KEY,
        episode_id  INTEGER NOT NULL REFERENCES episodes(id),
        tag         TEXT NOT NULL,                -- F12 / S3
        kind        TEXT NOT NULL CHECK (kind IN ('F','S')),
        content     TEXT NOT NULL,
        value       TEXT,
        source      TEXT NOT NULL,
        url         TEXT,
        excerpt     TEXT,
        fetched_at  TEXT NOT NULL,
        verified    TEXT NOT NULL DEFAULT 'pending',
        UNIQUE (episode_id, tag)
    );
    CREATE TABLE assets (
        id           INTEGER PRIMARY KEY,
        source_type  TEXT NOT NULL,              -- 脚本/用户链接/GPT搜索/Drive
        source_url   TEXT,
        local_path   TEXT,
        drive_path   TEXT,
        duration     REAL,
        width        INTEGER,
        height       INTEGER,
        sha256       TEXT UNIQUE,
        fingerprints TEXT,                       -- 每个镜头的画面指纹
        tags         TEXT,
        thumbs       TEXT,
        credit       TEXT,                       -- 右下角出处文字
        created_at   TEXT NOT NULL
    );
    CREATE TABLE asset_uses (
        id            INTEGER PRIMARY KEY,
        episode_id    INTEGER NOT NULL REFERENCES episodes(id),
        asset_id      INTEGER NOT NULL REFERENCES assets(id),
        src_in        REAL NOT NULL,
        src_out       REAL NOT NULL,
        timeline_at   REAL,
        cross_episode INTEGER NOT NULL DEFAULT 0,
        approval      TEXT NOT NULL DEFAULT 'n/a' CHECK (approval IN ('n/a','pending','approved','rejected')),
        created_at    TEXT NOT NULL
    );
    CREATE TABLE usage (
        id              INTEGER PRIMARY KEY,
        ts              TEXT NOT NULL,
        episode_id      INTEGER REFERENCES episodes(id),
        task_id         INTEGER REFERENCES tasks(id),
        model           TEXT NOT NULL,
        effort          TEXT NOT NULL,
        input_tokens    INTEGER NOT NULL DEFAULT 0,
        cached_tokens   INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens   INTEGER NOT NULL DEFAULT 0,
        reasoning_tokens INTEGER NOT NULL DEFAULT 0,
        quota_used_percent REAL
    );
    CREATE INDEX usage_ts ON usage(ts);
    CREATE TABLE archives (
        episode_id    INTEGER PRIMARY KEY REFERENCES episodes(id),
        drive_path    TEXT,
        upload_state  TEXT NOT NULL DEFAULT 'pending',
        verified_at   TEXT,
        delete_after  TEXT,
        extensions    INTEGER NOT NULL DEFAULT 0,
        deleted_at    TEXT
    );
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # M3 数据层
    """
    CREATE TABLE race_events (
        season        INTEGER NOT NULL,
        round         INTEGER NOT NULL,
        name_en       TEXT NOT NULL,              -- FastF1 EventName，如 Bahrain Grand Prix
        official_name TEXT,
        country       TEXT,
        location      TEXT,
        event_date    TEXT NOT NULL,              -- 正赛当地日期 YYYY-MM-DD
        format        TEXT NOT NULL,              -- conventional / sprint_qualifying …
        updated_at    TEXT NOT NULL,
        PRIMARY KEY (season, round)
    );
    CREATE TABLE race_sessions (
        season     INTEGER NOT NULL,
        round      INTEGER NOT NULL,
        idx        INTEGER NOT NULL,              -- 1..5 对应 FastF1 Session1..5
        name       TEXT NOT NULL,                 -- Practice 1 / Sprint Qualifying / Race …
        start_utc  TEXT,                          -- ISO8601 UTC
        PRIMARY KEY (season, round, idx),
        FOREIGN KEY (season, round) REFERENCES race_events(season, round) ON DELETE CASCADE
    );
    CREATE TABLE calendar_changes (
        id          INTEGER PRIMARY KEY,
        detected_at TEXT NOT NULL,
        season      INTEGER NOT NULL,
        round       INTEGER,
        detail      TEXT NOT NULL
    );
    CREATE TABLE fia_docs (
        id            INTEGER PRIMARY KEY,
        url           TEXT NOT NULL UNIQUE,
        title         TEXT NOT NULL,
        event         TEXT,                       -- FIA 页面上的分站名
        published_at  TEXT,
        first_seen_at TEXT NOT NULL,
        notified      INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE glossary (
        id          INTEGER PRIMARY KEY,
        kind        TEXT NOT NULL CHECK (kind IN ('车手','车队','大奖赛','赛道','其他')),
        original    TEXT NOT NULL,
        suggested   TEXT,
        approved    TEXT,
        state       TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','approved')),
        source      TEXT,                          -- 内置词典 / GPT / 用户
        note        TEXT,
        updated_at  TEXT NOT NULL,
        UNIQUE (kind, original)
    );
    CREATE TABLE news_items (
        id           INTEGER PRIMARY KEY,
        url          TEXT NOT NULL UNIQUE,
        title        TEXT NOT NULL,
        source       TEXT,
        published_at TEXT,
        summary      TEXT,
        via          TEXT NOT NULL CHECK (via IN ('script','gpt')),
        fetched_at   TEXT NOT NULL
    );
    CREATE TABLE news_picks (
        id         INTEGER PRIMARY KEY,
        day        TEXT NOT NULL,                  -- 北京时间日期
        rank       INTEGER NOT NULL,
        news_id    INTEGER NOT NULL REFERENCES news_items(id),
        branch     TEXT,                           -- 建议做成哪个分支
        reason     TEXT,
        UNIQUE (day, rank)
    )
    """,
    "ALTER TABLE news_picks ADD COLUMN headline TEXT",
    # M4 素材和图形
    """
    ALTER TABLE assets ADD COLUMN title TEXT;
    ALTER TABLE assets ADD COLUMN uploader TEXT;
    ALTER TABLE assets ADD COLUMN state TEXT NOT NULL DEFAULT 'ready';
    ALTER TABLE assets ADD COLUMN has_subtitles INTEGER;
    ALTER TABLE assets ADD COLUMN has_logo INTEGER;
    ALTER TABLE assets ADD COLUMN logo_note TEXT;
    ALTER TABLE assets ADD COLUMN tagged_at TEXT;
    CREATE TABLE asset_shots (
        id        INTEGER PRIMARY KEY,
        asset_id  INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
        idx       INTEGER NOT NULL,              -- 镜头序号，从 0 开始
        start_s   REAL NOT NULL,
        end_s     REAL NOT NULL,
        keyframe  TEXT,                         -- 关键帧图片路径
        phash     TEXT,                         -- JSON：按固定帧率抽帧的 64 位感知哈希（16 位十六进制）序列
        clip_vec  BLOB,                         -- 以文搜图用的向量（float16）
        tags      TEXT,                         -- JSON
        UNIQUE (asset_id, idx)
    );
    CREATE TABLE downloads (
        id          INTEGER PRIMARY KEY,
        url         TEXT NOT NULL,
        episode_id  INTEGER REFERENCES episodes(id),
        state       TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','running','done','failed')),
        progress    REAL,
        error       TEXT,
        asset_id    INTEGER REFERENCES assets(id),
        created_at  TEXT NOT NULL,
        finished_at TEXT
    );
    ALTER TABLE archives ADD COLUMN reminded_at TEXT
    """,
    # 逐镜头标记自带字幕和其他频道台标（2026-09-30 用户确认），剪辑时挑干净镜头用
    """
    ALTER TABLE asset_shots ADD COLUMN has_subtitles INTEGER;
    ALTER TABLE asset_shots ADD COLUMN has_logo INTEGER;
    ALTER TABLE asset_shots ADD COLUMN logo_note TEXT
    """,
    # 用户在网页上改/加的主标签（JSON 数组），非空就替代自动结果，重新打标不覆盖
    """
    ALTER TABLE asset_shots ADD COLUMN main_manual TEXT
    """,
    # M5 配音和合成；1.3 倍做法 2026-10-10 用户盲听选定程序加速
    """
    CREATE TABLE voice_takes (
        id          INTEGER PRIMARY KEY,
        episode_id  INTEGER NOT NULL REFERENCES episodes(id),
        stage_run   INTEGER NOT NULL,
        method      TEXT NOT NULL,                -- 剪映脚本 / Codex / 手动
        voice       TEXT NOT NULL,
        draft       TEXT,                         -- 剪映草稿名
        raw_dir     TEXT NOT NULL,
        audio_path  TEXT,                         -- 后处理后的配音
        speed       REAL NOT NULL DEFAULT 1.0,
        duration    REAL,
        align_path  TEXT,
        issues      TEXT,                         -- JSON：读错、停顿、语速提醒
        created_at  TEXT NOT NULL
    );
    CREATE TABLE timelines (
        id          INTEGER PRIMARY KEY,
        episode_id  INTEGER NOT NULL REFERENCES episodes(id),
        stage_run   INTEGER NOT NULL,
        kind        TEXT NOT NULL CHECK (kind IN ('粗剪','成片')),
        path        TEXT NOT NULL,                -- 时间线 JSON
        video_path  TEXT,
        checks      TEXT,                         -- JSON：规则检查 / 硬检查结果
        created_at  TEXT NOT NULL
    );
    CREATE TABLE deliveries (
        id          INTEGER PRIMARY KEY,
        episode_id  INTEGER NOT NULL REFERENCES episodes(id),
        kind        TEXT NOT NULL,                -- 成片/字幕/横版封面/竖版封面/标题和简介/制作记录
        path        TEXT NOT NULL,
        sha256      TEXT,
        created_at  TEXT NOT NULL,
        UNIQUE (episode_id, kind)
    );
    INSERT OR IGNORE INTO settings(key, value) VALUES
        ('voice.name', '热血解说员'), ('voice.speed', '1.3'),
        ('voice.speed_method', '程序加速'), ('voice.gap', '0.5')
    """,
    # M6 QA 和 Benchmark：历史成片 60 分作棘轮起点，科普用第一期分数作起点（基线留空）
    """
    CREATE TABLE reviews (
        id              INTEGER PRIMARY KEY,
        episode_id      INTEGER REFERENCES episodes(id),  -- 校准历史成片时为空
        stage_run       INTEGER,
        kind            TEXT NOT NULL CHECK (kind IN ('成片','校准','测试')),
        branch          TEXT,
        video_path      TEXT NOT NULL,
        rubric_version  TEXT,
        total           REAL,
        scores          TEXT,                      -- JSON：{分项: 分数}
        issues          TEXT,                      -- JSON：[{at, item, detail, severity}]
        model           TEXT,
        codex_thread_id TEXT,
        created_at      TEXT NOT NULL
    );
    CREATE TABLE baselines (
        branch      TEXT PRIMARY KEY CHECK (branch IN ('资讯','前瞻','复盘','科普')),
        value       REAL,
        updated_at  TEXT NOT NULL
    );
    CREATE TABLE lessons (
        id          INTEGER PRIMARY KEY,
        episode_id  INTEGER REFERENCES episodes(id),
        branch      TEXT,                          -- 空表示所有分支通用
        content     TEXT NOT NULL,
        state       TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','approved','rejected')),
        created_at  TEXT NOT NULL,
        decided_at  TEXT
    );
    INSERT OR IGNORE INTO baselines(branch, value, updated_at) VALUES
        ('资讯', 60, '2026-10-11T00:00:00+00:00'), ('前瞻', 60, '2026-10-11T00:00:00+00:00'),
        ('复盘', 60, '2026-10-11T00:00:00+00:00'), ('科普', NULL, '2026-10-11T00:00:00+00:00');
    INSERT OR IGNORE INTO settings(key, value) VALUES
        ('qa.rework_rounds', '2'), ('qa.recalibrate_gap', '10'), ('qa.baseline_window', '5')
    """,
    # M6-7 棘轮与自动返工：关卡自动通过标记、计入基线的评审、返工记录（cycle = 当时用户打回过几次）
    """
    ALTER TABLE gates ADD COLUMN auto INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE reviews ADD COLUMN approved_at TEXT;
    CREATE TABLE reworks (
        id          INTEGER PRIMARY KEY,
        episode_id  INTEGER NOT NULL REFERENCES episodes(id),
        cycle       INTEGER NOT NULL,
        round       INTEGER NOT NULL,
        review_id   INTEGER REFERENCES reviews(id),
        total       REAL,
        baseline    REAL,
        back_to     TEXT NOT NULL,
        steps       TEXT,
        issues      TEXT,
        state       TEXT NOT NULL DEFAULT 'running' CHECK (state IN ('running','done')),
        created_at  TEXT NOT NULL,
        finished_at TEXT
    )
    """,
    # M6-8 Benchmark 与经验笔记：基线变化历史（起点取迁移时的现值）、笔记适用步骤和依据、改写前的 AI 原文
    """
    CREATE TABLE baseline_history (
        id          INTEGER PRIMARY KEY,
        branch      TEXT NOT NULL,
        old_value   REAL,
        value       REAL,
        reason      TEXT,
        episode_id  INTEGER REFERENCES episodes(id),
        review_id   INTEGER REFERENCES reviews(id),
        created_at  TEXT NOT NULL
    );
    INSERT INTO baseline_history(branch, old_value, value, reason, created_at)
        SELECT branch, NULL, value, '起点', updated_at FROM baselines WHERE value IS NOT NULL;
    ALTER TABLE lessons ADD COLUMN step TEXT;
    ALTER TABLE lessons ADD COLUMN basis TEXT;
    ALTER TABLE lessons ADD COLUMN original TEXT
    """,
    # 用户在网页上标“不可用”的镜头（如与 F1 无关的画面，2026-10-12 用户定），剪辑决策和以文搜图都跳过
    """
    ALTER TABLE asset_shots ADD COLUMN unusable INTEGER NOT NULL DEFAULT 0
    """,
    # 会话里每次进关卡留一张卡片（第 3 批，2026-10-13 用户定：审过的东西一直看得到）；补上已有的关卡
    """
    INSERT INTO messages(episode_id, role, content, card, created_at)
        SELECT episode_id, 'card', gate || ' 待审', json_object('type', 'gate', 'gate', gate, 'gate_id', id), created_at
        FROM gates ORDER BY id
    """,
    # 长镜头按画面变化切细（2026-10-13 用户定）：原镜头标 split=1 不再给剪辑挑，切出来的段追加在后面、记 parent_idx
    """
    ALTER TABLE asset_shots ADD COLUMN split INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE asset_shots ADD COLUMN parent_idx INTEGER
    """,
    # 素材身份（2026-10-14 用户定：找素材先用本站画面）：哪站（赛历英文站名）、哪年、哪个赛段、什么类别；
    # identity_manual=1 是用户在网页上改过的，不再按标题重算
    """
    ALTER TABLE assets ADD COLUMN gp_event TEXT;
    ALTER TABLE assets ADD COLUMN season INTEGER;
    ALTER TABLE assets ADD COLUMN session TEXT;
    ALTER TABLE assets ADD COLUMN category TEXT;
    ALTER TABLE assets ADD COLUMN identity_manual INTEGER NOT NULL DEFAULT 0
    """,
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        # executescript 会隐式提交，所以逐条执行以保证整次迁移原子
        with transaction(conn):
            for statement in _split(sql):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={i}")


def _split(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
