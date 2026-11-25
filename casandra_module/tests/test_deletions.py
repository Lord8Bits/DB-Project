from uuid import uuid1

import pytest
from cassandra import DriverException
from fastapi import HTTPException

from app import main
from app.helpers import uuid_to_month


class Result:
    def __init__(self, row=None, rows=()):
        self.row = row
        self.rows = rows

    def one(self):
        return self.row

    def __iter__(self):
        return iter(self.rows)


def test_delete_comment_removes_rows_from_all_partitions(monkeypatch):
    comment_id = uuid1()
    unrelated_comment_id = uuid1()
    location = {
        "video_id": 42,
        "month": uuid_to_month(comment_id),
        "shard": 2,
    }
    unrelated_location = {
        "video_id": 99,
        "month": uuid_to_month(unrelated_comment_id),
        "shard": 1,
    }
    reply_ids = [uuid1(), uuid1()]
    calls = []
    state = {
        "comment_locations": {
            comment_id: location,
            unrelated_comment_id: unrelated_location,
        },
        "comments_by_video": {
            (42, location["month"], location["shard"], comment_id),
            (99, unrelated_location["month"], unrelated_location["shard"], unrelated_comment_id),
        },
        "replies_by_comment": {
            (comment_id, location["month"], location["shard"], reply_id)
            for reply_id in reply_ids
        },
        "reply_locations": set(reply_ids),
        "comment_likes": {
            (comment_id, shard, 1000 + shard)
            for shard in range(main.settings.like_shards)
        } | {(unrelated_comment_id, 0, 9000)},
        "comment_like_totals": {
            comment_id: 0,
            unrelated_comment_id: 2,
        },
    }

    def execute(query, params=()):
        calls.append((query, params))
        if query.startswith("SELECT video_id"):
            return Result(state["comment_locations"].get(params[0]))
        if query.startswith("SELECT reply_id"):
            matching = [
                row[3] for row in state["replies_by_comment"]
                if row[:3] == params
            ]
            return Result(rows=[{"reply_id": reply_id} for reply_id in matching])
        if query.startswith("DELETE FROM reply_locations"):
            state["reply_locations"].discard(params[0])
        elif query.startswith("DELETE FROM replies_by_comment"):
            state["replies_by_comment"] = {
                row for row in state["replies_by_comment"] if row[:3] != params
            }
        elif query.startswith("DELETE FROM comment_likes"):
            state["comment_likes"] = {
                row for row in state["comment_likes"] if row[:2] != params
            }
        elif query.startswith("DELETE FROM comment_like_totals"):
            state["comment_like_totals"].pop(params[0], None)
        elif query.startswith("DELETE FROM comments_by_video"):
            state["comments_by_video"].discard(params)
        elif query.startswith("DELETE FROM comment_locations"):
            state["comment_locations"].pop(params[0], None)
        return Result()

    monkeypatch.setattr(main.db, "execute", execute)

    response = main.delete_comment(str(comment_id))

    assert response.status_code == 204
    like_deletes = [
        params for query, params in calls
        if query.startswith("DELETE FROM comment_likes")
    ]
    assert like_deletes == [(comment_id, shard) for shard in range(main.settings.like_shards)]
    assert (comment_id, location["month"], location["shard"]) in [
        params for query, params in calls
        if query.startswith("DELETE FROM replies_by_comment")
    ]
    assert all(
        ("DELETE FROM reply_locations WHERE reply_id=%s", (reply_id,)) in calls
        for reply_id in reply_ids
    )
    assert (42, location["month"], 2, comment_id) in [
        params for query, params in calls
        if query.startswith("DELETE FROM comments_by_video")
    ]
    assert any(query.startswith("DELETE FROM comment_like_totals") for query, _ in calls)
    assert any(query.startswith("DELETE FROM comment_locations") for query, _ in calls)
    assert comment_id not in state["comment_locations"]
    assert (42, location["month"], location["shard"], comment_id) not in state["comments_by_video"]
    assert not any(row[0] == comment_id for row in state["replies_by_comment"])
    assert not any(row[0] == comment_id for row in state["comment_likes"])
    assert not any(row in state["reply_locations"] for row in reply_ids)
    assert comment_id not in state["comment_like_totals"]
    assert unrelated_comment_id in state["comment_locations"]
    assert (99, unrelated_location["month"], unrelated_location["shard"],
            unrelated_comment_id) in state["comments_by_video"]
    assert (unrelated_comment_id, 0, 9000) in state["comment_likes"]
    assert state["comment_like_totals"][unrelated_comment_id] == 2


def test_delete_comment_returns_404_when_location_is_missing(monkeypatch):
    monkeypatch.setattr(main.db, "execute", lambda query, params=(): Result())

    with pytest.raises(HTTPException) as exc_info:
        main.delete_comment(str(uuid1()))

    assert exc_info.value.status_code == 404


def test_delete_reply_uses_stored_partition_and_preserves_parent(monkeypatch):
    reply_id = uuid1()
    unrelated_reply_id = uuid1()
    location = {
        "comment_id": uuid1(),
        "month": "2026-11",
        "shard": 3,
    }
    unrelated_location = {
        "comment_id": uuid1(),
        "month": "2026-10",
        "shard": 1,
    }
    calls = []
    state = {
        "reply_locations": {
            reply_id: location,
            unrelated_reply_id: unrelated_location,
        },
        "replies_by_comment": {
            (location["comment_id"], location["month"], location["shard"], reply_id),
            (unrelated_location["comment_id"], unrelated_location["month"],
             unrelated_location["shard"], unrelated_reply_id),
        },
        "comments": {location["comment_id"], unrelated_location["comment_id"]},
    }

    def execute(query, params=()):
        calls.append((query, params))
        if query.startswith("SELECT comment_id"):
            return Result(state["reply_locations"].get(params[0]))
        if query.startswith("DELETE FROM replies_by_comment"):
            state["replies_by_comment"].discard((*params[:3], params[3]))
        elif query.startswith("DELETE FROM reply_locations"):
            state["reply_locations"].pop(params[0], None)
        return Result()

    monkeypatch.setattr(main.db, "execute", execute)

    response = main.delete_reply(str(reply_id))

    assert response.status_code == 204
    assert (
        "DELETE FROM replies_by_comment "
        "WHERE comment_id=%s AND month=%s AND shard=%s AND reply_id=%s",
        (location["comment_id"], location["month"], location["shard"], reply_id),
    ) in calls
    assert any(query.startswith("DELETE FROM reply_locations") for query, _ in calls)
    assert not any("comments_by_video" in query for query, _ in calls)
    assert reply_id not in state["reply_locations"]
    assert (location["comment_id"], location["month"], location["shard"],
            reply_id) not in state["replies_by_comment"]
    assert location["comment_id"] in state["comments"]
    assert unrelated_reply_id in state["reply_locations"]
    assert (unrelated_location["comment_id"], unrelated_location["month"],
            unrelated_location["shard"], unrelated_reply_id) in state["replies_by_comment"]


def test_delete_reply_returns_404_when_location_is_missing(monkeypatch):
    monkeypatch.setattr(main.db, "execute", lambda query, params=(): Result())

    with pytest.raises(HTTPException) as exc_info:
        main.delete_reply(str(uuid1()))

    assert exc_info.value.status_code == 404


def test_delete_reply_maps_cassandra_errors_to_service_unavailable(monkeypatch):
    def execute(query, params=()):
        raise DriverException("database unavailable")

    monkeypatch.setattr(main.db, "execute", execute)

    with pytest.raises(HTTPException) as exc_info:
        main.delete_reply(str(uuid1()))

    assert exc_info.value.status_code == 503


def test_zero_like_recount_does_not_recreate_total_row(monkeypatch):
    comment_id = uuid1()
    state = {"comment_like_totals": {comment_id: 0}}
    calls = []

    def execute(query, params=()):
        calls.append((query, params))
        if query.startswith("DELETE FROM comment_like_totals"):
            state["comment_like_totals"].pop(params[0], None)
        return Result()

    monkeypatch.setattr(main.db, "execute", execute)

    assert main.recount(comment_id) == 0

    assert ("DELETE FROM comment_like_totals WHERE comment_id=%s", (comment_id,)) in calls
    assert not any(query.startswith("INSERT INTO comment_like_totals") for query, _ in calls)
    assert comment_id not in state["comment_like_totals"]
