"""Redis 异步客户端单例（第 12 期）。

【为什么单独一个模块，而不是在用到的地方各自 from_url】
`redis.asyncio.from_url(...)` 每次调用都会新建一个【连接池】。若语义缓存、限流、
以及将来别的组件各建一个，一个进程里就会存在多个连接池：

    ① 连接数翻倍（Redis 侧看到的 client 数量与预期不符，排查时容易被误导）
    ② 每个池各自维护空闲连接，资源利用率变差
    ③ 关闭时机不统一，进程退出时容易留下未释放的连接

因此这里用 `lru_cache(maxsize=1)` 把"同一条 URL 只建一个客户端"这件事收敛成一个函数，
全项目只从 `get_redis()` 取客户端。

【为什么只连 db 0】
本模块是【应用侧】客户端（语义缓存 + 限流）。Celery 的 broker（db 1）与
result backend（db 2）由 Celery 自己按配置直连，【不共用这个客户端】——
三者的生命周期与清理策略完全不同，混用会互相干扰（详见 config.py 的说明）。
"""

from functools import lru_cache

import redis.asyncio as aioredis
from redis.asyncio import Redis

from app.core.config import settings


@lru_cache(maxsize=1)
def get_redis() -> Redis:
    """获取应用侧 Redis 异步客户端（进程内单例）。

    【两个关键参数】
    - `decode_responses=True`：读写自动以 str 处理。
      默认（False）返回 bytes，意味着每处取值都要 `v.decode()`、每处写值都要 `v.encode()`，
      漏一处就是"看起来存进去了但取出来是 b'...'"这类难查的小 bug。
      本项目缓存的是文本（问题、答案、标签），用 str 更贴合，且省掉全部手工编解码。
      ⚠️ 代价：存二进制（如 float32 向量字节）时必须显式标注，
      本项目的向量是交给 RedisVL 处理的，不走这个客户端，因此不受影响。
    - `from_url(settings.redis_url)`：URL 末尾的 `/0` 就是逻辑库编号。
      换成 `/1` 会连到 Celery 的 broker 库 —— 那是最危险的一类笔误，
      因为它不会报错，只会让两个组件互相看到对方的键。

    :return: redis.asyncio.Redis 客户端（进程内唯一）
    """
    return aioredis.from_url(settings.redis_url, decode_responses=True)
