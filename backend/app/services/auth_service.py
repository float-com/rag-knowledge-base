"""认证服务（第 11 期）。

【模块职责说明】
只负责登录这一件事：把"用户名 + 密码"换成"已认证的 User 实体"。

它刻意【不做】令牌签发之外的任何编排：
- 不签发 HTTP 响应（那是路由层的事）；
- 不抛 HTTP 异常（它返回 None，由路由层决定翻译成 401 还是别的）。

【为什么登录只写两个方法】
`authenticate` 负责"验证身份"，`issue_token` 负责"发放凭证"。
拆成两步而不是合成一个 login()，是为了让调用点能看清顺序：
先认证通过，才谈得上签发；也方便将来加"登录审计"时插在两步之间。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import create_access_token, verify_password
from app.db.models import User, UserStatus
from app.db.repositories.user_repo import UserRepository

logger = get_logger(__name__)


class AuthService:
    """登录认证服务。"""

    def __init__(self, session: AsyncSession) -> None:
        """注入请求级异步会话，并初始化用户仓储。"""
        self.session = session
        self.user_repo = UserRepository(session)

    async def authenticate(self, username: str, password: str) -> User | None:
        """校验登录凭据。

        【三种失败一律返回 None，不区分原因】
        - 用户名不存在；
        - 密码不对；
        - 账号已被停用（status 不是 active）。

        【为什么要合并成"同一个失败"，而不是分别给不同提示】
        这是安全领域的标准做法 —— 避免**侧信道泄露账号是否存在**：
        如果"用户名不存在"提示"该用户不存在"、"密码错"提示"密码错误"，
        攻击者就能拿一份用户名字典逐个探测，把"哪些用户名真实存在"筛出来，
        然后再针对这批真实账号集中爆破密码。
        统一返回"用户名或密码错误"，让攻击者无法区分自己错在哪一步。

        【停用账号为什么也返回 None】
        同理：若停用账号提示"账号已停用"，等于也承认了这个用户名的存在。
        这里只在**服务端日志**里记录停用事件（便于管理员排查"为什么员工登不上"），
        对外仍然是一个笼统的失败。

        :param username: 登录用户名
        :param password: 明文密码
        :return: 认证通过的 User 实体（roles 已加载）；失败返回 None
        """
        # get_by_username 是"预加载角色"的查询，
        # 因为登录成功后紧接着就要汇总权限标签（见 permission_service）。
        user = await self.user_repo.get_by_username(username)
        if user is None:
            return None

        if user.status != UserStatus.ACTIVE:
            # 只记服务端日志，不对外暴露"该账号存在但被停用"
            logger.info("login refused (disabled): username=%s", username)
            return None

        if not verify_password(password, user.password_hash):
            return None

        return user

    @staticmethod
    def issue_token(user: User) -> str:
        """为用户签发访问令牌。

        【为什么是 staticmethod 而不是实例方法】
        它不需要数据库、不需要 session，只依赖传入的 user 与全局配置 ——
        标成静态方法能明确表达"这里不发生任何 I/O"，
        调用方也不必先构造 AuthService 实例。

        【sub 放的是 user.id 的字符串形式】
        JWT 的 payload 必须是可 JSON 序列化的，UUID 对象不能直接塞进去。
        decode 时再按字符串取回、由调用方自行转回 UUID。
        """
        return create_access_token(str(user.id))
