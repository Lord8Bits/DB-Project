"""
Tests for redis_layer.py. They run against fakeredis (an in-memory Redis that
executes the real Lua scripts), so no server is needed:

    pip install -r requirements.txt
    pytest -v
"""
import threading

import pytest

pytest.importorskip("lupa")      # fakeredis needs lupa to run Lua scripts
import fakeredis

import redis_layer as rl
from repository import InMemoryRepository


@pytest.fixture
def env():
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    repo = InMemoryRepository()
    repo.add_video(42, "My Trip", channel_id=7, views=120, likes=8)
    layer = rl.VideoRedis(client, repo)
    return client, repo, layer


# ---------------------------------------------------------------- cache ---
def test_miss_then_hit(env):
    client, repo, layer = env
    first = layer.open_video(42)
    second = layer.open_video(42)
    assert first["source"] == "db"
    assert second["source"] == "cache"
    assert repo.db_reads == 1                       # MySQL read only once
    assert 300 <= client.ttl(rl.cache_key(42)) <= 330


def test_cached_value_is_json_string(env):
    client, _, layer = env
    layer.open_video(42)
    assert client.type(rl.cache_key(42)) == "string"
    assert '"video_name": "My Trip"' in client.get(rl.cache_key(42))


def test_missing_video_is_negatively_cached(env):
    client, repo, layer = env
    for _ in range(3):
        with pytest.raises(rl.VideoNotFound):
            layer.open_video(999)
    assert repo.db_reads == 1                       # only the first call hit MySQL
    assert client.ttl(rl.cache_key(999)) <= rl.NEGATIVE_TTL_SECONDS


def test_invalidate_forces_reload(env):
    _, repo, layer = env
    layer.open_video(42)
    layer.invalidate_video(42)
    assert layer.open_video(42)["source"] == "db"
    assert repo.db_reads == 2


# ---------------------------------------------------------------- views ---
def test_view_counted_once_per_session(env):
    client, _, layer = env
    a = layer.count_view(42, "s9")
    b = layer.count_view(42, "s9")                  # replay of the same session
    c = layer.count_view(42, "s10")                 # a different session
    assert a == {"counted": True, "views": 121, "likes": 8}
    assert b["counted"] is False and b["views"] == 121
    assert c["counted"] is True and c["views"] == 122
    assert client.ttl(rl.session_key(42, "s9")) > 86000


def test_cold_start_warms_from_mysql(env):
    client, _, layer = env
    assert not client.exists(rl.stats_key(42))      # Redis is empty
    assert layer.count_view(42, "s1")["views"] == 121   # not 1!


def test_view_for_unknown_video_raises(env):
    _, _, layer = env
    with pytest.raises(rl.VideoNotFound):
        layer.count_view(999, "s1")


def test_concurrent_same_session_counts_once(env):
    _, _, layer = env
    layer.warm_stats(42)
    results = []

    def worker():
        results.append(layer.count_view(42, "same-session")["counted"])

    threads = [threading.Thread(target=worker) for _ in range(50)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1                 # exactly one winner
    assert layer.get_totals(42)["views"] == 121


# ---------------------------------------------------------------- likes ---
def test_like_then_unlike(env):
    _, _, layer = env
    assert layer.apply_like(1, 42, rl.new_event_id(), True) == \
        {"applied": True, "likes": 9}
    assert layer.apply_like(1, 42, rl.new_event_id(), False) == \
        {"applied": True, "likes": 8}


def test_second_like_by_same_user_is_ignored_by_mysql(env):
    _, _, layer = env
    layer.apply_like(1, 42, rl.new_event_id(), True)
    again = layer.apply_like(1, 42, rl.new_event_id(), True)
    assert again["applied"] is False and again["reason"] == "no_change"
    assert again["likes"] == 9


def test_retried_event_counts_once(env):
    client, _, layer = env
    layer.warm_stats(42)
    event = rl.new_event_id()
    first = layer.record_like_event(42, event, +1)
    retry = layer.record_like_event(42, event, +1)  # same event ID
    assert first == {"applied": True, "likes": 9}
    assert retry["applied"] is False and retry["reason"] == "duplicate_event"
    assert retry["likes"] == 9
    assert client.hlen(rl.like_seen_key(42)) == 1
    assert client.ttl(rl.like_seen_key(42)) > 0


def test_likes_never_negative(env):
    client, repo, layer = env
    repo.add_video(5, "Empty", 1, views=0, likes=0)
    layer.warm_stats(5)
    assert layer.record_like_event(5, "e1", -1)["likes"] == 0


def test_bad_delta_rejected(env):
    _, _, layer = env
    with pytest.raises(ValueError):
        layer.record_like_event(42, "e", 5)


# ------------------------------------------------------------- write-behind
def test_flush_writes_totals_to_mysql(env):
    _, repo, layer = env
    layer.count_view(42, "s1")
    layer.apply_like(1, 42, rl.new_event_id(), True)
    assert repo.counters[42] == (120, 8)            # MySQL not updated yet
    assert layer.flush_counters() == 1
    assert repo.counters[42] == (121, 9)
    assert layer.flush_counters() == 0              # nothing dirty any more


def test_flush_failure_keeps_videos_dirty(env):
    client, repo, layer = env
    layer.count_view(42, "s1")
    repo.fail_updates = True
    with pytest.raises(ConnectionError):
        layer.flush_counters()
    assert client.sismember(rl.DIRTY_KEY, "42")     # still queued
    repo.fail_updates = False
    assert layer.flush_counters() == 1
    assert repo.counters[42][0] == 121


def test_background_flusher(env):
    _, repo, layer = env
    layer.count_view(42, "s1")
    flusher = rl.CounterFlusher(layer, interval=0.1)
    flusher.start()
    flusher.stop()
    assert repo.counters[42][0] == 121


# ------------------------------------------------------------ reconcile ---
def test_reconcile_repairs_drift(env):
    client, repo, layer = env
    layer.warm_stats(42)
    # MySQL changed but the Redis step was lost:
    repo.set_rating(77, 42, True)
    assert layer.get_totals(42)["likes"] == 8
    assert layer.reconcile_likes(42) == 9
    assert layer.get_totals(42)["likes"] == 9


def test_cold_start_in_the_middle_of_a_like_does_not_double_count(env):
    client, repo, layer = env
    # MySQL already stored the like, but Redis has no counters at all
    # (it restarted between the MySQL change and the Redis step).
    repo.set_rating(77, 42, True)                    # real likes in MySQL: 9
    assert not client.exists(rl.stats_key(42))
    result = layer.record_like_event(42, "e-cold", +1)
    assert result == {"applied": True, "likes": 9}   # 9, not 10
    # a retry of the same event is ignored
    assert layer.record_like_event(42, "e-cold", +1)["reason"] == "duplicate_event"
    assert layer.get_totals(42)["likes"] == 9
