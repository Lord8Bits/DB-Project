import hashlib
import uuid
from datetime import datetime, timezone

_UUID_EPOCH = 0x01B21DD213814000

def new_timeuuid():
    return uuid.uuid1()

def uuid_to_month(value):
    if value.version != 1:
        raise ValueError("Comment ID must be a time-based UUID (UUID v1).")
    seconds = (value.time - _UUID_EPOCH) / 10_000_000
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m")

def stable_shard(value, shard_count):
    raw = value.bytes if isinstance(value, uuid.UUID) else str(value).encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:4], "big") % shard_count

def parse_uuid(value):
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("Invalid UUID.") from exc
