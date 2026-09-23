"""Atomic peer-cache operations. All writes stay below peer-cache keys."""

BEGIN_SYNC = """
if redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
local generation = redis.call('INCR', KEYS[1])
redis.call('SET', KEYS[2], ARGV[1] .. ':' .. generation, 'EX', 10)
return generation
"""

REPLACE = """
local generation = ARGV[1]
if redis.call('GET', KEYS[4]) ~= generation or
   redis.call('GET', KEYS[5]) ~= ARGV[2] .. ':' .. generation then
  return {0, 0, 0, 1}
end
local now = tonumber(ARGV[3])
local expired = redis.call('ZRANGEBYSCORE', KEYS[8], '-inf', now)
for _, endpoint in ipairs(expired) do
  redis.call('HDEL', KEYS[3], endpoint)
  redis.call('ZREM', KEYS[8], endpoint)
end
redis.call('DEL', KEYS[1], KEYS[2])
local accepted, filtered, suppressed = 0, 0, 0
for i = 4, #ARGV do
  local value = cjson.decode(ARGV[i])
  local endpoint = value.endpoint
  if value.expires_epoch <= now or redis.call('HEXISTS', KEYS[6], endpoint) == 1 then
    filtered = filtered + 1
  else
    local raw = redis.call('HGET', KEYS[3], endpoint)
    local blocked = false
    if raw then
      local failure = cjson.decode(raw)
      blocked = now < failure.blocked_until or
        value.checked_epoch <= math.max(failure.failed_source_checked_at, failure.failed_at + 5)
    end
    if blocked then
      suppressed = suppressed + 1
    else
      redis.call('HSET', KEYS[1], endpoint, ARGV[i])
      redis.call('ZADD', KEYS[2], value.expires_epoch, endpoint)
      accepted = accepted + 1
    end
  end
end
redis.call('HSET', KEYS[7], 'last_sync_at', ARGV[3], 'accepted', accepted)
redis.call('DEL', KEYS[5])
return {accepted, filtered, suppressed, 0}
"""

SELECT = """
local now = tonumber(ARGV[1])
local minimum_score = tonumber(ARGV[2])
local maximum_latency = tonumber(ARGV[3])
local max_age = tonumber(ARGV[4])
local min_successes = tonumber(ARGV[5])
local count = tonumber(ARGV[6])
local principal = ARGV[7]
local mode = ARGV[8]
local excluded = {}
for _, endpoint in ipairs(cjson.decode(ARGV[9])) do excluded[endpoint] = true end
local selected = {}
local members = redis.call('ZRANGEBYSCORE', KEYS[2], '(' .. ARGV[1], '+inf')
for _, endpoint in ipairs(members) do
  if not excluded[endpoint] and redis.call('HEXISTS', KEYS[3], endpoint) == 0 then
    local raw = redis.call('HGET', KEYS[1], endpoint)
    if raw then
      local value = cjson.decode(raw)
      local age = now - value.checked_epoch
      if value.domain == ARGV[10] and value.score >= minimum_score and
         value.latency_ewma_ms <= maximum_latency and
         value.consecutive_successes >= min_successes and
         age >= -5 and age < max_age and value.expires_epoch > now then
        local failure_raw = redis.call('HGET', KEYS[4], endpoint)
        local blocked = false
        if failure_raw then
          local failure = cjson.decode(failure_raw)
          blocked = now < failure.blocked_until or
            value.checked_epoch <= math.max(failure.failed_source_checked_at, failure.failed_at + 5)
        end
        local usable_until = math.min(now + 600, value.expires_epoch,
                                      value.checked_epoch + max_age - 5)
        if not blocked and usable_until > now then
          if mode == 'count' then
            table.insert(selected, endpoint)
          else
            local index = #selected + 1
            local token = ARGV[10 + (index - 1) * 2 + 1]
            local digest = ARGV[10 + (index - 1) * 2 + 2]
            local receipt = {
              digest=digest, principal_fingerprint=principal, domain=value.domain,
              endpoint=endpoint, selection_source='peer', peer_name=value.peer_name,
              source_checked_at=value.checked_epoch, snapshot=value,
              issued_at=now, expires_at=now + 600, failure_applied=false,
            }
            local issued = redis.call('SET', KEYS[5] .. digest, cjson.encode(receipt),
                                      'EX', 600, 'NX')
            if issued then
              table.insert(selected, cjson.encode({record=value, token=token,
                                                   usable_until=usable_until}))
            end
          end
          if #selected >= count then break end
        end
      end
    end
  end
end
return selected
"""

INVALIDATE = """
local raw = redis.call('GET', KEYS[1])
if not raw then return -1 end
local receipt = cjson.decode(raw)
if receipt.failure_applied then return 0 end
receipt.failure_applied = true
local ttl = redis.call('PTTL', KEYS[1])
if ttl <= 0 then return -1 end
redis.call('SET', KEYS[1], cjson.encode(receipt), 'PX', ttl)
local failure = {
  failed_at=tonumber(ARGV[1]),
  failed_source_checked_at=receipt.source_checked_at,
  blocked_until=tonumber(ARGV[1]) + tonumber(ARGV[2]),
}
redis.call('HSET', KEYS[2], receipt.endpoint, cjson.encode(failure))
redis.call('ZADD', KEYS[5], tonumber(ARGV[1]) + tonumber(ARGV[3]), receipt.endpoint)
redis.call('HDEL', KEYS[3], receipt.endpoint)
redis.call('ZREM', KEYS[4], receipt.endpoint)
return 1
"""
