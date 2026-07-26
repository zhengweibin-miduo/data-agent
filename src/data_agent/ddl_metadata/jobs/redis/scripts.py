"""DDL 任务多键原子 Redis Lua 协议。"""

from typing import ClassVar


class JobScripts:
    """集中提供无运行时状态的原子 Lua 脚本。"""

    SUBMIT: ClassVar[str] = """
if redis.call('SET', KEYS[3], ARGV[1], 'EX', ARGV[2], 'NX') == false then
  return 0
end
redis.call('HSET', KEYS[1],
  'job_id', ARGV[1], 'source', ARGV[3], 'status', 'pending',
  'revision', '0', 'attempt', '0', 'question_round', '0',
  'created_at', ARGV[4], 'updated_at', ARGV[4],
  'graph_version', ARGV[5], 'ddl', ARGV[6], 'dialect', 'mysql')
redis.call('ZADD', KEYS[2], ARGV[7], ARGV[1] .. ':0')
local submit_time = redis.call('TIME')
redis.call('ZADD', KEYS[4], submit_time[1], ARGV[1])
return 1
"""

    TRANSITION: ClassVar[str] = """
if redis.call('HGET', KEYS[1], 'status') ~= ARGV[1] then return 0 end
if redis.call('HGET', KEYS[1], 'revision') ~= ARGV[3] then return 0 end
redis.call('HSET', KEYS[1], 'status', ARGV[2], 'updated_at', ARGV[4])
local field_count = tonumber(ARGV[8])
for index = 0, field_count - 1 do
  redis.call('HSET', KEYS[1], ARGV[9 + index * 2], ARGV[10 + index * 2])
end
if ARGV[8 + field_count * 2 + 2] == '1' then
  redis.call('HINCRBY', KEYS[1], 'attempt', 1)
end
redis.call('ZREM', KEYS[2], ARGV[5])
if ARGV[2] == 'waiting_input' then
  redis.call('ZADD', KEYS[2], ARGV[6], ARGV[5])
end
local job_id = redis.call('HGET', KEYS[1], 'job_id')
local redis_time = redis.call('TIME')
if ARGV[7] == '1' then
  if redis.call('GET', KEYS[3]) == job_id then
    redis.call('DEL', KEYS[3])
  end
  redis.call('ZADD', KEYS[4], redis_time[1], job_id)
  redis.call('ZREM', KEYS[5], job_id)
  redis.call('HDEL', KEYS[1],
    'ddl', 'answer_json', 'questions_json',
    'expires_at', 'expires_at_epoch')
  redis.call('EXPIRE', KEYS[1], ARGV[8 + field_count * 2 + 1])
else
  redis.call('ZADD', KEYS[5], redis_time[1], job_id)
end
return 1
"""

    # 受理回答后清除 questions_json 与等待截止时间，使 pending 状态下的读取不再
    # 返回上一轮问题；question_set_id 与 answer_hash 必须保留，重复提交的幂等判定
    # （返回码 2）依赖它们，清除会把合法重试误判为 stale_answer。
    #
    # 同理，终态清理也只删除原始 answer_json 与 ddl，保留这两个指纹字段直到任务
    # Hash 的结果保留期到期：受理回答后会立即调度激活，任务可能在 HTTP 响应送达
    # 或客户端重试之前就到达终态；若终态一并删除指纹，合法的相同回答重试就会拿到
    # stale_answer，调用方无法据此判断先前请求是否已被受理。
    ANSWER: ClassVar[str] = """
local status = redis.call('HGET', KEYS[1], 'status')
local revision = redis.call('HGET', KEYS[1], 'revision')
if status ~= 'waiting_input' then
  if redis.call('HGET', KEYS[1], 'answer_hash') == ARGV[4]
     and redis.call('HGET', KEYS[1], 'question_set_id') == ARGV[2]
     and revision == ARGV[5] then return 2 end
  return 0
end
if revision ~= ARGV[1]
   or redis.call('HGET', KEYS[1], 'question_set_id') ~= ARGV[2] then
  return 0
end
if tonumber(redis.call('HGET', KEYS[1], 'expires_at_epoch')) <= tonumber(ARGV[3]) then
  redis.call('HSET', KEYS[1], 'status', 'rejected', 'updated_at', ARGV[6],
    'error_json', ARGV[10])
  redis.call('ZREM', KEYS[2], ARGV[8])
  if redis.call('GET', KEYS[4]) == redis.call('HGET', KEYS[1], 'job_id') then
    redis.call('DEL', KEYS[4])
  end
  redis.call('ZADD', KEYS[5], ARGV[3], redis.call('HGET', KEYS[1], 'job_id'))
  redis.call('ZREM', KEYS[6], redis.call('HGET', KEYS[1], 'job_id'))
  redis.call('HDEL', KEYS[1],
    'ddl', 'answer_json', 'questions_json',
    'expires_at', 'expires_at_epoch')
  redis.call('EXPIRE', KEYS[1], ARGV[11])
  return -1
end
redis.call('HSET', KEYS[1],
  'status', 'pending', 'revision', ARGV[5], 'updated_at', ARGV[6],
  'answer_hash', ARGV[4], 'answer_json', ARGV[7])
redis.call('HDEL', KEYS[1], 'questions_json', 'expires_at', 'expires_at_epoch')
redis.call('ZREM', KEYS[2], ARGV[8])
redis.call('ZADD', KEYS[3], ARGV[3], ARGV[9])
local answer_time = redis.call('TIME')
redis.call('ZADD', KEYS[6], answer_time[1], redis.call('HGET', KEYS[1], 'job_id'))
if redis.call('GET', KEYS[4]) == redis.call('HGET', KEYS[1], 'job_id') then
  redis.call('EXPIRE', KEYS[4], ARGV[12])
end
return 1
"""

    RENEW: ClassVar[str] = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

    RELEASE: ClassVar[str] = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
