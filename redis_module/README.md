# Redis layer: integration guide

For the teammate who connects this to the API. Redis owner: **Brahim EL HANAFI**. Project: YouTube replica, DBS, Prof. Sara EL-ATEIF.

## 0. In one minute

Your API never writes a Redis command. It creates **one** `VideoRedis` object and calls five functions that return plain dictionaries:

| You call | When | Redis does |
|---|---|---|
| `open_video(video_id)` | A user opens a video page | Serves metadata from cache (MySQL only on a miss) plus live views and likes |
| `count_view(video_id, session_id)` | Playback is qualified (e.g. after 30 s) | Counts **one view per session** |
| `apply_like(user_id, video_id, event_id, liked)` | A user likes or unlikes | Stores the like in MySQL, then updates the counter **once per event** |
| `invalidate_video(video_id)` | After a video is edited in MySQL | Drops the cached copy |
| `get_totals(video_id)` | You only need views and likes | Returns both |

A background thread (`CounterFlusher`) copies the changed view totals into MySQL every 5 seconds.

**Your 5 steps:** (1) set up Redis and the packages (section 3), (2) set the database variables (section 4), (3) create the objects once at API startup (section 6), (4) call the five functions from your endpoints, (5) make the front end send `session_id` and `event_id` (section 8). Then tick the checklist in section 13.

## 1. What is in the folder

| File | What it is |
|---|---|
| `redis_layer.py` | The layer. **The file the API imports.** |
| `repository.py` | The only code that talks to MySQL, plus an in-memory fake for tests |
| `lua/count_view.lua`, `lua/apply_like.lua` | Two small scripts that make counting atomic. Keep them in `lua/` next to `redis_layer.py` |
| `redis.conf` | Redis server settings (persistence, memory limit, eviction) |
| `docker-compose.yml` | Starts Redis 7 with that config |
| `requirements.txt` | Python packages |
| `test_redis_layer.py` | 18 unit tests (no server needed) |
| `test_mysql_integration.py` | 9 tests against a real MySQL with the team's schema (opt-in) |
| `sql/youtube_schema_reference.sql` | Copy of the team's schema, used only by the integration tests. **Your own SQL file stays the source of truth** |
| `demo.py` | Live demo that prints the evidence for Presentation 4 |
| `DESIGN.md` | Long design notes (reasons, likely professor questions) |

Send and keep the folder together: `redis_layer.py` finds `lua/` and `repository.py` by relative path.

## 2. Prerequisites
- Python 3.10 or newer
- Docker (to run Redis)
- A MySQL server with the team's schema (database `youtube`)

## 3. Setup (about 10 minutes)

```bash
cd redis_layer
python3 -m venv .venv
source .venv/bin/activate              # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Start Redis (pick one):

```bash
# A) with Docker Compose
docker compose up -d

# B) without the Compose plugin
docker run -d --name yt-redis -p 6379:6379 \
  -v "$PWD/redis.conf:/usr/local/etc/redis/redis.conf:ro" \
  -v redis-data:/data \
  redis:7 redis-server /usr/local/etc/redis/redis.conf
```

(Add `sudo` before `docker` if you get "permission denied".) Check it:

```bash
docker exec -it yt-redis redis-cli ping      # prints PONG
pytest -v                                    # 18 passed, 1 skipped (the MySQL file is opt-in)
python demo.py                               # live demo; uses Redis database 15 and ERASES it
```

`requirements.txt` must contain plain lines (no backticks, no markdown):

```
redis>=5.0
pymysql>=1.1
pytest>=8.0
fakeredis[lua]>=2.23
```

`redis` and `pymysql` are needed at runtime. `pytest` and `fakeredis` are only for the tests.

## 4. Configuration

All settings come from environment variables, so there are no passwords in the code.

| Variable | Default | Meaning |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis address and database number |
| `DB_HOST` | `127.0.0.1` | MySQL host |
| `DB_PORT` | `3306` | MySQL port (`15306` for the Vitess VTGate example) |
| `DB_USER` | `root` | MySQL user |
| `DB_PASSWORD` | empty | MySQL password |
| `DB_NAME` | `youtube` | Database name from the team's script |

`REDIS_URL` is read by your API code (section 6). The `DB_*` variables are read by `MySQLRepository()` itself.

## 5. How it uses the team's database schema

Only these tables and columns are touched:

| Table | Columns | Used for |
|---|---|---|
| `Videos` | `video_id`, `video_name`, `vid_length`, `description`, `thumbnail`, `upload_date`, `channel_id` | Metadata that is cached |
| `Videos` | `view_cnt` | Starting value of the view counter; **written back** by the flush |
| `Like_Dislike` | `user_id`, `video_id`, `type`, `rated_at` | The durable like. Likes are **counted from here** |

Decisions you should know about:
- **No schema change is needed.** `Videos` has no like counter column, so the number of likes is `COUNT(*)` of rows with `type = 'like'` in `Like_Dislike`. That table is the source of truth, and Redis only caches the total. The flush therefore writes back **only `view_cnt`**.
- **Optional:** if the team later adds a like counter column to `Videos`, set `MySQLRepository.LIKES_COLUMN = 'your_column_name'` and the flush will also write likes there.
- **Table names are case-sensitive on Linux.** The code uses exactly `Videos` and `Like_Dislike`. If a table is renamed, change `VIDEOS_TABLE` or `LIKE_TABLE` at the top of `MySQLRepository`.
- A like when the user had a dislike turns the row into a like. An unlike removes only a `like` row. Dislike counts are not tracked in Redis.
- The tables `Users`, `Channels`, `Comments`, `Subscribe`, `View`, `Playlist`, `Contain`, `Tags` and `Tagged` are **not touched**.
- `Channels.total_view_cnt` is **not** updated by this layer.

## 6. Connecting it to the API

Create **one** `VideoRedis` object and start **one** flusher when the API starts:

```python
import os
from redis_layer import make_client, VideoRedis, CounterFlusher, VideoNotFound
from repository import MySQLRepository

layer = VideoRedis(
    make_client(os.getenv("REDIS_URL", "redis://localhost:6379/0")),
    MySQLRepository())                 # reads the DB_* variables

flusher = CounterFlusher(layer, interval=5)
flusher.start()                        # once, at startup
# at shutdown: flusher.stop()          # does a last flush
```

Example endpoints (Flask shown; any framework works the same way):

```python
from flask import Flask, jsonify, request
app = Flask(__name__)

@app.get("/videos/<int:video_id>")
def get_video(video_id):
    try:
        return jsonify(layer.open_video(video_id))
    except VideoNotFound:
        return jsonify(error="video not found"), 404

@app.post("/videos/<int:video_id>/view")
def post_view(video_id):
    body = request.get_json()
    return jsonify(layer.count_view(video_id, body["session_id"]))

@app.post("/videos/<int:video_id>/like")
def post_like(video_id):
    b = request.get_json()
    return jsonify(layer.apply_like(b["user_id"], video_id,
                                    b["event_id"], b.get("liked", True)))
```

Notes:
- In Flask debug mode use `use_reloader=False`, otherwise two flusher threads start.
- The Redis client and the layer are safe to share between request threads.
- After you update a video in MySQL (title, description, thumbnail), call `layer.invalidate_video(video_id)`.

## 7. The function contract

| Function | Returns |
|---|---|
| `open_video(video_id)` | `{"metadata": {...}, "source": "cache" or "db", "views": n, "likes": n}`. Raises `VideoNotFound` |
| `count_view(video_id, session_id)` | `{"counted": true/false, "views": n, "likes": n}` |
| `apply_like(user_id, video_id, event_id, liked)` | `{"applied": true, "likes": n}` or `{"applied": false, "reason": "no_change" or "duplicate_event", "likes": n}` |
| `invalidate_video(video_id)` | nothing |
| `get_totals(video_id)` | `{"views": n, "likes": n}` |
| `reconcile_likes(video_id)` | the real number of likes from MySQL (maintenance job) |
| `flush_counters()` | number of videos written (the flusher calls it; you normally do not) |

Details:
- `metadata` has the same shape on a miss and on a hit; dates are text such as `2026-11-20 10:00:00`.
- `liked=True` means like, `liked=False` means unlike.
- `VideoNotFound` is raised for an unknown video (including `count_view` and `apply_like`).
- If Redis itself is down, the functions raise `redis.exceptions.RedisError`. Decide in the API whether to answer with an error or to fall back to reading MySQL directly. This layer does not hide the failure.

## 8. What the front end must do

- **`session_id`:** a random string created **when the player starts**. Send it with the view request. The same session never counts twice (remembered for 24 h).
- **When to send the view:** after the playback is "qualified" (the deck says for example 30 seconds), not when the page opens.
- **`event_id`:** a random string created **once per click** on like or unlike (`crypto.randomUUID()` in JavaScript). **If the request is retried, send the same `event_id` again.** A new ID on retry breaks the protection.
- Send the real `user_id` of the logged-in user with a like.

## 9. How the data is stored in Redis

| Key | Type | Content | Lifetime |
|---|---|---|---|
| `cache:video:42` | STRING | JSON of the video metadata | about 300 s |
| `video:{42}:stats` | HASH | `views`, `likes` | none |
| `video:{42}:like_seen` | HASH | IDs of applied like events | 7 days |
| `view:{42}:session:s9` | STRING | `1` (marker) | 24 h |
| `stats:dirty` | SET | videos changed since the last flush | none |
| `lock:video:42` | STRING | short lock against cache stampede | 5 s |

Look inside while testing:

```bash
docker exec -it yt-redis redis-cli
KEYS *
HGETALL video:{42}:stats
TTL cache:video:42
```

## 10. Guarantees and failure behavior

| Situation | What happens |
|---|---|
| Same session sent twice | Counted once |
| Same `event_id` sent twice | Counted once |
| User likes twice | MySQL says "no change", Redis is not touched |
| Redis restarts | The append-only file restores the data. If something is still missing it is rebuilt from MySQL |
| Redis loses the counters in the middle of a like | The like count is taken from MySQL's `COUNT` and is **not** counted twice (bug found and fixed with the real database) |
| MySQL is down during a flush | The changed videos stay queued and are retried on the next flush; nothing is lost |
| Cache entry is stale | Lives at most about 5 minutes, or call `invalidate_video` |
| Many users miss the cache at once | One request reloads, the others wait (no stampede) |

Limits to be aware of:
- MySQL `view_cnt` can lag Redis by up to about 5 seconds.
- If the app crashes after MySQL stores a like but before Redis is updated, the Redis like count is one too low until `reconcile_likes(video_id)` runs. Run it from a periodic job for popular videos.
- One Redis server only. Production would add replication (Sentinel) or a Cluster.

## 11. Tests

```bash
pytest -v                       # 18 unit tests, in-memory Redis, no server
```

Against a **real MySQL** with the team's schema (opt-in):

```bash
RUN_MYSQL_TESTS=1 DB_USER=root DB_PASSWORD=yourpassword pytest -v
```

These 9 extra tests build their own database called **`youtube_redis_test`** and drop it at the end. They never touch your real `youtube` database. Last run: **27 passed** (18 unit + 9 on MariaDB 10.11 with the team's exact schema).

## 12. Notes for the schema owners (observations only, nothing was changed)

These came up while testing against the schema script. They are for the team to decide.

1. **Likes counting speed.** `COUNT` of likes for a video runs only on a cold start or in `reconcile_likes`. `Like_Dislike` has a foreign-key index on `video_id`, but `type` is not in it, so each counted row needs an extra lookup. Fine for thousands of likes per video; for millions the team may want an index that covers `video_id` and `type`.
2. **Table-name case.** Names such as `Videos` and `Like_Dislike` are case-sensitive on Linux. Everyone should write them exactly as in the script.
3. **The `View` table** has the key `(user_id, video_id)`: one row per user and video. Redis counts one view per playback **session**. Decide what `View` means (watch history, or "has watched") and keep `Videos.view_cnt` as the total of counted views. The Redis layer does not read or write `View`.
4. **Deck and script differ.** Slides show BIGINT ids, `VARCHAR(255)` video names, `VARCHAR(2048)` thumbnails, `CHAR(2)` countries, a `likes_comment` column and a likes table called `Rating` or `video_like`. The script uses `INT UNSIGNED`, `VARCHAR(100)`, `VARCHAR(500)`, `VARCHAR(60)`, no `likes_comment` and `Like_Dislike`. Align the slides and the script before presenting.
5. **`DROP DATABASE IF EXISTS youtube;`** at the top of the script erases the whole database if it is run by mistake on shared data.
6. **Comments** exist in the MySQL script but the architecture puts comments in Cassandra. Say which store owns them.
7. **Vitess.** The script is a single-server design with cross-table foreign keys. A sharded Vitess layout (slide 10) cannot keep foreign keys across shards, so the sharded version needs its own schema. The queries in `repository.py` all filter by `video_id`, which is what sharding needs.

## 13. Integration checklist

- [ ] Folder unzipped, venv created, `pip install -r requirements.txt` done
- [ ] Redis running (`PONG`), `pytest -v` shows 18 passed
- [ ] `DB_*` variables set; `RUN_MYSQL_TESTS=1 pytest -v` shows 27 passed against your MySQL
- [ ] One `VideoRedis` object and one `CounterFlusher` created at API startup (and `flusher.stop()` at shutdown)
- [ ] Endpoints call the functions in section 7
- [ ] Front end creates `session_id` per playback and `event_id` per click, and reuses `event_id` on retry
- [ ] Video-edit endpoint calls `invalidate_video`
- [ ] One real view, one real like and one flush checked in MySQL (`view_cnt` changed, a row exists in `Like_Dislike`)
- [ ] API decides what to do if Redis is down

## 14. Troubleshooting

| Problem | Fix |
|---|---|
| `ConnectionError ... 6379` | Redis is not running: `docker ps`, then start it (section 3) |
| Port 6379 already in use | Stop the other Redis, or map another port and change `REDIS_URL` |
| `ModuleNotFoundError: redis_layer` | Run Python from inside the folder, or add it to `PYTHONPATH` |
| `ModuleNotFoundError: pymysql` | `pip install pymysql` |
| `Table 'youtube.videos' doesn't exist` | Table names are case-sensitive: the code uses `Videos` |
| `Access denied for user` | Check `DB_USER` and `DB_PASSWORD` |
| `Unknown database 'youtube'` | Set `DB_NAME` or run the team's schema script |
| MySQL tests skipped | Set `RUN_MYSQL_TESTS=1` |
| `pip install` fails on requirements | Remove any backticks or markdown from `requirements.txt` |
| Views in MySQL are behind | Normal: the flush runs every 5 s. Make sure the flusher was started |

## 15. AI usage note

This layer was generated with AI (Claude), with the professor's permission given in class for the Redis and Cassandra parts, then tested on a real Redis 7 and a real MySQL-compatible server. Record the prompts and the references in `AI_PROMPTS.md`, as the project guide requires.
