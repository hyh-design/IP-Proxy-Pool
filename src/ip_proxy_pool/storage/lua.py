"""Atomic Redis operations used by the proxy lease repository."""

CLAIM_DUE = """
local members = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
for _, member in ipairs(members) do
  redis.call('ZREM', KEYS[1], member)
  redis.call('ZADD', KEYS[2], ARGV[3], member)
  redis.call('HSET', KEYS[3], member, ARGV[4])
end
return members
"""

CLAIM_PRIORITY_DUE = """
local claimed = {}
local priority = redis.call(
  'ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[3]
)
for _, member in ipairs(priority) do
  if not redis.call('ZSCORE', KEYS[3], member) then
    redis.call('ZREM', KEYS[2], member)
    redis.call('ZADD', KEYS[3], ARGV[4], member)
    redis.call('HSET', KEYS[4], member, ARGV[5])
    table.insert(claimed, member)
  end
end

local remaining = tonumber(ARGV[2]) - #claimed
if remaining > 0 then
  local general = redis.call(
    'ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[1], 'LIMIT', 0, remaining
  )
  for _, member in ipairs(general) do
    if not redis.call('ZSCORE', KEYS[3], member) then
      redis.call('ZREM', KEYS[2], member)
      redis.call('ZADD', KEYS[3], ARGV[4], member)
      redis.call('HSET', KEYS[4], member, ARGV[5])
      table.insert(claimed, member)
    end
  end
end
return claimed
"""

COMPLETE_LEASE = """
if redis.call('HGET', KEYS[5], ARGV[1]) ~= ARGV[2] then
  return 0
end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
redis.call('ZADD', KEYS[2], ARGV[4], ARGV[1])
redis.call('ZADD', KEYS[3], ARGV[5], ARGV[1])
if ARGV[6] == '' then
  redis.call('ZREM', KEYS[6], ARGV[1])
else
  redis.call('ZADD', KEYS[6], ARGV[6], ARGV[1])
end
if ARGV[7] == '' then
  redis.call('ZREM', KEYS[7], ARGV[1])
else
  redis.call('ZADD', KEYS[7], ARGV[7], ARGV[1])
end
redis.call('ZREM', KEYS[4], ARGV[1])
redis.call('HDEL', KEYS[5], ARGV[1])
return 1
"""

RELEASE_LEASE = """
if redis.call('HGET', KEYS[3], ARGV[1]) ~= ARGV[2] then
  return 0
end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[1])
if redis.call('ZSCORE', KEYS[4], ARGV[1]) then
  redis.call('ZADD', KEYS[4], ARGV[3], ARGV[1])
end
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[1])
return 1
"""

RECLAIM_EXPIRED = """
local members = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
for _, member in ipairs(members) do
  redis.call('ZREM', KEYS[1], member)
  redis.call('ZADD', KEYS[2], ARGV[1], member)
  redis.call('HDEL', KEYS[3], member)
end
return members
"""

DELETE_LEASE = """
if redis.call('HGET', KEYS[5], ARGV[1]) ~= ARGV[2] then
  return 0
end
redis.call('HDEL', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
redis.call('ZREM', KEYS[4], ARGV[1])
redis.call('HDEL', KEYS[5], ARGV[1])
redis.call('ZREM', KEYS[6], ARGV[1])
redis.call('ZREM', KEYS[7], ARGV[1])
return 1
"""

REPLACE_LATENCY_INDEX = """
redis.call('DEL', KEYS[2])
if redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('RENAME', KEYS[1], KEYS[2])
end
redis.call('SET', KEYS[3], ARGV[1])
return redis.call('ZCARD', KEYS[2])
"""
