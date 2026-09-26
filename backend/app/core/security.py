"""认证安全工具函数（第 11 期）。

【模块职责说明】
本模块是认证体系的【密码学原语层】，只做三件事，不含任何数据库与业务逻辑：

1. 密码哈希与校验（bcrypt）：
   把用户明文密码转成不可逆的哈希串落库，并在登录时比对。

2. JWT 签发（create_access_token）：
   把"用户身份"编码成一个带签名、带有效期的自包含令牌。

3. JWT 解码验签（decode_access_token）：
   从令牌里取回 sub（用户 ID），失败一律抛 UnauthorizedError(401)。

【为什么这三件事要独立成模块】
它们是纯函数：输入相同、输出相同，不依赖 session / 不依赖请求上下文，
因此可以被 Service 层、依赖注入层、甚至将来的定时任务复用；也便于单独写单元测试。

【两个必须记住的坑（均已在真机实测确认）】
1. bcrypt 5.x 起**移除了对超长密码的静默截断**：
   明文超过 72 字节时，hashpw 与 checkpw **都会**直接抛 ValueError（4.x 之前是悄悄截断）。
   因此 verify_password 必须把 ValueError 一起兜住 —— 否则一个超长密码会让接口 500。
   注意本模块【故意不做长度前置校验】，保持与教程一致；长度校验属于请求参数校验层的事，
   已记入归档遗留项，第 6 步写 /auth 路由时需要决定在哪拦。

2. ValueError 还是"非法哈希串"的信号：
   库里若存了非 bcrypt 格式的脏数据，checkpw 同样抛 ValueError。
   对这两种情况都按"密码不对"处理即可，绝不能把异常抛给调用方 ——
   否则等于用一个错误信息向前端泄露"这个账号的哈希是坏的"。
"""

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.core.config import settings
from app.core.exceptions import UnauthorizedError


def hash_password(plain: str) -> str:
    """把明文密码转成 bcrypt 哈希串。

    :param plain: 用户输入的明文密码
    :return: 60 字符的 bcrypt 哈希串，形如 `$2b$12$...`
             （其中 `$2b$` 是算法版本标识，`12` 是 cost 工作因子）

    【为什么用 bcrypt 而不是 MD5 / SHA256】：
    通用哈希函数设计目标是"算得快"，GPU 每秒能试上亿次；
    bcrypt 的设计目标是"故意算得慢" —— cost=12 时单次哈希本机实测约 267ms
    （校验一侧约 349ms），对登录接口完全无感（人一次只登一次），但对暴力枚举是致命的。
    cost 还可以随算力上涨而上调。

    【为什么用 gensalt() 而不是固定 salt】：
    gensalt() 每次都生成随机盐并【写进哈希串本身】，
    因此同一个密码两次调用会得到两个不同的哈希串 —— 这让彩虹表彻底失效。
    大白话：
    - 固定盐是一锅出，张三李四密码一样密文就一样，破一个全玩完。
    - 随机盐是一人一配方，密码一样密文也完全不同，黑客只能挨个死磕。
    - 盐写在串里不怕被看，它不是密钥，只是让每人的密文独一无二，顺便留着下次登录比对。
    """
    # 链式调用执行流程（由内向外）：
    # 1. plain.encode("utf-8")  -> str 转 bytes（bcrypt 只接受二进制输入）
    # 2. bcrypt.gensalt()       -> 动态生成含 cost 因子（默认 12）的随机盐（bytes）
    # 3. bcrypt.hashpw(...)     -> 执行加盐慢哈希运算，输出 60 字节的哈希结果（bytes）
    # 4. .decode("utf-8")       -> 将最终哈希 bytes 转回 str，便于存入数据库字段
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文密码与库里的哈希串是否匹配。

    :param plain: 用户本次输入的明文密码
    :param hashed: 数据库 users.password_hash 里存的哈希串
    :return: True 匹配；False 不匹配

    【为什么要把 ValueError 也当成 False】：
    两类输入会让 bcrypt 抛 ValueError，而它们【都不该是 500】：
    - 明文超过 72 字节（bcrypt 5.x 不再静默截断，直接抛错）；
    - 库里的 hashed 不是合法 bcrypt 串（历史脏数据 / 手工插入的假数据）。
    这两种情况在"登录是否成功"这个语义上，都等价于"密码不对"。
    """
    try:
        # 语法拆解与执行流程：
        # 1. plain.encode("utf-8")  -> 用户刚输入的明文密码（str 转 bytes）
        # 2. hashed.encode("utf-8") -> 数据库查出来的历史哈希串（str 转 bytes）
        # 3. bcrypt.checkpw(pwd_bytes, hash_bytes) -> 核心校验函数：
        #    - 它会自动从 hashed 提取出当时保存的 salt 和 cost；
        #      （已实测：对 rounds=12 的串校验约 208ms、rounds=10 的串约 50ms，
        #        相差约 4 倍，证明确实是读了串里记录的 cost 而不是用固定值）
        #    - 用同样的配方把 plain 重新算一次哈希，再与串尾的摘要比对；
        #    - 比对一致返回 True，否则返回 False。
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        # 明文的 72 字节上限、以及非法 hash 串，都会走到这里，
        # 统一按"密码不对"处理即可，绝不向上抛
        return False


def create_access_token(subject: str) -> str:
    """签发访问令牌（JWT，JSON Web Token）。

    【核心流程体系】：
    1. 配置安全防御：运行时校验密钥合法性，阻断空密钥签发导致的伪造灾难；
    2. 计算标准时间载荷：基于绝对零时区（UTC）计算秒级签发时刻与过期时刻；
    3. 组装三段式凭证并签名：利用 HMAC 对称算法（如 HS256）与密钥执行签名并完成 Base64 编码。

    【三个标准声明（RFC 7519 Registered Claims）的含义与架构考量】：
    - `sub`（subject）："这个令牌代表谁"。本项目放 user_id 的字符串形式；
    - `iat`（issued at）：签发时刻。用于审计留痕与排障，**不参与有效性阻断**；
    - `exp`（expiration）：过期时刻。**它是 JWT 唯一的自动失效机制**。

    【为什么必须强制包含 exp】：
    JWT 遵循【无状态（Stateless）】设计原则 —— 服务端内存与数据库中完全不保存“已签发了哪些令牌”。
    这意味着在不引入 Redis 黑名单等重型机制的前提下，服务端**在物理上无法主动吊销**单个已被分发的 Token。
    因此，过期时间（exp）是阻断窃听泄露与防凭证无限期滥用的唯一绝对防线。

    :param subject: 令牌所代表的主体标识（通常为 user.id 的 str 格式）
    :return: 编码与签名后的三段式字符串（Header.Payload.Signature）
    :raises UnauthorizedError: 当服务端环境变量未配置 JWT_SECRET 时阻断抛出
    """
    # -------------------------------------------------------------------------
    # 步骤 1：配置安全防线（零容忍阻断）
    # -------------------------------------------------------------------------
    # 唯一的配置防线（当前实际生效的第一道，也是唯一一道阻断）：
    #   config 层提供了 warn_if_jwt_unconfigured() 做启动期告警，但它【尚未接入 create_app()】，
    #   因此不能假设"启动时已经告警过了"。详见归档遗留项。
    # 若允许空密钥继续执行，黑客只需传空串即可自由伪造任意 user_id 的 Token，鉴权体系将彻底瓦解。
    #
    # ⚠️【已知语义瑕疵，待收尾决定】这里抛的是 UnauthorizedError(401)，
    #   但"服务端自己没配密钥"本质是服务端配置故障（本该 503，与 ConfigurationError 一致）。
    #   现状的副作用：前端收到 401 会清登录态并跳 /login，用户重登仍是 401 → 陷入重登死循环，
    #   而真正的病因（缺配置）一个字都不会提示出来。
    if not settings.jwt_configured:
        raise UnauthorizedError("服务端未配置 JWT secret，无法签发 token")

    # -------------------------------------------------------------------------
    # 步骤 2：获取基准时间并装配 Claims 载荷（Payload）
    # -------------------------------------------------------------------------
    # 【语法点：datetime.now(UTC)】
    # - UTC 是 Python 3.11+ 标准库中提供的便捷常量，等价于 datetime.timezone.utc；
    # - 显式锚定世界协调时（零时区），避免服务器本地系统时区（如东八区或容器默认时区）干扰绝对时间戳。
    now = datetime.now(UTC)

    payload = {
        # sub 声明：标明当前 Token 归属于哪位用户
        "sub": subject,

        # 【语法点：now.timestamp() 与 int(...)】
        # - now.timestamp() 返回的是 1970-01-01 以来的 Epoch Unix 秒级时间戳（带小数位的 float）；
        # - RFC 7519 规范明确要求 iat / exp 必须是整数（NumericDate），故用 int() 抹平小数，记录签发那一秒。
        "iat": int(now.timestamp()),

        # 【语法点：now + timedelta(...)】
        # - timedelta(minutes=settings.jwt_expire_minutes) 构造一段“N 分钟”的时间增量；
        # - 两者相加即为未来的绝对时间点；再转为秒级时间戳，作为 Token 自动失效的生死线。
        "exp": int((now + timedelta(minutes=settings.jwt_expire_minutes)).timestamp()),
    }

    # -------------------------------------------------------------------------
    # 步骤 3：哈希签名与三段式序列化（Header.Payload.Signature）
    # -------------------------------------------------------------------------
    # 【底层做了什么】：
    # 1. 自动构建 Header：{"alg": "HS256", "typ": "JWT"} 并转为 Base64URL；
    # 2. 将 payload 字典转为 JSON 字符串并进行 Base64URL 编码；
    # 3. 采用 settings.jwt_algorithm（如 HS256），拿 settings.jwt_secret 对 "Header.Payload"
    #    进行 HMAC 运算生成一段防篡改的签名摘要（Signature）；
    # 4. 用英文句点 "." 把这三段拼接，产出最终安全交付给前端的 Bearer Token 字符串。
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> str:
    """校验访问令牌（JWT）并提取其中的主体标识（用户 ID）。

    【核心流程体系】：
    1. 配置安全防御：动态检测系统是否完成 JWT 运行配置，防止空密钥验签产生安全绕过；
    2. 签名与时效核验：借助 PyJWT 原生解密器校验签名完整性、算法白名单与 exp 过期时效；
    3. 精准异常转译：区分“会话过期”与“凭证伪造/损坏”，结合 `from exc` 保留排障异常链；
    4. 载荷防御性清洗：强制校验 sub 声明的类型（非空字符串），阻断空值渗透到下游数据库层。

    【为什么要区分捕获两个异常，而不是单一捕获 InvalidTokenError】：
    - ExpiredSignatureError 是 InvalidTokenError 的子类：
      * 过期（Expired）：属于常规生命周期终结，提示用户重新登录即可；
      * 无效（Invalid）：涵盖签名被篡改、密钥更换、格式截断或算法混淆攻击，
        给出与"过期"**区分开**的提示（本项目暂无更细的风控动作）。
    - 拆分捕获的目的很单纯：让前端能按文案区分"该重登了"与"这个凭证有问题"。
      【注意】即使只写 `except InvalidTokenError` 也能跑通（子类会被一起捕获），
      分开写不是为了技术上的必须，而是为了提示准确。

    :param token: 客户端在 HTTP 头部 `Authorization: Bearer <token>` 中携带的原始 JWT 字符串
    :return: 经过验签与类型核验的有效用户 ID 字符串（sub 声明）
    :raises UnauthorizedError: 401 状态码异常（密钥未配、令牌过期、非法伪造、或缺失有效 sub）
    """
    # -------------------------------------------------------------------------
    # 步骤 1：配置安全防线（阻断未就绪状态）
    # -------------------------------------------------------------------------
    # 【架构一致性规范】：
    # 统一使用 Settings 的语义化属性 jwt_configured 进行健康检查，与 create_access_token 保持一致；
    # 将来若密钥来源改成证书文件或密钥管理服务，只需改这一个属性，本函数不用动。
    if not settings.jwt_configured:
        raise UnauthorizedError("服务端未完成 JWT 必要配置，无法校验 token")

    # -------------------------------------------------------------------------
    # 步骤 2：解密验签与时效检查（PyJWT 底层自动接管）
    # -------------------------------------------------------------------------
    try:
        # jwt.decode 内部会自动核对三项致命安全指标：
        # 1. 签名对比：拿 settings.jwt_secret 重新计算哈希并与 Token 尾部对比，防篡改；
        # 2. 时效核验：读取 payload["exp"]，自动对比当前 UTC 时间，超时直接抛出 ExpiredSignatureError；
        # 3. 算法限定：algorithms 传白名单列表（如 ["HS256"]），防范经典的“None 算法”或“算法降级”绕过攻击。
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError as exc:
        # 【语法点：raise ... from exc】
        # 将原始异常挂载到 UnauthorizedError.__cause__ 上，既给前端纯净文案，又为服务端保留完整堆栈
        raise UnauthorizedError("登录已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        # 捕获所有其他非法情况：签名不匹配 / 结构被截断 / 篡改 / 算法不符等
        raise UnauthorizedError("无效的访问凭证") from exc

    # -------------------------------------------------------------------------
    # 步骤 3：载荷业务字段防御性核验（Payload Sanitization）
    # -------------------------------------------------------------------------
    # 安全边界防护：jwt.decode 仅能证明“该令牌是由本服务端合法签发且未被修改”，
    # 但无法保证 payload 中的业务字段齐全有效。
    subject = payload.get("sub")

    # 双重严密守卫：
    # 1. isinstance(subject, str)：阻断黑客故意伪造的布尔型、数字型或列表字典等奇异类型；
    # 2. not subject：阻断空字符串（""）或 None 值；
    # 严防非法空值穿透本函数，导致下游 ORM 层拿着 None 或空串直接发起数据库查询。
    if not isinstance(subject, str) or not subject:
        raise UnauthorizedError("无效的访问凭证")

    return subject
