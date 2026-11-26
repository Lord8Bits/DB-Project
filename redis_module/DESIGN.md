# Redis layer – design notes (study guide for the author)

> This file explains the design for **you** (Brahim). The file for the teammate who integrates is `README.md`.

This folder is the Redis part of the project. It implements exactly what slides 4, 5, 10, 18 and 19 of the Blueprint deck promise. **Read this file first, then read the code.** You must be able to explain every part in the final presentation.

## 1. Files

| File | Role |
|---|---|
| `redis_layer.py` | The layer: five operations + helpers (the file to study most) |
| `lua/count_view.lua` | Atomic "one view per session" script |
| `lua/apply_like.lua` | Atomic "apply a like event at most once" script |
| `repository.py` | The only code that talks to MySQL (+ an in-memory fake) |
| `redis.conf` | Server settings: persistence, memory limit, eviction |
| `docker-compose.yml` | Starts Redis 7 with that config |
| `test_redis_layer.py` | 18 unit tests, run without a server |
| `test_mysql_integration.py` | 9 tests against a real MySQL with the team's schema (opt-in) |
| `sql/youtube_schema_reference.sql` | Copy of the team's schema, used by the integration tests |
| `demo.py` | Live demo that prints the evidence the guide asks for |

## 2. Quick start

```bash
pip install -r requirements.txt
pytest -v                      # 17 tests, no server needed
docker compose up -d           # real Redis
python demo.py                 # the demo (uses Redis database 15, erases it)
docker compose exec redis redis-cli -n 15   # look at the keys yourself
```

Inside `redis-cli -n 15` try: `KEYS *`, `TYPE cache:video:42`, `TTL cache:video:42`, `HGETALL video:{42}:stats`.

## 3. How data is stored (slide 18)

| Key | Type | Content | Lifetime | Why this type |
|---|---|---|---|---|
| `cache:video:42` | STRING | JSON of the video metadata | 300 s (+0–30 s random) | Read/replaced as a whole |
| `video:{42}:stats` | HASH | `views`, `likes` | none | Two counters in one key, each changed independently with `HINCRBY` |
| `video:{42}:like_seen` | HASH | event ID → timestamp | 7 days | One lookup answers "seen this event?" |
| `view:{42}:session:s9` | STRING | `1` | 24 h | Only its existence matters |
| `stats:dirty` *(added)* | SET | IDs of videos changed since last flush | none | Tells the flusher what to write |
| `lock:video:42` *(added)* | STRING | random token | 5 s | Stops a cache stampede |

`{42}` in braces is a **hash tag**: in Redis Cluster only the text inside the braces decides the server, so all keys of video 42 live on the same node and one script may use several of them.

## 4. The five operations, step by step

### 4.1 Open a video – cache-aside (`open_video`)
1. `GET cache:video:42`. If found → return it (**hit**, ~0.1 ms).
2. If missing (**miss**): take a lock with `SET lock:video:42 <token> NX PX 5000`. `NX` = only if absent, so exactly one request wins.
3. The winner reads MySQL through VTGate, stores the JSON with `SET ... EX 300+jitter`, releases the lock.
4. The losers wait (50 ms steps, max 1 s) and read the cache the winner filled.
5. If the video does not exist, a marker `__null__` is cached for 30 s (**negative caching**) so repeated requests do not hit MySQL.
6. Live totals come from `HMGET video:{42}:stats views likes`.

*Why jitter?* If 10,000 keys were created together they would all expire together and send a wave of queries to MySQL.
*Why a token on the lock?* The release script deletes the lock only if the token is still ours (never someone else's lock).

### 4.2 Count a view (`count_view` → `lua/count_view.lua`)
The script runs as one atomic unit:
1. `EXISTS video:{42}:stats`; if not, return `-2` (cold) so Python warms it from MySQL and retries.
2. `SET view:{42}:session:s9 1 NX EX 86400`: succeeds only the first time.
3. If it failed → return `-1` (already counted).
4. Otherwise `HINCRBY ... views 1`, `SADD stats:dirty 42`, return the new total.

*Why a script and not three commands?* Between "check the session" and "increment", another request could slip in and both would count. A Lua script is executed alone: no other command runs in the middle.

### 4.3 Like / unlike (`apply_like`, `record_like_event` → `lua/apply_like.lua`)
Two safety layers:
1. **MySQL first** (`set_rating`): the durable truth. It returns `True` only if the user's like really changed. Liking twice never reaches Redis.
2. **Event ID in Redis**: `HSETNX video:{42}:like_seen <event_id> <time>` returns 1 only the first time. If the API retries the Redis step (timeout, message delivered twice) the same event ID is recognised and ignored.
Then `HINCRBY ... likes ±1`, with a floor at 0.

The client creates the event ID **once per user action** (`new_event_id()`) and re-sends the same value on retries.

### 4.4 Cold start (`warm_stats`)
`video:{42}:stats` has no TTL, but Redis can still lose it (crash without persistence, manual delete). If a script finds it missing it returns `-2`; Python loads `view_cnt` and the number of likes (a `COUNT` on `Like_Dislike`) from MySQL with `HSETNX` (writes a field only if missing, so it never overwrites a fresher value) and retries. Without this the counter would restart from 0.

### 4.5 Write-behind (`flush_counters`, `CounterFlusher`)
Every 5 s a background thread:
1. `SPOP stats:dirty 500` takes up to 500 changed video IDs **and removes them atomically**.
2. Reads their totals with `HMGET` (pipelined).
3. Sends one `UPDATE Videos SET view_cnt=… WHERE video_id=…` per video. Likes are not written back: `Like_Dislike` is already their durable home.
4. If MySQL fails, the IDs are put back with `SADD` and the next flush retries. Nothing is lost.

*Why write-behind?* A viral video can get thousands of views per second. Writing each one to MySQL would overload it; Redis absorbs them and MySQL receives one update per video every 5 s.

### 4.6 Reconcile (`reconcile_likes`)
Rare failure: MySQL committed a like, then the app crashed before the Redis step. Redis is then 1 too low. A periodic job recomputes `COUNT(*)` from MySQL and overwrites the counter.

## 5. Server settings (`redis.conf`)
- `appendonly yes` + `appendfsync everysec`: every write is logged; after a crash at most ~1 s is lost. Counters survive a restart (verified).
- `save …`: periodic RDB snapshots as a second safety net.
- `maxmemory 256mb` + `maxmemory-policy volatile-lru`: when full, Redis deletes only keys **with a TTL** (cache, sessions, event IDs), least recently used first. `video:{id}:stats` has no TTL, so it is never evicted.

## 6. Differences from the slides (say them openly)
- `like_seen` gets a 7-day TTL (the slide shows none) so it cannot grow forever.
- Cache TTL = 300 s + random 0–30 s.
- Added `stats:dirty` (flush queue) and `lock:video:{id}` (stampede lock).
- The code returns `-2` for "cold" and re-warms from MySQL.

## 7. Schema compatibility (updated)
The code now targets the team's real MySQL schema (database `youtube`). See `README.md`, section 5 and section 12, for the exact tables used and the findings about the schema. Key points: tables are `Videos` and `Like_Dislike` (case-sensitive on Linux), likes are counted from `Like_Dislike`, and only `view_cnt` is written back by the flush (no `like_cnt` column is needed).

## 8. Known limits (good answers to "what if…?")
- `stats:dirty` is one key. On a real Redis Cluster it would be split per slot/shard. Fine for one server.
- The like path is "MySQL then Redis": a crash between the two leaves Redis low until `reconcile_likes` runs.
- Bug found and fixed with the real database: if Redis had lost its counters, a like was counted twice (MySQL already held it when the counter was rebuilt). `apply_like` now warms the counters BEFORE the MySQL change, and `record_like_event` uses MySQL's COUNT when it meets a cold start.
- Under extreme memory pressure `like_seen` (has a TTL) could be evicted and a very late retry counted twice.
- Redis is a single server here. Production would add replication + Sentinel/Cluster.
- `MySQLRepository` opens one connection per call (simple). Use a pool in production. It is not tested against a live Vitess; it is tested against a real MySQL-compatible server (MariaDB 10.11) loaded with the team's schema, and the Redis logic against a real Redis 7.

## 9. Plugging it into the API
See `README.md`, section 6. The database settings come from environment variables (`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`).
