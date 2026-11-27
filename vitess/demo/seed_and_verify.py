import random
import pymysql
from faker import Faker

fake = Faker()
Faker.seed(42)
random.seed(42)

VTGATE_CONFIG = {
    "host": "127.0.0.1",
    "port": 15003,
    "user": "root",
    "password": "",
    "charset": "utf8mb4",
    "autocommit": True,
    "cursorclass": pymysql.cursors.DictCursor,
}


def print_header(title: str):
    print("\n" + "=" * 72)
    print(f" {title}")
    print("=" * 72)


def explain_vitess(cursor, label: str, sql: str):
    print(f"\n--- {label} ---")
    print(f"SQL: {sql}")
    cursor.execute(f"EXPLAIN FORMAT=vitess {sql}")
    for r in cursor.fetchall():
        print(
            f"  operator={r.get('operator')} | variant={r.get('variant')} | "
            f"keyspace={r.get('keyspace')} | destination={r.get('destination')}"
        )


def main():
    conn = pymysql.connect(**VTGATE_CONFIG)
    with conn.cursor() as cur:
        print_header("1. SEEDING UNSHARDED CORE DATA (yt_core)")

        categories = [
            (1, "Technology"),
            (2, "Education"),
            (3, "Gaming"),
            (4, "Music"),
            (5, "Cooking"),
        ]
        for cid, cname in categories:
            cur.execute(
                "INSERT IGNORE INTO yt_core.categories (category_id, category_name) VALUES (%s, %s)",
                (cid, cname),
            )
            cur.execute(
                "INSERT IGNORE INTO yt_videos.categories (category_id, category_name) VALUES (%s, %s)",
                (cid, cname),
            )

        tags = [
            (1, "vitess"),
            (2, "mysql"),
            (3, "sharding"),
            (4, "distributed-systems"),
            (5, "tutorial"),
            (6, "cooking"),
        ]
        for tid, tname in tags:
            cur.execute(
                "INSERT IGNORE INTO yt_core.tags (tag_id, tag_name) VALUES (%s, %s)",
                (tid, tname),
            )

        for uid in range(1, 21):
            cur.execute(
                """
                INSERT IGNORE INTO yt_core.users (user_id, username, email, country, birthday)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    uid,
                    f"{fake.user_name()}_{uid}",
                    f"user{uid}@example.com",
                    random.choice(["MA", "US", "FR", "DE", "JP"]),
                    fake.date_of_birth(minimum_age=18, maximum_age=45),
                ),
            )

        for ch_id in range(1, 11):
            cur.execute(
                """
                INSERT IGNORE INTO yt_core.channels (channel_id, user_id, channel_name, country)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    ch_id,
                    ch_id,
                    f"{fake.company()} TV",
                    random.choice(["MA", "US", "FR"]),
                ),
            )

        for uid in range(1, 21):
            for ch_id in random.sample(range(1, 11), 3):
                cur.execute(
                    "INSERT IGNORE INTO yt_core.subscriptions (user_id, channel_id) VALUES (%s, %s)",
                    (uid, ch_id),
                )

        print("Seeded users, channels, categories, tags, and subscriptions in yt_core.")

        print_header("2. SEEDING SHARDED VIDEOS (yt_videos via video_seq)")

        topics = [
            ("How Vitess Shards MySQL at YouTube Scale", "Deep dive into VTGate, VSchema, and xxhash routing", 1),
            ("Building Distributed Databases from Scratch", "CAP theorem, ACID vs BASE, and horizontal scaling", 2),
            ("Quick 10-Minute Moroccan Tagine Cooking Guide", "Traditional cooking recipe step by step", 5),
            ("Redis Caching Patterns for Viral Traffic", "Cache-aside and atomic counters explained", 1),
            ("SQL Indexing and EXPLAIN ANALYZE Masterclass", "Optimizing B-Tree and FULLTEXT indexes", 2),
        ]

        inserted_ids = []
        for i in range(40):
            title_base, desc, cat_id = topics[i % len(topics)]
            ch_id = (i % 10) + 1
            cur.execute(
                """
                INSERT INTO yt_videos.videos
                    (channel_id, category_id, video_name, description, vid_length, thumbnail, view_cnt, like_cnt)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    ch_id,
                    cat_id,
                    f"{title_base} #{i + 1}",
                    desc,
                    random.randint(180, 1800),
                    f"https://cdn.yt-clone.local/thumbs/{i + 1}.jpg",
                    random.randint(100, 500_000),
                    random.randint(10, 25_000),
                ),
            )
            new_vid_id = cur.lastrowid
            inserted_ids.append(new_vid_id)

            for tid in random.sample([1, 2, 3, 4, 5, 6], 2):
                cur.execute(
                    "INSERT IGNORE INTO yt_videos.video_tag (video_id, tag_id) VALUES (%s, %s)",
                    (new_vid_id, tid),
                )

        print(f"Inserted {len(inserted_ids)} videos (IDs {inserted_ids[0]}..{inserted_ids[-1]}).")

        print_header("3. VERIFYING SHARD DISTRIBUTION (-80 vs 80-)")
        for shard in ["-80", "80-"]:
            cur.execute(f"USE `yt_videos:{shard}`")
            cur.execute("SELECT COUNT(*) AS cnt FROM videos")
            print(f"  Shard {shard:>3} -> {cur.fetchone()['cnt']} videos")

        cur.execute("USE yt_videos")
        cur.execute("SELECT COUNT(*) AS cnt FROM yt_videos.channel_video_lookup")
        print(f"  channel_video_lookup auto-maintained rows -> {cur.fetchone()['cnt']}")

        print_header("4. VITESS QUERY ROUTING PLANS (EXPLAIN FORMAT=vitess)")
        sample_video_id = inserted_ids[0]

        explain_vitess(
            cur,
            "PATTERN 1: TARGETED (Open single video by video_id)",
            f"SELECT video_id, video_name, view_cnt FROM yt_videos.videos WHERE video_id = {sample_video_id}",
        )

        explain_vitess(
            cur,
            "PATTERN 2: LOOKUP (Channel page by channel_id = 7)",
            "SELECT video_id, video_name, upload_date FROM yt_videos.videos WHERE channel_id = 7",
        )

        explain_vitess(
            cur,
            "PATTERN 3: COLOCATED + REFERENCE JOIN (Single shard execution)",
            f"""
            SELECT v.video_id, v.video_name, c.category_name, vt.tag_id
            FROM yt_videos.videos v
            JOIN yt_videos.categories c ON v.category_id = c.category_id
            JOIN yt_videos.video_tag vt ON v.video_id = vt.video_id
            WHERE v.video_id = {sample_video_id}
            """.strip(),
        )

        explain_vitess(
            cur,
            "PATTERN 4: SCATTER-GATHER (Full-text search without shard key)",
            "SELECT video_id, video_name FROM yt_videos.videos WHERE MATCH(video_name, description) AGAINST('cooking')",
        )

    conn.close()


if __name__ == "__main__":
    main()