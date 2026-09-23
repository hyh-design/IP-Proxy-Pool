"""Atomic ownership case transitions; production time comes from Redis TIME."""

_CLOCK = """
local function now_ms(override)
  local supplied = tonumber(override)
  if supplied and supplied >= 0 then return supplied end
  local clock = redis.call('TIME')
  return tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
end
"""

OPEN_CASE = (
    _CLOCK
    + """
local existing = redis.call('HGET', KEYS[1], 'cycle_json')
if existing then
  if existing ~= ARGV[2] or redis.call('HGET', KEYS[1], 'members_json') ~= ARGV[4] then
    return redis.error_reply('conflicting case ID')
  end
  return 0
end
local now = now_ms(ARGV[6])
local status = 'pending'
local revision = 0
if ARGV[3] ~= '' and redis.call('EXISTS', KEYS[2]) == 1 then
  local received = tonumber(redis.call('HGET', KEYS[2], 'server_time')) or 0
  local entry = tonumber(ARGV[9]) or 0
  if received >= entry and received <= now then
    status = 'internal'
    revision = 1
  end
end
redis.call('HSET', KEYS[1], 'case_id', ARGV[1], 'cycle_json', ARGV[2],
  'cycle_digest', ARGV[3], 'members_json', ARGV[4], 'creator_member', ARGV[5],
  'created_at', now, 'status', status, 'revision', revision)
redis.call('EXPIRE', KEYS[1], ARGV[8])
if ARGV[3] ~= '' then
  redis.call('SADD', KEYS[3], ARGV[1])
  redis.call('EXPIRE', KEYS[3], ARGV[8])
end
if status == 'pending' then
  redis.call('ZADD', KEYS[4], now + 86400000, ARGV[1])
  local members = cjson.decode(ARGV[4])
  for _, member in ipairs(members) do
    redis.call('ZADD', ARGV[7] .. ':jobs:' .. member, now, ARGV[1] .. ':1')
  end
end
return 1
"""
)

CLAIM_JOB = (
    _CLOCK
    + """
local now = now_ms(ARGV[2])
local jobs = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now, 'LIMIT', 0, 100)
for _, value in ipairs(jobs) do
  local case_id, round = string.match(value, '^(.+):([12])$')
  local case_key = ARGV[3] .. ':case:' .. case_id
  local result_key = case_key .. ':round:' .. round .. ':' .. ARGV[1]
  local lease_key = case_key .. ':lease:' .. round .. ':' .. ARGV[1]
  if redis.call('HGET', case_key, 'status') ~= 'pending'
      or redis.call('EXISTS', result_key) == 1 then
    redis.call('ZREM', KEYS[1], value)
  elseif (tonumber(redis.call('HGET', lease_key, 'expires_at')) or 0) <= now then
    redis.call('HSET', lease_key, 'token', ARGV[4], 'expires_at', now + 30000)
    redis.call('EXPIRE', lease_key, 172800)
    return {case_id, round, redis.call('HGET', case_key, 'cycle_json')}
  end
end
return {}
"""
)

SUBMIT_RESULT = (
    _CLOCK
    + """
local now = now_ms(ARGV[7])
local case_key = KEYS[1]
if redis.call('EXISTS', case_key) == 0 then return redis.error_reply('unknown case') end
local members = cjson.decode(redis.call('HGET', case_key, 'members_json'))
local member_ok = false
for _, member in ipairs(members) do
  if member == ARGV[2] then member_ok = true end
end
if not member_ok then return redis.error_reply('member not in case snapshot') end
if ARGV[3] ~= '1' and ARGV[3] ~= '2' then return redis.error_reply('invalid round') end
if ARGV[4] ~= 'found' and ARGV[4] ~= 'absent' and ARGV[4] ~= 'error' then
  return redis.error_reply('invalid check result')
end
local marker = ARGV[6] .. ':result-id:' .. ARGV[5]
local signature = ARGV[1] .. ':' .. ARGV[2] .. ':' .. ARGV[3] .. ':' .. ARGV[4]
local prior = redis.call('GET', marker)
if prior then
  if prior == signature then return 0 end
  return redis.error_reply('conflicting result ID')
end
if redis.call('HGET', case_key, 'status') ~= 'pending' then
  return redis.error_reply('case already resolved')
end
local result_key = case_key .. ':round:' .. ARGV[3] .. ':' .. ARGV[2]
if redis.call('EXISTS', result_key) == 1 then
  return redis.error_reply('conflicting result')
end
local lease_key = case_key .. ':lease:' .. ARGV[3] .. ':' .. ARGV[2]
if redis.call('HGET', lease_key, 'token') ~= ARGV[8]
    or (tonumber(redis.call('HGET', lease_key, 'expires_at')) or 0) <= now then
  return redis.error_reply('stale job lease')
end
local job_id = ARGV[1] .. ':' .. ARGV[3]
local jobs_key = ARGV[6] .. ':jobs:' .. ARGV[2]
redis.call('SET', marker, signature, 'EX', 172800)
redis.call('DEL', lease_key)
if ARGV[4] == 'error' then
  redis.call('HINCRBY', case_key, 'check_errors', 1)
  redis.call('ZADD', jobs_key, now + 30000, job_id)
  return 1
end
redis.call('HSET', result_key, 'result_id', ARGV[5], 'result', ARGV[4])
redis.call('EXPIRE', result_key, 172800)
redis.call('ZREM', jobs_key, job_id)
if ARGV[4] == 'found' then
  redis.call('HSET', case_key, 'status', 'internal')
  redis.call('HINCRBY', case_key, 'revision', 1)
  redis.call('ZREM', KEYS[2], ARGV[1])
  for _, member in ipairs(members) do
    redis.call('ZREM', ARGV[6] .. ':jobs:' .. member, ARGV[1] .. ':1', ARGV[1] .. ':2')
  end
  return 2
end
local complete = true
for _, member in ipairs(members) do
  local key = case_key .. ':round:' .. ARGV[3] .. ':' .. member
  if redis.call('HGET', key, 'result') ~= 'absent' then complete = false end
end
if complete then
  if ARGV[3] == '1' then
    local due = math.max(now + 15000, tonumber(redis.call('HGET', case_key, 'created_at')) + 30000)
    redis.call('HSET', case_key, 'first_round_completed_at', now)
    for _, member in ipairs(members) do
      redis.call('ZADD', ARGV[6] .. ':jobs:' .. member, due, ARGV[1] .. ':2')
    end
  else
    redis.call('HSET', case_key, 'status', 'external')
    redis.call('HINCRBY', case_key, 'revision', 1)
    redis.call('ZREM', KEYS[2], ARGV[1])
  end
end
return 3
"""
)

REAP_EXPIRED = (
    _CLOCK
    + """
local now = now_ms(ARGV[2])
local cases = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now, 'LIMIT', 0, 100)
local count = 0
for _, case_id in ipairs(cases) do
  local case_key = ARGV[1] .. ':case:' .. case_id
  if redis.call('HGET', case_key, 'status') == 'pending' then
    redis.call('HSET', case_key, 'status', 'unconfirmed')
    redis.call('HINCRBY', case_key, 'revision', 1)
    local members = cjson.decode(redis.call('HGET', case_key, 'members_json'))
    for _, member in ipairs(members) do
      redis.call('ZREM', ARGV[1] .. ':jobs:' .. member, case_id .. ':1', case_id .. ':2')
    end
    count = count + 1
  end
  redis.call('ZREM', KEYS[1], case_id)
end
return count
"""
)
