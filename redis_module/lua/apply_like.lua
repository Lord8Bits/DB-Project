-- apply_like.lua : apply ONE like/unlike event to the counter, at most once.
--
-- KEYS[1] = video:{id}:like_seen   (hash: event_id -> timestamp)
-- KEYS[2] = video:{id}:stats       (hash with fields views, likes)
-- KEYS[3] = stats:dirty            (set of videos whose totals changed)
-- ARGV[1] = event id (unique per user action, reused on retries)
-- ARGV[2] = delta: 1 for a like, -1 for an unlike
-- ARGV[3] = current unix time (stored as the value, useful for debugging)
-- ARGV[4] = TTL of the like_seen hash in seconds
-- ARGV[5] = video id
--
-- Returns:  new likes total  -> event applied
--           -1               -> event already applied (retry ignored)
--           -2               -> stats hash is missing (cold start)

if redis.call('EXISTS', KEYS[2]) == 0 then
  return -2
end

-- HSETNX sets the field only if it does not exist and returns 1 when it
-- did, 0 when the event ID was already there => this is a retry.
if redis.call('HSETNX', KEYS[1], ARGV[1], ARGV[3]) == 0 then
  return -1
end
-- Old event IDs are useless after a while: let the whole hash expire.
redis.call('EXPIRE', KEYS[1], ARGV[4])

local likes = redis.call('HINCRBY', KEYS[2], 'likes', ARGV[2])

-- Safety net: a counter must never be negative.
if likes < 0 then
  redis.call('HSET', KEYS[2], 'likes', 0)
  likes = 0
end

redis.call('SADD', KEYS[3], ARGV[5])
return likes
