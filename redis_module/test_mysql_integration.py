"""
Integration tests: the Redis layer + MySQLRepository against a REAL MySQL
server that has the team's schema (sql/youtube_schema_reference.sql).

They are skipped unless you ask for them:

    RUN_MYSQL_TESTS=1 DB_USER=root DB_PASSWORD=... pytest test_mysql_integration.py -v

SAFETY: the tests create and drop a separate database called
`youtube_redis_test`. They never touch your real `youtube` database.
"""
import os
import re
from pathlib import Path

import pytest

if os.getenv("RUN_MYSQL_TESTS") != "1":
    pytest.skip("set RUN_MYSQL_TESTS=1 to run the MySQL tests",
                allow_module_level=True)

pymysql = pytest.importorskip("pymysql")
pytest.importorskip("lupa")
import fakeredis

import redis_layer as rl
from repository import MySQLRepository

TEST_DB = "youtube_redis_test"
SCHEMA = Path(__file__).parent / "sql" / "youtube_schema_reference.sql"


def _admin_connection(database=None):
    return pymysql.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""),
        database=database, autocommit=True, charset="utf8mb4")


def _run(sql, params=None, fetch=False):
    conn = _admin_connection(TEST_DB)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if fetch else None
    finally:
        conn.close()


@pytest.fixture(scope="module", autouse=True)
def database():
    """Build a fresh copy of the team's schema in the test database."""
    text = SCHEMA.read_text()
    text = re.sub(r"\byoutube\b", TEST_DB, text)          # never the real DB
    statements = [
        s.strip() for s in
        "\n".join(l for l in text.splitlines()
                  if not l.strip().startswith("--")).split(";")
        if s.strip()]
    conn = _admin_connection()
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
    finally:
        conn.close()
    yield
    conn = _admin_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
    finally:
        conn.close()


@pytest.fixture
def env(database):
    """Fresh rows for every test: 4 users, 1 channel, 1 video with 2 likes."""
    for table in ("Like_Dislike", "Videos", "Channels", "Users"):
        _run(f"DELETE FROM `{table}`")
    for uid in (1, 2, 3, 4):
        _run("INSERT INTO Users (user_id, username, email) VALUES (%s, %s, %s)",
             (uid, f"user{uid}", f"user{uid}@example.com"))
    _run("INSERT INTO Channels (channel_id, channel_name, user_id) "
         "VALUES (7, 'My Channel', 1)")
    _run("INSERT INTO Videos (video_id, video_name, vid_length, description, "
         "thumbnail, view_cnt, channel_id) "
         "VALUES (42, 'My Trip', 300, 'desc', 'thumb.jpg', 120, 7)")
    _run("INSERT INTO Like_Dislike (user_id, video_id, `type`) VALUES "
         "(1, 42, 'like'), (2, 42, 'like'), (3, 42, 'dislike')")

    repo = MySQLRepository(database=TEST_DB)
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    return repo, rl.VideoRedis(client, repo)


# ----------------------------------------------------------- repository ---
def test_get_video(env):
    repo, _ = env
    video = repo.get_video(42)
    assert video["video_name"] == "My Trip"
    assert video["channel_id"] == 7
    assert repo.get_video(999) is None


def test_get_counters_counts_only_likes(env):
    repo, _ = env
    assert repo.get_counters(42) == (120, 2)      # the dislike is not counted
    assert repo.get_counters(999) is None


def test_set_rating_reports_real_changes(env):
    repo, _ = env
    assert repo.set_rating(4, 42, True) is True       # new like
    assert repo.set_rating(4, 42, True) is False      # same like again
    assert repo.set_rating(4, 42, False) is True      # unlike
    assert repo.set_rating(4, 42, False) is False     # nothing to remove
    assert repo.set_rating(3, 42, True) is True       # dislike -> like
    assert repo.set_rating(3, 42, True) is False
    assert repo.count_likes(42) == 3


def test_unlike_does_not_remove_a_dislike(env):
    repo, _ = env
    assert repo.set_rating(3, 42, False) is False
    assert _run("SELECT `type` FROM Like_Dislike WHERE user_id=3 AND video_id=42",
                fetch=True)[0][0] == "dislike"


def test_bulk_update_writes_views_only_by_default(env):
    repo, _ = env
    repo.bulk_update_counters([(42, 500, 99)])
    assert _run("SELECT view_cnt FROM Videos WHERE video_id=42",
                fetch=True)[0][0] == 500


def test_optional_likes_column(env):
    repo, _ = env
    _run("ALTER TABLE Videos ADD COLUMN like_cnt BIGINT UNSIGNED NOT NULL DEFAULT 0")
    try:
        repo.LIKES_COLUMN = "like_cnt"
        repo.bulk_update_counters([(42, 501, 7)])
        assert _run("SELECT view_cnt, like_cnt FROM Videos WHERE video_id=42",
                    fetch=True)[0] == (501, 7)
        assert repo.get_counters(42) == (501, 7)
    finally:
        repo.LIKES_COLUMN = None
        _run("ALTER TABLE Videos DROP COLUMN like_cnt")


# ------------------------------------------------------------ end to end ---
def test_full_flow_with_redis_and_mysql(env):
    repo, layer = env

    page = layer.open_video(42)                       # miss -> MySQL
    assert page["source"] == "db" and page["metadata"]["video_name"] == "My Trip"
    assert (page["views"], page["likes"]) == (120, 2)
    assert layer.open_video(42)["source"] == "cache"  # hit

    assert layer.count_view(42, "s1")["views"] == 121
    assert layer.count_view(42, "s1")["counted"] is False

    event = rl.new_event_id()
    assert layer.apply_like(4, 42, event, True) == {"applied": True, "likes": 3}
    assert layer.apply_like(4, 42, rl.new_event_id(), True)["reason"] == "no_change"
    assert layer.record_like_event(42, event, +1)["reason"] == "duplicate_event"

    # MySQL already has the like row (durable truth) ...
    assert repo.count_likes(42) == 3
    # ... and the view total reaches MySQL after the flush.
    assert _run("SELECT view_cnt FROM Videos WHERE video_id=42",
                fetch=True)[0][0] == 120
    assert layer.flush_counters() == 1
    assert _run("SELECT view_cnt FROM Videos WHERE video_id=42",
                fetch=True)[0][0] == 121


def test_cold_start_rebuilds_from_mysql(env):
    repo, layer = env
    # Redis is empty: the first like must continue from the real count (2).
    assert layer.apply_like(4, 42, rl.new_event_id(), True)["likes"] == 3


def test_reconcile_repairs_redis_from_mysql(env):
    repo, layer = env
    layer.warm_stats(42)
    _run("INSERT INTO Like_Dislike (user_id, video_id, `type`) VALUES (4, 42, 'like')")
    assert layer.get_totals(42)["likes"] == 2         # Redis does not know yet
    assert layer.reconcile_likes(42) == 3
    assert layer.get_totals(42)["likes"] == 3
