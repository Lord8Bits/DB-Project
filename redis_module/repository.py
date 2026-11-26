"""
repository.py : the ONLY place where the Redis layer talks to MySQL.

The Redis layer (redis_layer.py) depends on the small interface below, not on
MySQL directly. That gives us two benefits:
  * unit tests and the demo run without a database (InMemoryRepository);
  * the real implementation (MySQLRepository) uses the team's schema
    (database `youtube`: tables Videos and Like_Dislike), and only this file
    must change if the schema or the server (MySQL / Vitess) changes.

Settings come from environment variables (see the README):
    DB_HOST (127.0.0.1)  DB_PORT (3306)  DB_USER (root)
    DB_PASSWORD ("")     DB_NAME (youtube)
For Vitess, point DB_HOST/DB_PORT at VTGate (port 15306 in the local example).
"""
from __future__ import annotations

import os
import threading
import time
from abc import ABC, abstractmethod
from typing import Optional


class VideoRepository(ABC):
    """What the Redis layer needs from the relational database."""

    @abstractmethod
    def get_video(self, video_id: int) -> Optional[dict]:
        """Video metadata as a dict, or None if the video does not exist."""

    @abstractmethod
    def get_counters(self, video_id: int) -> Optional[tuple[int, int]]:
        """(view_cnt, like_cnt) stored in MySQL, or None if no such video."""

    @abstractmethod
    def set_rating(self, user_id: int, video_id: int, liked: bool) -> bool:
        """Durably add (liked=True) or remove (liked=False) the user's like.

        Returns True only if the membership really changed. This is what
        prevents double counting at the database level.
        """

    @abstractmethod
    def count_likes(self, video_id: int) -> int:
        """Real number of likes (source of truth, used to reconcile Redis)."""

    @abstractmethod
    def bulk_update_counters(self, rows: list[tuple[int, int, int]]) -> None:
        """Write (video_id, views, likes) rows into the videos table."""


# ---------------------------------------------------------------------------
# In-memory implementation: tests + demo (no database needed)
# ---------------------------------------------------------------------------
class InMemoryRepository(VideoRepository):
    def __init__(self, latency: float = 0.0):
        # `latency` simulates the cost of a database round trip (seconds), so
        # the demo can show the difference between a cache hit and a miss.
        self.latency = latency
        self.videos: dict[int, dict] = {}
        self.counters: dict[int, tuple[int, int]] = {}  # video_id -> (views, likes)
        self.ratings: set[tuple[int, int]] = set()      # (user_id, video_id) = liked
        self.db_reads = 0           # how many times the "database" was queried
        self.flush_calls = 0        # how many bulk updates were received
        self.fail_updates = False   # set True to simulate "MySQL is down"
        self._lock = threading.Lock()

    def add_video(self, video_id: int, name: str, channel_id: int,
                  views: int = 0, likes: int = 0) -> None:
        with self._lock:
            self.videos[video_id] = {
                "video_id": video_id,
                "video_name": name,
                "channel_id": channel_id,
            }
            self.counters[video_id] = (views, likes)
            # keep the likes table consistent with the stored like_cnt
            for i in range(likes):
                self.ratings.add((-(i + 1), video_id))

    def _sleep(self) -> None:
        if self.latency:
            time.sleep(self.latency)

    def get_video(self, video_id):
        self._sleep()
        with self._lock:
            self.db_reads += 1
            video = self.videos.get(video_id)
            return dict(video) if video else None

    def get_counters(self, video_id):
        self._sleep()
        with self._lock:
            return self.counters.get(video_id)

    def set_rating(self, user_id, video_id, liked):
        with self._lock:
            key = (user_id, video_id)
            if liked and key not in self.ratings:
                self.ratings.add(key)
                return True
            if not liked and key in self.ratings:
                self.ratings.remove(key)
                return True
            return False

    def count_likes(self, video_id):
        with self._lock:
            return sum(1 for (_, v) in self.ratings if v == video_id)

    def bulk_update_counters(self, rows):
        if self.fail_updates:
            raise ConnectionError("MySQL is down (simulated)")
        with self._lock:
            self.flush_calls += 1
            for video_id, views, likes in rows:
                self.counters[video_id] = (views, likes)


# ---------------------------------------------------------------------------
# Real implementation: the team's MySQL schema
# ---------------------------------------------------------------------------
class MySQLRepository(VideoRepository):
    # Exact table names of the team's schema. MySQL on Linux is CASE-SENSITIVE
    # for table names, so `videos` would NOT find `Videos`.
    VIDEOS_TABLE = "Videos"
    LIKE_TABLE = "Like_Dislike"

    # The schema has no like counter column in Videos, so by default the
    # number of likes is COUNTed from Like_Dislike (the source of truth) and
    # only view_cnt is written back by the flush. If the team later adds a
    # column such as like_cnt to Videos, set this to its name and the flush
    # will also persist likes there.
    LIKES_COLUMN = None

    def __init__(self, host=None, port=None, user=None, password=None,
                 database=None):
        self._cfg = dict(
            host=host or os.getenv("DB_HOST", "127.0.0.1"),
            port=int(port or os.getenv("DB_PORT", "3306")),
            user=user or os.getenv("DB_USER", "root"),
            password=(password if password is not None
                      else os.getenv("DB_PASSWORD", "")),
            database=database or os.getenv("DB_NAME", "youtube"),
            autocommit=True,      # every statement commits immediately
            charset="utf8mb4",
        )

    # -- helpers ------------------------------------------------------------
    def _connect(self):
        import pymysql  # imported here so the unit tests do not need it
        return pymysql.connect(**self._cfg)

    @property
    def _vt(self) -> str:      # quoted Videos table name
        return f"`{self.VIDEOS_TABLE}`"

    @property
    def _lt(self) -> str:      # quoted Like_Dislike table name
        return f"`{self.LIKE_TABLE}`"

    # -- interface ------------------------------------------------------------
    def get_video(self, video_id):
        conn = self._connect()
        try:
            with conn.cursor(pymysql_dict_cursor()) as cur:
                cur.execute(
                    "SELECT video_id, video_name, vid_length, description, "
                    "thumbnail, upload_date, channel_id "
                    f"FROM {self._vt} WHERE video_id = %s", (video_id,))
                return cur.fetchone()
        finally:
            conn.close()

    def get_counters(self, video_id):
        if self.LIKES_COLUMN:
            sql = (f"SELECT view_cnt, `{self.LIKES_COLUMN}` "
                   f"FROM {self._vt} WHERE video_id = %s")
        else:
            # One round trip: the video's views plus its number of likes.
            sql = ("SELECT v.view_cnt, "
                   f"(SELECT COUNT(*) FROM {self._lt} AS l "
                   "WHERE l.video_id = v.video_id AND l.`type` = 'like') "
                   f"FROM {self._vt} AS v WHERE v.video_id = %s")
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (video_id,))
                row = cur.fetchone()
                return (int(row[0]), int(row[1])) if row else None
        finally:
            conn.close()

    def set_rating(self, user_id, video_id, liked):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                if liked:
                    # Insert the like; if the user already has a rating on
                    # this video (a dislike, or the same like), update it.
                    # Assignments run left to right: rated_at is decided
                    # using the OLD type, then type becomes 'like'.
                    # rowcount is 0 when the row already was a like.
                    cur.execute(
                        f"INSERT INTO {self._lt} "
                        "(user_id, video_id, `type`, rated_at) "
                        "VALUES (%s, %s, 'like', NOW()) "
                        "ON DUPLICATE KEY UPDATE "
                        "rated_at = IF(`type` = 'like', rated_at, NOW()), "
                        "`type` = 'like'", (user_id, video_id))
                else:
                    cur.execute(
                        f"DELETE FROM {self._lt} "
                        "WHERE user_id = %s AND video_id = %s "
                        "AND `type` = 'like'", (user_id, video_id))
                return cur.rowcount > 0
        finally:
            conn.close()

    def count_likes(self, video_id):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) FROM {self._lt} "
                    "WHERE video_id = %s AND `type` = 'like'", (video_id,))
                return int(cur.fetchone()[0])
        finally:
            conn.close()

    def bulk_update_counters(self, rows):
        if not rows:
            return
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                if self.LIKES_COLUMN:
                    cur.executemany(
                        f"UPDATE {self._vt} SET view_cnt = %s, "
                        f"`{self.LIKES_COLUMN}` = %s WHERE video_id = %s",
                        [(views, likes, vid) for vid, views, likes in rows])
                else:
                    cur.executemany(
                        f"UPDATE {self._vt} SET view_cnt = %s "
                        "WHERE video_id = %s",
                        [(views, vid) for vid, views, _likes in rows])
        finally:
            conn.close()


def pymysql_dict_cursor():
    import pymysql.cursors
    return pymysql.cursors.DictCursor
