"""
demo.py : live demo for Presentation 4. Runs against a REAL Redis server and
prints the evidence the project guide asks for.

    docker compose up -d
    python demo.py

It uses Redis database 15 and ERASES it at the start, so it never touches
other data. MySQL is replaced by InMemoryRepository with a 50 ms delay per
query, to make the cache effect visible.
"""
import os
import statistics
import time

import redis_layer as rl
from repository import InMemoryRepository

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/15")
DB_LATENCY = 0.05          # 50 ms per simulated MySQL query
RUNS = 30


def title(text: str) -> None:
    print(f"\n{'=' * 64}\n{text}\n{'=' * 64}")


def main() -> None:
    client = rl.make_client(REDIS_URL)
    client.flushdb()
    repo = InMemoryRepository(latency=DB_LATENCY)
    repo.add_video(42, "My Trip", channel_id=7, views=120, likes=8)
    layer = rl.VideoRedis(client, repo)

    # ---------------------------------------------------------------
    title("1. Cache-aside: open a video WITHOUT cache vs WITH cache")
    miss_times, hit_times = [], []
    for _ in range(RUNS):
        layer.invalidate_video(42)                       # force a miss
        t = time.perf_counter(); layer.open_video(42)
        miss_times.append((time.perf_counter() - t) * 1000)
        t = time.perf_counter(); layer.open_video(42)    # now a hit
        hit_times.append((time.perf_counter() - t) * 1000)
    print(f"{'':16}{'average':>10}{'worst':>10}")
    print(f"{'miss (MySQL)':16}{statistics.mean(miss_times):>8.2f}ms{max(miss_times):>8.2f}ms")
    print(f"{'hit (Redis)':16}{statistics.mean(hit_times):>8.2f}ms{max(hit_times):>8.2f}ms")
    print(f"speed-up: x{statistics.mean(miss_times) / statistics.mean(hit_times):.0f}"
          f"  | key TTL now: {client.ttl(rl.cache_key(42))} s")
    print("cached value:", client.get(rl.cache_key(42)))

    # ---------------------------------------------------------------
    title("2. Views: one view per session")
    print("same session s9, three times:")
    for _ in range(3):
        print("  ", layer.count_view(42, "s9"))
    print("new session s10:", layer.count_view(42, "s10"))
    print("session marker TTL:", client.ttl(rl.session_key(42, "s9")), "s")

    # ---------------------------------------------------------------
    title("3. Likes: a retried event counts once")
    event = rl.new_event_id()          # one ID per user action
    print("user 2 likes the video          :", layer.apply_like(2, 42, event, True))
    print("API retries the Redis step (timeout), SAME event ID:")
    print("  ", layer.record_like_event(42, event, +1))
    print("user 2 unlikes                  :", layer.apply_like(2, 42, rl.new_event_id(), False))
    print("stats hash:", client.hgetall(rl.stats_key(42)))

    # ---------------------------------------------------------------
    title("4. Write-behind: Redis totals reach MySQL within 5 s")
    print("MySQL (view_cnt, like_cnt) BEFORE flush:", repo.counters[42])
    print("Redis totals                           :", layer.get_totals(42))
    flusher = rl.CounterFlusher(layer, interval=5.0)
    flusher.start()
    print("waiting 6 seconds for the background flusher ...")
    time.sleep(6)
    print("MySQL (view_cnt, like_cnt) AFTER flush :", repo.counters[42])
    flusher.stop()

    # ---------------------------------------------------------------
    title("5. Cold start: Redis lost its counters, MySQL rebuilds them")
    client.delete(rl.stats_key(42))
    print("stats hash deleted (simulates eviction/crash). Next view:")
    print("  ", layer.count_view(42, "s11"), "<- continues from the MySQL total")

    # ---------------------------------------------------------------
    title("6. Persistence check (manual)")
    print("Run:  docker restart yt-redis   then:   python demo.py --after-restart")
    print("(see README, section 'Prove persistence')")


def after_restart() -> None:
    client = rl.make_client(REDIS_URL)
    print("stats after Redis restart:", client.hgetall(rl.stats_key(42)))
    print("INFO persistence:", {k: v for k, v in client.info("persistence").items()
                                 if k in ("aof_enabled", "loading")})


if __name__ == "__main__":
    import sys
    after_restart() if "--after-restart" in sys.argv else main()
