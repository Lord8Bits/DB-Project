-- count_view.lua : count ONE view per playback session, atomically.
--
-- KEYS[1] = view:{id}:session:{sid}   (marker string, expires after 24 h)
-- KEYS[2] = video:{id}:stats          (hash with fields views, likes)
-- KEYS[3] = stats:dirty               (set of videos whose totals changed)
-- ARGV[1] = session TTL in seconds
-- ARGV[2] = video id (stored in the dirty set for the write-behind flush)
--
-- Returns:  new views total  -> the view was counted
--           -1               -> this session was already counted (ignored)
--           -2               -> stats hash is missing (cold start): the
--                               caller must warm it from MySQL and retry

-- 1) Never increment a hash that does not exist yet: it would start at 0
--    instead of the real total stored in MySQL.
if redis.call('EXISTS', KEYS[2]) == 0 then
  return -2
end

-- 2) SET ... NX only succeeds if the key does not exist. Success means
--    "first time we see this session". Failure returns false in Lua.
local first_time = redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[1])
if not first_time then
  return -1
end

-- 3) Count the view and remember that this video needs flushing to MySQL.
local views = redis.call('HINCRBY', KEYS[2], 'views', 1)
redis.call('SADD', KEYS[3], ARGV[2])
return views
