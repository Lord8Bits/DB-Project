# YouTube Cassandra Module

Python + FastAPI implementation of the Cassandra responsibilities in Presentation 3:
- Comments by video (`comments_by_video`)
- Replies by comment (`replies_by_comment`)
- Comment likes (`comment_likes`)
- Recounted totals (`comment_like_totals`)

MySQL/Vitess and Redis are not included; this service handles the Cassandra part only.

## Run

Requires Docker Desktop / Docker Engine and Docker Compose.

```bash
docker compose up --build
```

Cassandra's first startup can take a few minutes. Open:
- API docs: http://localhost:8000/docs
- Health: http://localhost:8000/health

Stop: `docker compose down`
Delete demo data: `docker compose down -v` (deletes the Cassandra volume).

## Test the API

Create a comment:
```bash
curl -X POST http://localhost:8000/videos/42/comments -H "Content-Type: application/json" -d '{"user_id":7,"author_name":"Sara","body":"Great video!"}'
```
Copy the returned `comment_id` and `month`, then list comments:
```bash
curl "http://localhost:8000/videos/42/comments?month=2026-11&limit=20"
```
Create a reply:
```bash
curl -X POST http://localhost:8000/comments/COMMENT_ID/replies -H "Content-Type: application/json" -d '{"user_id":9,"author_name":"Ali","body":"Thank you!"}'
```
List replies:
```bash
curl "http://localhost:8000/comments/COMMENT_ID/replies"
```
Like a comment:
```bash
curl -X POST http://localhost:8000/comments/COMMENT_ID/likes -H "Content-Type: application/json" -d '{"account_id":7}'
```
Unlike:
```bash
curl -X DELETE http://localhost:8000/comments/COMMENT_ID/likes/7
```
Get/recount total likes:
```bash
curl "http://localhost:8000/comments/COMMENT_ID/likes"
```
Delete a reply (the parent comment is kept):
```bash
curl -i -X DELETE http://localhost:8000/replies/REPLY_ID
```
Delete a comment (its replies and likes are deleted too):
```bash
curl -i -X DELETE http://localhost:8000/comments/COMMENT_ID
```
Both delete endpoints return `204 No Content` on success, `404 Not Found` when the ID is unknown, and `503 Service Unavailable` when Cassandra reports an operation failure.

## Data model

### Comments
Partition key: `(video_id, month, shard)`. Clustering column: `comment_id DESC`.
The API generates UUID v1 time-based IDs, assigns each comment deterministically to one of 4 shards, queries each shard and merges the newest comments.
`comment_locations` stores the complete partition key by `comment_id` so a delete can locate the row without a table scan.

### Replies
Partition key: `(comment_id, month, shard)`. Clustering column: `reply_id ASC`.
The comment UUID v1 gives the month; the same deterministic shard function locates the reply partition.
`reply_locations` stores the reply's parent partition key by `reply_id`; deleting a reply removes only the reply and its location row.

### Comment likes
Partition key: `(comment_id, shard)`, clustering key: `account_id`.
Shard assignment is `SHA256(account_id) % 16`. The same account/comment pair maps to one row, so retries do not create duplicate rows.

`comment_like_totals` stores positive totals; a zero-count recount removes the row. The demo recounts all 16 like partitions on demand. This is a demonstration-oriented implementation, not a production-scale counting design.
Deleting a comment removes its reply partition, every configured `comment_likes` shard, its cached total, and its comment row.

The location tables are populated for comments and replies created after this schema is applied. Existing rows created before the location tables existed cannot be deleted by ID until the location rows are backfilled from their original partition keys; those keys cannot be reconstructed from the IDs alone.

## Configuration
See `.env.example`. Local keyspace uses `SimpleStrategy` with replication factor 1. For a multi-node cluster, use a topology-aware replication strategy and suitable replication factor.

## Tests
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
```
The tests cover UUID/shard helpers and the delete endpoint behavior using a mocked database; Cassandra does not need to be running.

## Scope
Educational baseline. Authentication, moderation, editing, cursor pagination, rate limiting and multi-region deployment are not included.
