ACQUIRE = """
local info = redis.call('INFO', 'server')
local uptime = string.match(info, 'uptime_in_seconds:(%d+)')
if not uptime or tonumber(uptime) < tonumber(ARGV[2]) then
  return {-1, 0, 0}
end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - 60000)
local count = redis.call('ZCARD', KEYS[1])
if redis.call('EXISTS', KEYS[2]) == 1 then
  return {1, 0, 5 - count}
end
if count < 5 then
  redis.call('ZADD', KEYS[1], now, ARGV[1])
  redis.call('PEXPIRE', KEYS[1], 120000)
  redis.call('SET', KEYS[2], '1', 'PX', 600000)
  return {1, 0, 4 - count}
end
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
local retry = math.max(1, tonumber(oldest[2]) + 60000 - now)
return {0, retry, 0}
"""
