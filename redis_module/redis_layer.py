"""
redis_layer.py : the Redis part of the YouTube replica.

One class, VideoRedis, exposes the five operations of our design. The API
calls these functions and never needs to know a Redis command:

    open_video(video_id)                       -> cached metadata + live totals
    count_view(video_id, session_id)           -> one view per session
    apply_like(user_id, video_id, event_id, liked)
    record_like_event(video_id, event_id, delta)   (Redis step, safe to retry)
    warm_stats(video_id)                       -> rebuild counters from MySQL
    flush_counters()                           -> write-behind to MySQL
    reconcile_likes(video_id)                  -> repair drift (maintenance)

Redis keys (slide 18 of the deck):
    cache:video:{id}              STRING (JSON)  metadata cache, TTL ~300 s
    video:{id}:stats              HASH           views, likes     (no TTL)
    video:{id}:like_seen          HASH           applied event IDs
    view:{id}:session:{sid}       STRING "1"     counted session, TTL 24 h
Extra keys added by this implementation:
    stats:dirty                   SET   videos whose totals changed (for flush)
    lock:video:{id}               STRING short lock against cache stampede

The {id} inside braces is a Redis Cluster "hash tag": every key of one video
lands on the same node, which is what allows a script to touch several keys.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
import uuid
from pathlib import Path

import redis

from repository import VideoRepository

log = logging.getLogger("redis_layer")

# --------------------------------------------------------------------------
# Tunable settings
# --------------------------------------------------------------------------
CACHE_TTL_SECONDS = 300      # slide 18: metadata expires after 300 s
CACHE_TTL_JITTER = 30        # random extra seconds so keys do not all expire together
NEGATIVE_TTL_SECONDS = 30    # "this video does not exist" is remembered briefly
SESSION_TTL_SECONDS = 86400  # slide 18: counted session remembered 24 h
LIKE_SEEN_TTL_SECONDS = 7 * 86400  # applied event IDs kept 7 days
LOCK_TTL_MS = 5000           # max time a cache refill may hold the lock
DIRTY_KEY = "stats:dirty"
NULL_MARKER = "__null__"

# Return codes shared with the Lua scripts
DUPLICATE = -1   # already counted / already applied
COLD = -2        # stats hash missing: warm it from MySQL and retry

LUA_DIR = Path(__file__).parent / "lua"

# Release the lock only if we still own it (compare token, then delete) in
# one atomic step, so we never delete somebody else's lock.
_RELEASE_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


# --------------------------------------------------------------------------
# Key builders (one place, so a typo cannot create a ghost key)
# --------------------------------------------------------------------------
def cache_key(video_id) -> str:
    return f"cache:video:{video_id}"


def stats_key(video_id) -> str:
    return f"video:{{{video_id}}}:stats"      # -> video:{42}:stats


def like_seen_key(video_id) -> str:
    return f"video:{{{video_id}}}:like_seen"


def session_key(video_id, session_id) -> str:
    return f"view:{{{video_id}}}:session:{session_id}"


def lock_key(video_id) -> str:
    return f"lock:video:{video_id}"


def make_client(url: str = "redis://localhost:6379/0") -> redis.Redis:
    """decode_responses=True: Redis returns str instead of bytes."""
    return redis.Redis.from_url(url, decode_responses=True)


def new_event_id() -> str:
    """Unique ID of one user action. The client creates it ONCE and re-sends
    the same value when it retries; that is what makes retries harmless."""
    return uuid.uuid4().hex


class VideoNotFound(Exception):
    pass


# --------------------------------------------------------------------------
# The layer
# --------------------------------------------------------------------------
class VideoRedis:
    def __init__(self, client: redis.Redis, repo: VideoRepository):
        self.r = client
        self.repo = repo
        # register_script uploads the Lua code once and then calls it by its
        # SHA1 (EVALSHA): cheaper than sending the script text every time.
        self._count_view_script = client.register_script(
            (LUA_DIR / "count_view.lua").read_text())
        self._apply_like_script = client.register_script(
            (LUA_DIR / "apply_like.lua").read_text())
        self._release_lock_script = client.register_script(_RELEASE_LOCK)

    # ------------------------------------------------------------------
    # 1. Open a video: cache-aside for metadata + live totals from Redis
    # ------------------------------------------------------------------
    def open_video(self, video_id: int) -> dict:
        """Returns {"metadata": {...}, "views": n, "likes": n, "source": ...}
        where source is "cache" (Redis hit) or "db" (miss, MySQL was read)."""
        metadata, source = self._get_metadata(video_id)
        if metadata is None:
            raise VideoNotFound(video_id)
        totals = self.get_totals(video_id)
        return {"metadata": metadata, "source": source, **totals}

    def _get_metadata(self, video_id: int):
        key = cache_key(video_id)

        raw = self.r.get(key)
        if raw is not None:                       # HIT
            return self._decode(raw), "cache"

        # MISS. If 1000 users miss at the same instant, only one should hit
        # MySQL (cache stampede). SET NX PX = take a short-lived lock.
        token = uuid.uuid4().hex
        got_lock = self.r.set(lock_key(video_id), token, nx=True,
                              px=LOCK_TTL_MS)
        if not got_lock:
            # Somebody else is refilling the cache: wait a bit and re-check.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                time.sleep(0.05)
                raw = self.r.get(key)
                if raw is not None:
                    return self._decode(raw), "cache"
            # The lock holder is too slow: read MySQL ourselves.

        try:
            metadata = self.repo.get_video(video_id)
            if metadata is None:
                # Negative caching: do not hammer MySQL for a missing video.
                self.r.set(key, NULL_MARKER, ex=NEGATIVE_TTL_SECONDS)
                return None, "db"
            ttl = CACHE_TTL_SECONDS + random.randint(0, CACHE_TTL_JITTER)
            # default=str turns datetimes into text for JSON
            payload = json.dumps(metadata, default=str)
            self.r.set(key, payload, ex=ttl)
            # Return the SAME shape a cache hit returns (dates as text), so
            # the API sees identical data on a miss and on a hit.
            return json.loads(payload), "db"
        finally:
            if got_lock:
                self._release_lock_script(keys=[lock_key(video_id)],
                                          args=[token])

    @staticmethod
    def _decode(raw: str):
        return None if raw == NULL_MARKER else json.loads(raw)

    def invalidate_video(self, video_id: int) -> None:
        """Call after a video is edited in MySQL, so no stale copy is served."""
        self.r.delete(cache_key(video_id))

    # ------------------------------------------------------------------
    # 2. Live totals + warm-up
    # ------------------------------------------------------------------
    def get_totals(self, video_id: int) -> dict:
        views, likes = self.r.hmget(stats_key(video_id), "views", "likes")
        if views is None or likes is None:
            self.warm_stats(video_id)
            views, likes = self.r.hmget(stats_key(video_id), "views", "likes")
        return {"views": int(views), "likes": int(likes)}

    def warm_stats(self, video_id: int) -> None:
        """Rebuild video:{id}:stats from MySQL (first use, restart, flush).

        HSETNX writes a field only if it is missing, so warming can never
        overwrite a value another request has just created."""
        counters = self.repo.get_counters(video_id)
        if counters is None:
            raise VideoNotFound(video_id)
        views, likes = counters
        pipe = self.r.pipeline(transaction=True)
        pipe.hsetnx(stats_key(video_id), "views", views)
        pipe.hsetnx(stats_key(video_id), "likes", likes)
        pipe.execute()

    def _ensure_stats(self, video_id: int) -> None:
        """Warm the stats hash if (and only if) it does not exist."""
        if not self.r.exists(stats_key(video_id)):
            self.warm_stats(video_id)

    def _recover_likes_after_cold_start(self, video_id: int,
                                        event_id: str) -> int:
        """Cold start in the middle of a like: trust MySQL's COUNT (it already
        includes this event), store it, and remember the event ID so a retry
        is ignored. Returns the real number of likes."""
        self.warm_stats(video_id)                    # creates views (and likes)
        real = self.repo.count_likes(video_id)
        pipe = self.r.pipeline(transaction=True)
        pipe.hset(stats_key(video_id), "likes", real)
        pipe.hsetnx(like_seen_key(video_id), event_id, int(time.time()))
        pipe.expire(like_seen_key(video_id), LIKE_SEEN_TTL_SECONDS)
        pipe.sadd(DIRTY_KEY, video_id)
        pipe.execute()
        return real

    def _run_script(self, script, keys, args, video_id):
        """Run a counter script; if the stats hash is cold, warm it from
        MySQL and retry once."""
        result = script(keys=keys, args=args)
        if result == COLD:
            self.warm_stats(video_id)
            result = script(keys=keys, args=args)
        if result == COLD:
            raise RuntimeError(f"stats for video {video_id} could not be warmed")
        return result

    # ------------------------------------------------------------------
    # 3. Qualified view: one view per session
    # ------------------------------------------------------------------
    def count_view(self, video_id: int, session_id: str) -> dict:
        """Call once the playback is qualified (e.g. after 30 s)."""
        result = self._run_script(
            self._count_view_script,
            keys=[session_key(video_id, session_id), stats_key(video_id),
                  DIRTY_KEY],
            args=[SESSION_TTL_SECONDS, video_id],
            video_id=video_id)
        if result == DUPLICATE:
            return {"counted": False, **self.get_totals(video_id)}
        return {"counted": True, "views": result,
                "likes": self.get_totals(video_id)["likes"]}

    # ------------------------------------------------------------------
    # 4. Like / unlike
    # ------------------------------------------------------------------
    def apply_like(self, user_id: int, video_id: int, event_id: str,
                   liked: bool) -> dict:
        """Full like flow:
          1. MySQL first (durable truth): did the user's like really change?
          2. Only if yes, update the Redis counter, at most once per event.
        """
        # Make sure the counter hash exists BEFORE MySQL changes. If it were
        # rebuilt after the change, the rebuilt total would already include
        # this like and the script would add it a second time.
        self._ensure_stats(video_id)
        changed = self.repo.set_rating(user_id, video_id, liked)
        if not changed:
            return {"applied": False, "reason": "no_change",
                    "likes": self.get_totals(video_id)["likes"]}
        return self.record_like_event(video_id, event_id, +1 if liked else -1)

    def record_like_event(self, video_id: int, event_id: str, delta: int) -> dict:
        """The Redis step on its own. Safe to retry with the same event_id:
        the second call is recognised and ignored."""
        if delta not in (1, -1):
            raise ValueError("delta must be +1 or -1")
        result = self._apply_like_script(
            keys=[like_seen_key(video_id), stats_key(video_id), DIRTY_KEY],
            args=[event_id, delta, int(time.time()), LIKE_SEEN_TTL_SECONDS,
                  video_id])
        if result == COLD:
            # Redis lost the counters between the MySQL change and this call.
            # MySQL already contains this event, so its COUNT is the answer:
            # use it directly instead of adding +1/-1 a second time.
            return {"applied": True,
                    "likes": self._recover_likes_after_cold_start(video_id,
                                                                  event_id)}
        if result == DUPLICATE:
            return {"applied": False, "reason": "duplicate_event",
                    "likes": self.get_totals(video_id)["likes"]}
        return {"applied": True, "likes": result}

    # ------------------------------------------------------------------
    # 5. Write-behind: Redis totals -> MySQL (called every 5 s)
    # ------------------------------------------------------------------
    def flush_counters(self, batch_size: int = 500) -> int:
        """Copy the totals of every changed video into MySQL.
        Returns the number of videos written."""
        flushed = 0
        while True:
            # SPOP removes AND returns up to batch_size members atomically,
            # so two flushers can never take the same video.
            ids = self.r.spop(DIRTY_KEY, batch_size)
            if not ids:
                break

            pipe = self.r.pipeline(transaction=False)
            for vid in ids:
                pipe.hmget(stats_key(vid), "views", "likes")
            values = pipe.execute()

            rows = [(int(vid), int(v), int(l))
                    for vid, (v, l) in zip(ids, values)
                    if v is not None and l is not None]
            try:
                self.repo.bulk_update_counters(rows)
            except Exception:
                # MySQL is down: put the IDs back so the next flush retries.
                self.r.sadd(DIRTY_KEY, *ids)
                raise
            flushed += len(rows)
        return flushed

    # ------------------------------------------------------------------
    # 6. Maintenance: repair drift between Redis and MySQL
    # ------------------------------------------------------------------
    def reconcile_likes(self, video_id: int) -> int:
        """Set the Redis like counter to the real COUNT of likes in MySQL.
        Run it for popular videos from a periodic job (e.g. hourly). It fixes
        the rare case where MySQL was updated but the Redis step was lost."""
        self.warm_stats(video_id)  # make sure the hash exists
        real = self.repo.count_likes(video_id)
        self.r.hset(stats_key(video_id), "likes", real)
        self.r.sadd(DIRTY_KEY, video_id)
        return real


# --------------------------------------------------------------------------
# Background thread that runs flush_counters() every few seconds
# --------------------------------------------------------------------------
class CounterFlusher(threading.Thread):
    def __init__(self, layer: VideoRedis, interval: float = 5.0):
        super().__init__(daemon=True, name="counter-flusher")
        self.layer = layer
        self.interval = interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        # Event.wait returns True when stop() was called, so the loop ends fast.
        while not self._stop_event.wait(self.interval):
            try:
                n = self.layer.flush_counters()
                if n:
                    log.info("flushed %d video counters to MySQL", n)
            except Exception as exc:  # never let the thread die
                log.error("flush failed, will retry: %s", exc)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=self.interval + 1)
        try:
            self.layer.flush_counters()   # final flush on shutdown
        except Exception as exc:
            log.error("final flush failed: %s", exc)
