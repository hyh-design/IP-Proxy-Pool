"""Atomic Redis scripts for dashboard history."""

WRITE_HISTORY = """
redis.call('HSET', KEYS[2], ARGV[1], ARGV[3])
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[4])
for _, bucket in ipairs(expired) do
  redis.call('HDEL', KEYS[2], bucket)
end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[4])
local overflow = redis.call('ZCARD', KEYS[1]) - tonumber(ARGV[5])
if overflow > 0 then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, overflow - 1)
  for _, bucket in ipairs(oldest) do
    redis.call('HDEL', KEYS[2], bucket)
    redis.call('ZREM', KEYS[1], bucket)
  end
end
return redis.call('ZCARD', KEYS[1])
"""
