from uuid import uuid1
from app.helpers import stable_shard, uuid_to_month

def test_month_from_timeuuid():
    assert len(uuid_to_month(uuid1())) == 7

def test_shard_is_stable():
    assert stable_shard("account-7", 16) == stable_shard("account-7", 16)
    assert 0 <= stable_shard("account-7", 16) < 16
