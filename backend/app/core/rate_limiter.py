"""滑动窗口限流（第 12 期）。

【算法：Redis Sorted Set + pipeline 原子化】
每个用户一份"请求时间戳集合"存在一个 Sorted Set 里（score = 请求时刻），
每次请求做四件事（一个 pipeline 内原子完成）：

    ① zremrangebyscore  清掉窗口外的旧时间戳
    ② zcard             数窗口内当前有多少次请求
    ③ zadd              把本次请求的时间戳加进去
    ④ expire            给 key 续窗口时长的 TTL

【为什么不引入 slowapi / fastapi-limiter 等装饰器】
滑动窗口算法本身就是本期要展示的核心知识。基于 redis-py 自己写三十行、
逻辑透明可读，比一个黑盒装饰器更适合教学 —— 而且出问题时能直接看懂每一步。
"""

import time
from functools import lru_cache
from uuid import uuid4

from app.core.config import settings
from app.core.exceptions import RateLimitError
from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger(__name__)

# 限流窗口固定 60 秒，配合 RATE_LIMIT_PER_MINUTE 形成「每分钟 N 次」语义。
# 【为什么不做成可配置】窗口长度与"每分钟 N 次"这个表述是绑定的：
#   若窗口可配，那 RATE_LIMIT_PER_MINUTE 这个名字就名不副实了
#   （30 秒窗口下它实际是"每半分钟 N 次"）。要调整频率就调 N，不要调窗口。
_WINDOW_SECONDS = 60


class RateLimiter:
    """按 identity（通常是 user:<uuid>）维度的滑动窗口限流器。

    【为什么按 identity 而不是按 IP】
    本项目所有业务接口都要求登录（第 11 期的 CurrentUser），用户身份是可靠的；
    而 IP 在 NAT / 代理 / 移动网络下会被大量用户共享，按 IP 限流容易误伤。
    匿名入口（如将来的 MCP / API Key）没有 user_id，那类限流留给后续章节。
    """

    def __init__(self) -> None:
        # 复用第 2 章建的【应用侧】客户端（连 db0，decode_responses=True）。
        # 注意不要与 Celery 的 broker(db1) / result(db2) 混用 —— 那是另外两个库。
        self._redis = get_redis()

    async def check(self, identity: str) -> None:
        """检查并记录一次请求；越限时抛 RateLimitError，未越限则静默返回。

        :param identity: 限流维度标识，形如 `user:<uuid>`
        :raises RateLimitError: 窗口内请求次数已达上限（HTTP 429）

        【为什么四条命令必须放进一个 pipeline（transaction=True）】
        它们是"读—改—写"的组合：先数、再写、还要设过期。
        若逐条单独发送，两个并发请求可能同时读到 count=59、然后各自都判定"没超限"，
        结果双双放行 —— 限流在最需要它的时候（突发并发）恰好失效。
        pipeline(transaction=True) 底层走 MULTI / EXEC，
        把四条命令打包成一次网络往返、在服务端原子执行，中间不会被其它客户端插队。
        """
        now = time.time()
        key = f"rag:rate_limit:{identity}"

        async with self._redis.pipeline(transaction=True) as pipe:
            # ① 清掉"60 秒之前"的成员：留下的就是当前滑动窗口内的请求
            await pipe.zremrangebyscore(key, 0, now - _WINDOW_SECONDS)
            # ② 数窗口内当前请求数。
            #    ★ 它排在 zadd 之前，所以拿到的是【本次请求加入之前】的数量 ——
            #      下面的判定条件因此写成 count >= limit（而不是 > limit）
            await pipe.zcard(key)
            # ③ 把本次请求记进去。
            #    【为什么 member 用 uuid 而不是时间戳字符串】
            #    Sorted Set 的 member 必须唯一：若用 str(now) 当 member，
            #    同一微秒内的两次请求会被当成"同一个成员"而只保留一条 →
            #    计数偏小 → 限流放水。uuid 保证每次插入都是新成员。
            #    （score 仍然用 now，排序与范围清理靠它。）
            await pipe.zadd(key, {str(uuid4()): now})
            # ④ 给 key 续窗口时长的 TTL。
            #    【为什么每次都要续】限流 key 是"有生命周期的临时数据"：
            #      活跃用户每次请求都续上 60 秒，保证窗口有效；
            #      而冷用户停止请求 60 秒后 key 自动过期消失 ——
            #      否则每个来访过的用户都会在 Redis 里留下一个永不过期的 key。
            await pipe.expire(key, _WINDOW_SECONDS)
            # 返回值按命令顺序对应：(zremrangebyscore, zcard, zadd, expire)
            _, count, _, _ = await pipe.execute()

        limit = settings.rate_limit_per_minute
        # ② 数的量是"本次之前"的，所以 count >= limit 表示本次会越界
        if int(count) >= limit:
            logger.warning(
                "rate limit exceeded: identity=%s count=%s limit=%s",
                identity,
                count,
                limit,
            )
            raise RateLimitError(
                f"请求过于频繁，每分钟最多 {limit} 次，请稍后再试"
            )

        # ⚠️ 一个要知道的行为特性（不是 bug，是滑动窗口的自然结果）：
        #    第 ③ 步的 zadd 在判定【之前】就已经执行，所以【被拒绝的请求也被记进了窗口】。
        #    含义是：一旦触发限流，持续重试会让窗口内始终保持超限状态，
        #    必须等到最早的记录滑出 60 秒窗口才会恢复。
        #    对"恶意高频请求"而言这正是想要的效果（越刷越被挡）；
        #    但对"用户手抖连点"而言，他停手后仍会最多再等一个窗口。
        #    若要改成"拒绝时不计数"，就得把 zadd 挪到判定之后、并拆成两次往返
        #    （牺牲原子性）—— 本项目按教程保留当前写法。


@lru_cache(maxsize=1)
def get_rate_limiter() -> RateLimiter:
    """限流器单例。

    与 `get_redis()` / `get_semantic_cache()` 同一考虑：
    RateLimiter 内部持有 Redis 客户端，每个请求各建一个没有意义。
    """
    return RateLimiter()
