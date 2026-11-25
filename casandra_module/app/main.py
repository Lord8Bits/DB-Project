from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID
from cassandra import DriverException
from fastapi import FastAPI, HTTPException, Query, Response
from .database import db
from .config import settings
from .helpers import new_timeuuid, parse_uuid, stable_shard, uuid_to_month
from .schemas import CommentCreate, ReplyCreate, LikeCreate

@asynccontextmanager
async def lifespan(app):
    db.connect()
    yield
    db.close()

app = FastAPI(title="YouTube Cassandra Module", version="1.0.0", lifespan=lifespan)

def now():
    return datetime.now(timezone.utc)

@app.get("/health")
def health():
    try:
        db.execute("SELECT release_version FROM system.local")
        return {"status": "ok", "database": "Cassandra"}
    except Exception as exc:
        raise HTTPException(503, str(exc))

@app.post("/videos/{video_id}/comments", status_code=201)
def create_comment(video_id: int, data: CommentCreate):
    cid, created = new_timeuuid(), now()
    month = created.strftime("%Y-%m")
    shard = stable_shard(cid, settings.comment_shards)
    db.execute("INSERT INTO comments_by_video "
      "(video_id,month,shard,comment_id,user_id,author_name,body,created_at) "
      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
      (video_id,month,shard,cid,data.user_id,data.author_name,data.body,created))
    db.execute("INSERT INTO comment_locations (comment_id,video_id,month,shard) "
      "VALUES (%s,%s,%s,%s)", (cid,video_id,month,shard))
    return {"comment_id":str(cid),"video_id":video_id,"month":month,"shard":shard,
            "user_id":data.user_id,"author_name":data.author_name,"body":data.body,
            "created_at":created.isoformat()}

@app.get("/videos/{video_id}/comments")
def list_comments(video_id: int, month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
                  limit: int = Query(20, ge=1, le=100)):
    found = []
    for shard in range(settings.comment_shards):
        rows = db.execute("SELECT comment_id,user_id,author_name,body,created_at "
          "FROM comments_by_video WHERE video_id=%s AND month=%s AND shard=%s LIMIT %s",
          (video_id,month,shard,limit))
        found.extend(list(rows))
    found.sort(key=lambda r:r["comment_id"].time, reverse=True)
    return [{"comment_id":str(r["comment_id"]),"video_id":video_id,"user_id":r["user_id"],
             "author_name":r["author_name"],"body":r["body"],
             "created_at":r["created_at"].isoformat() if r["created_at"] else None}
            for r in found[:limit]]

@app.post("/comments/{comment_id}/replies", status_code=201)
def create_reply(comment_id: str, data: ReplyCreate):
    try:
        cid = parse_uuid(comment_id)
        month = uuid_to_month(cid)
        reply_to = parse_uuid(data.reply_to_id) if data.reply_to_id else None
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    shard, rid, created = stable_shard(cid, settings.comment_shards), new_timeuuid(), now()
    db.execute("INSERT INTO replies_by_comment "
      "(comment_id,month,shard,reply_id,user_id,author_name,body,reply_to_id,created_at) "
      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
      (cid,month,shard,rid,data.user_id,data.author_name,data.body,reply_to,created))
    db.execute("INSERT INTO reply_locations (reply_id,comment_id,month,shard) "
      "VALUES (%s,%s,%s,%s)", (rid,cid,month,shard))
    return {"reply_id":str(rid),"comment_id":str(cid),"user_id":data.user_id,
            "author_name":data.author_name,"body":data.body,
            "reply_to_id":str(reply_to) if reply_to else None,"created_at":created.isoformat()}

@app.get("/comments/{comment_id}/replies")
def list_replies(comment_id: str, limit: int = Query(50, ge=1, le=200)):
    try:
        cid = parse_uuid(comment_id)
        month = uuid_to_month(cid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    shard = stable_shard(cid, settings.comment_shards)
    rows = db.execute("SELECT reply_id,user_id,author_name,body,reply_to_id,created_at "
      "FROM replies_by_comment WHERE comment_id=%s AND month=%s AND shard=%s LIMIT %s",
      (cid,month,shard,limit))
    return [{"reply_id":str(r["reply_id"]),"comment_id":str(cid),"user_id":r["user_id"],
             "author_name":r["author_name"],"body":r["body"],
             "reply_to_id":str(r["reply_to_id"]) if r["reply_to_id"] else None,
             "created_at":r["created_at"].isoformat() if r["created_at"] else None}
            for r in rows]

@app.delete("/comments/{comment_id}", status_code=204, response_class=Response,
             responses={404: {"description": "Comment not found"},
                        503: {"description": "Cassandra operation failed"}})
def delete_comment(comment_id: str) -> Response:
    try:
        cid = parse_uuid(comment_id)
        uuid_to_month(cid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    try:
        location = db.execute(
             "SELECT video_id,month,shard FROM comment_locations WHERE comment_id=%s",
             (cid,)).one()
        if location is None:
             raise HTTPException(404, "Comment not found")

        partition = (location["video_id"], location["month"], location["shard"])
        reply_rows = db.execute(
             "SELECT reply_id FROM replies_by_comment "
             "WHERE comment_id=%s AND month=%s AND shard=%s",
             (cid, location["month"], location["shard"]))
        for reply in reply_rows:
             db.execute("DELETE FROM reply_locations WHERE reply_id=%s",
                        (reply["reply_id"],))
        db.execute(
             "DELETE FROM replies_by_comment WHERE comment_id=%s AND month=%s AND shard=%s",
             (cid, location["month"], location["shard"]))
        for shard in range(settings.like_shards):
             db.execute("DELETE FROM comment_likes WHERE comment_id=%s AND shard=%s",
                        (cid, shard))
        db.execute("DELETE FROM comment_like_totals WHERE comment_id=%s", (cid,))
        db.execute(
             "DELETE FROM comments_by_video "
             "WHERE video_id=%s AND month=%s AND shard=%s AND comment_id=%s",
             (*partition, cid))
        db.execute("DELETE FROM comment_locations WHERE comment_id=%s", (cid,))
    except DriverException as exc:
        raise HTTPException(503, "Cassandra operation failed") from exc

    return Response(status_code=204)

@app.delete("/replies/{reply_id}", status_code=204, response_class=Response,
             responses={404: {"description": "Reply not found"},
                        503: {"description": "Cassandra operation failed"}})
def delete_reply(reply_id: str) -> Response:
    try:
        rid = parse_uuid(reply_id)
        uuid_to_month(rid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    try:
        location = db.execute(
             "SELECT comment_id,month,shard FROM reply_locations WHERE reply_id=%s",
             (rid,)).one()
        if location is None:
             raise HTTPException(404, "Reply not found")

        db.execute(
             "DELETE FROM replies_by_comment "
             "WHERE comment_id=%s AND month=%s AND shard=%s AND reply_id=%s",
             (location["comment_id"], location["month"], location["shard"], rid))
        db.execute("DELETE FROM reply_locations WHERE reply_id=%s", (rid,))
    except DriverException as exc:
        raise HTTPException(503, "Cassandra operation failed") from exc

    return Response(status_code=204)

def recount(cid: UUID):
    total = 0
    for shard in range(settings.like_shards):
        rows = db.execute("SELECT account_id FROM comment_likes WHERE comment_id=%s AND shard=%s",
                          (cid,shard))
        total += sum(1 for _ in rows)
    if total:
        db.execute("INSERT INTO comment_like_totals (comment_id,total_likes,refreshed_at) "
                   "VALUES (%s,%s,%s)", (cid,total,now()))
    else:
        db.execute("DELETE FROM comment_like_totals WHERE comment_id=%s", (cid,))
    return total

@app.post("/comments/{comment_id}/likes")
def like_comment(comment_id: str, data: LikeCreate):
    try:
        cid = parse_uuid(comment_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    shard = stable_shard(str(data.account_id), settings.like_shards)
    db.execute("INSERT INTO comment_likes (comment_id,shard,account_id,liked_at) "
               "VALUES (%s,%s,%s,%s)", (cid,shard,data.account_id,now()))
    return {"comment_id":str(cid),"account_id":data.account_id,"liked":True,
            "total_likes":recount(cid)}

@app.delete("/comments/{comment_id}/likes/{account_id}")
def unlike_comment(comment_id: str, account_id: int):
    try:
        cid = parse_uuid(comment_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    shard = stable_shard(str(account_id), settings.like_shards)
    db.execute("DELETE FROM comment_likes WHERE comment_id=%s AND shard=%s AND account_id=%s",
               (cid,shard,account_id))
    return {"comment_id":str(cid),"account_id":account_id,"liked":False,
            "total_likes":recount(cid)}

@app.get("/comments/{comment_id}/likes")
def get_likes(comment_id: str):
    try:
        cid = parse_uuid(comment_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"comment_id":str(cid),"total_likes":recount(cid)}
