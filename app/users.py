"""用户账号、登录会话与角色权限。

角色职责：
- ``admin``  管理员：全部权限，包含用户管理与模型配置；
- ``staff``  员工：可上传/删除知识库文件、构建索引、创建/删除知识库；
- ``viewer`` 普通用户：只读，可问答、检索和查看资料。

会话令牌使用 ``secrets`` 生成，数据库只保存 SHA-256 摘要；明文只在登录响应中返回一次。
密码使用带随机盐的 scrypt 摘要，不保存也无法还原明文。
"""

import hashlib
import hmac
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
from fastapi import Request
from pydantic import BaseModel, ConfigDict

from app.clock import utc_now
from app.errors import ChatError

ROLES = ("admin", "staff", "viewer")
ROLE_LABELS = {"admin": "管理员", "staff": "员工", "viewer": "普通用户"}
INGEST_ROLES = ("admin", "staff")
ADMIN_ROLES = ("admin",)
LOGIN_FAILURE_LIMIT = 10
LOGIN_FAILURE_WINDOW = 300.0

logger = logging.getLogger(__name__)
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
# 用户名不存在时也执行一次等价校验，避免通过响应时间枚举账号。
DUMMY_PASSWORD_HASH = (
    "scrypt$16384$8$1$8f5454ef719c446e909b3d4223175f17$"
    "43a6e89561223ab4614247d3a6a1361ddbe6e41a2bb21c366f72a6619bbf71ed"
)


class User(BaseModel):
    """可安全返回给客户端的账号信息，不包含密码摘要。"""

    model_config = ConfigDict(extra="forbid")
    user_id: str
    username: str
    display_name: str
    role: str
    is_active: bool
    created_at: str
    updated_at: str
    last_login_at: str | None = None
    anonymous: bool = False
    service: bool = False


class Permissions(BaseModel):
    ingest_files: bool
    manage_knowledge_bases: bool
    manage_models: bool
    manage_users: bool


def permissions_for(role: str) -> Permissions:
    """前后端共用的权限口径；新增角色时只需在这里扩展。"""
    return Permissions(
        ingest_files=role in INGEST_ROLES,
        manage_knowledge_bases=role in INGEST_ROLES,
        manage_models=role in ADMIN_ROLES,
        manage_users=role in ADMIN_ROLES,
    )


LOCAL_USER = User(
    user_id="local",
    username="local",
    display_name="本地管理员",
    role="admin",
    is_active=True,
    created_at="",
    updated_at="",
    anonymous=True,
)
SERVICE_USER = User(
    user_id="service",
    username="service",
    display_name="服务令牌",
    role="admin",
    is_active=True,
    created_at="",
    updated_at="",
    service=True,
)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (AttributeError, TypeError, ValueError):
        return False


def validate_username(username: str) -> str:
    value = (username or "").strip()
    if not USERNAME_RE.fullmatch(value):
        raise ChatError(
            "invalid_username",
            "用户名需为 3–32 位字母、数字、下划线、点或连字符，并以字母或数字开头。",
            422,
        )
    return value


def validate_role(role: str) -> str:
    value = (role or "").strip()
    if value not in ROLES:
        raise ChatError("invalid_role", "无效的用户角色。", 422)
    return value


def validate_display_name(display_name: str, username: str) -> str:
    value = (display_name or "").strip() or username
    if not 1 <= len(value) <= 64:
        raise ChatError("invalid_display_name", "显示名称需为 1–64 个字符。", 422)
    if any(ord(char) < 32 for char in value):
        raise ChatError("invalid_display_name", "显示名称不能包含控制字符。", 422)
    return value


def to_user(row) -> User:
    return User(
        user_id=row["user_id"],
        username=row["username"],
        display_name=row["display_name"],
        role=row["role"],
        is_active=row["is_active"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_login_at=row["last_login_at"],
    )


class AuthService:
    """账号与会话服务；数据库连接在 lifespan 中绑定。"""

    CACHE_SECONDS = 5

    def __init__(self, settings):
        self.settings = settings
        self.service_token = settings.api_token.get_secret_value().strip()
        self.database = None
        self._has_users: bool | None = None
        self._checked = 0.0
        self._failures: dict[str, tuple[int, float]] = {}

    def bind(self, database):
        self.database = database

    @property
    def password_min_length(self) -> int:
        return self.settings.password_min_length

    async def has_users(self) -> bool:
        if self.database is None:
            return False
        now = time.monotonic()
        if self._has_users is not None and now - self._checked < self.CACHE_SECONDS:
            return self._has_users
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT EXISTS(SELECT 1 FROM users) AS present"
            )
            present = bool((await cursor.fetchone())["present"])
        self._has_users, self._checked = present, now
        return present

    async def required(self) -> bool:
        """配置了服务令牌或已存在任何账号时，API 必须鉴权。"""
        return bool(self.service_token) or await self.has_users()

    def forget_users(self):
        self._has_users = None
        self._checked = 0.0

    def validate_password(self, password: str):
        if not isinstance(password, str):
            raise ChatError("weak_password", "密码必须是字符串。", 422)
        if len(password) < self.password_min_length:
            raise ChatError(
                "weak_password", f"密码至少需要 {self.password_min_length} 个字符。", 422
            )
        if len(password) > 128:
            raise ChatError("weak_password", "密码不能超过 128 个字符。", 422)

    # ------------------------------------------------------------------ 账号

    async def _row(self, user_id: str):
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT * FROM users WHERE user_id=%s", (user_id,)
            )
            return await cursor.fetchone()

    async def _row_by_username(self, username: str):
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT * FROM users WHERE lower(username)=lower(%s)", (username.strip(),)
            )
            return await cursor.fetchone()

    async def get(self, user_id: str) -> User:
        row = await self._row(user_id)
        if row is None:
            raise ChatError("user_not_found", "用户不存在。", 404)
        return to_user(row)

    async def find(self, username: str) -> User | None:
        row = await self._row_by_username(username)
        return to_user(row) if row is not None else None

    async def list_users(self) -> list[User]:
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT * FROM users ORDER BY created_at, username"
            )
            rows = await cursor.fetchall()
        return [to_user(row) for row in rows]

    async def create_user(
        self,
        *,
        username: str,
        password: str,
        role: str = "viewer",
        display_name: str = "",
    ) -> User:
        username = validate_username(username)
        role = validate_role(role)
        display_name = validate_display_name(display_name, username)
        self.validate_password(password)
        user_id, timestamp = str(uuid4()), utc_now()
        try:
            async with self.database.transaction():
                await self.database.connection.execute(
                    "INSERT INTO users(user_id,username,display_name,role,password_hash,"
                    "is_active,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,TRUE,%s,%s)",
                    (
                        user_id,
                        username,
                        display_name,
                        role,
                        hash_password(password),
                        timestamp,
                        timestamp,
                    ),
                )
        except psycopg.errors.UniqueViolation:
            raise ChatError("user_exists", "用户名已被使用。", 409) from None
        self.forget_users()
        return await self.get(user_id)

    async def update_user(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
        role: str | None = None,
        is_active: bool | None = None,
        password: str | None = None,
    ) -> User:
        if role is not None:
            role = validate_role(role)
        if password is not None:
            self.validate_password(password)
        async with self.database.transaction():
            connection = self.database.connection
            await self._lock_admin_changes()
            cursor = await connection.execute(
                "SELECT * FROM users WHERE user_id=%s FOR UPDATE", (user_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                raise ChatError("user_not_found", "用户不存在。", 404)
            next_role = role if role is not None else row["role"]
            next_active = is_active if is_active is not None else row["is_active"]
            if row["role"] == "admin" and (next_role != "admin" or not next_active):
                await self._require_other_admin(user_id)
            next_name = (
                validate_display_name(display_name, row["username"])
                if display_name is not None
                else row["display_name"]
            )
            await connection.execute(
                "UPDATE users SET display_name=%s,role=%s,is_active=%s,updated_at=%s "
                "WHERE user_id=%s",
                (next_name, next_role, next_active, utc_now(), user_id),
            )
            if password is not None or not next_active:
                # 改密或停用后立即失效全部会话，避免旧令牌继续访问。
                await connection.execute(
                    "DELETE FROM user_sessions WHERE user_id=%s", (user_id,)
                )
            if password is not None:
                await connection.execute(
                    "UPDATE users SET password_hash=%s WHERE user_id=%s",
                    (hash_password(password), user_id),
                )
        return await self.get(user_id)

    async def delete_user(self, user_id: str, *, actor_id: str) -> None:
        if user_id == actor_id:
            raise ChatError("cannot_delete_self", "不能删除当前登录账号。", 409)
        async with self.database.transaction():
            connection = self.database.connection
            await self._lock_admin_changes()
            cursor = await connection.execute(
                "SELECT * FROM users WHERE user_id=%s FOR UPDATE", (user_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                raise ChatError("user_not_found", "用户不存在。", 404)
            if row["role"] == "admin":
                await self._require_other_admin(user_id)
            await connection.execute("DELETE FROM users WHERE user_id=%s", (user_id,))
        self.forget_users()

    async def _lock_admin_changes(self):
        # 在读取目标行之前串行化角色/停用/删除，避免两次修改都看到另一个管理员。
        await self.database.connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "current_database() || ':' || current_schema() || ':users:admin_changes',0))"
        )

    async def _require_other_admin(self, exclude_user_id: str):
        cursor = await self.database.connection.execute(
            "SELECT count(*) AS remaining FROM users "
            "WHERE role='admin' AND is_active AND user_id<>%s",
            (exclude_user_id,),
        )
        if not (await cursor.fetchone())["remaining"]:
            raise ChatError("last_admin", "必须至少保留一个启用状态的管理员。", 409)

    # ------------------------------------------------------------------ 会话

    async def _issue_session(self, user_id: str, *, last_login: bool = False):
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        timestamp = utc_now()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=self.settings.session_ttl_hours)
        ).isoformat()
        async with self.database.transaction():
            connection = self.database.connection
            await connection.execute(
                "DELETE FROM user_sessions WHERE expires_at <= %s", (timestamp,)
            )
            await connection.execute(
                "INSERT INTO user_sessions(token_hash,user_id,created_at,expires_at,last_seen_at) "
                "VALUES (%s,%s,%s,%s,%s)",
                (token_hash, user_id, timestamp, expires_at, timestamp),
            )
            if last_login:
                await connection.execute(
                    "UPDATE users SET last_login_at=%s WHERE user_id=%s",
                    (timestamp, user_id),
                )
        return token, expires_at

    async def authenticate(self, username: str, password: str):
        key = (username or "").strip().lower()
        self._throttle(key)
        row = await self._row_by_username(username or "")
        if row is None:
            verify_password(password, DUMMY_PASSWORD_HASH)
            self._record_failure(key)
            raise ChatError("invalid_credentials", "用户名或密码不正确。", 401)
        if not verify_password(password, row["password_hash"]):
            self._record_failure(key)
            raise ChatError("invalid_credentials", "用户名或密码不正确。", 401)
        if not row["is_active"]:
            raise ChatError("user_disabled", "账号已停用，请联系管理员。", 403)
        self._failures.pop(key, None)
        token, expires_at = await self._issue_session(row["user_id"], last_login=True)
        return token, expires_at, to_user(row)

    def _throttle(self, key: str):
        """同一用户名连续失败过多时暂停校验，降低在线猜测风险。"""
        count, first = self._failures.get(key, (0, 0.0))
        if not count:
            return
        now = time.monotonic()
        if now - first > LOGIN_FAILURE_WINDOW:
            self._failures.pop(key, None)
            return
        if count >= LOGIN_FAILURE_LIMIT:
            raise ChatError(
                "too_many_attempts", "登录失败次数过多，请稍后再试。", 429, True
            )

    def _record_failure(self, key: str):
        now = time.monotonic()
        count, first = self._failures.get(key, (0, now))
        self._failures[key] = (count + 1, first if count else now)
        if len(self._failures) > 1000:
            self._failures = {
                item: value
                for item, value in self._failures.items()
                if now - value[1] <= LOGIN_FAILURE_WINDOW
            }

    async def setup(self, *, username: str, password: str, display_name: str = ""):
        """创建首个管理员；只有在系统还没有任何账号时可用。"""
        username = validate_username(username)
        display_name = validate_display_name(display_name, username)
        self.validate_password(password)
        user_id, timestamp = str(uuid4()), utc_now()
        async with self.database.transaction():
            connection = self.database.connection
            await connection.execute(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(current_database() || ':users:setup',0))"
            )
            cursor = await connection.execute("SELECT count(*) AS total FROM users")
            if (await cursor.fetchone())["total"]:
                raise ChatError("setup_completed", "管理员账号已存在，请直接登录。", 409)
            await connection.execute(
                "INSERT INTO users(user_id,username,display_name,role,password_hash,"
                "is_active,created_at,updated_at) VALUES (%s,%s,%s,'admin',%s,TRUE,%s,%s)",
                (user_id, username, display_name, hash_password(password), timestamp, timestamp),
            )
        self.forget_users()
        token, expires_at = await self._issue_session(user_id, last_login=True)
        return token, expires_at, await self.get(user_id)

    async def bootstrap(self):
        """按环境配置创建首个管理员；已有账号或未配置密码时跳过。"""
        password = self.settings.admin_password.get_secret_value()
        if not password or await self.has_users():
            return None
        try:
            user = await self.create_user(
                username=self.settings.admin_username,
                password=password,
                role="admin",
                display_name=self.settings.admin_display_name,
            )
        except ChatError as exc:
            # 配置不合法时保留页面初始化流程，不在启动阶段中止服务。
            logger.warning("bootstrap_admin_skipped code=%s", exc.code)
            return None
        logger.info("bootstrap_admin_created username=%s", user.username)
        return user

    async def register(self, *, username: str, password: str, display_name: str = ""):
        """自助注册固定创建普通用户；不能借此获得员工或管理员权限。"""
        if not self.settings.registration_enabled:
            raise ChatError("registration_disabled", "注册功能已关闭，请联系管理员开通账号。", 403)
        if not await self.has_users():
            raise ChatError("setup_required", "请先创建管理员账号，再开放注册。", 409)
        user = await self.create_user(
            username=username,
            password=password,
            role="viewer",
            display_name=display_name,
        )
        token, expires_at = await self._issue_session(user.user_id, last_login=True)
        return token, expires_at, await self.get(user.user_id)

    async def resolve(self, token: str) -> tuple[User, str] | None:
        if not token or self.database is None:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT u.*, s.expires_at AS session_expires_at FROM user_sessions s "
                "JOIN users u ON u.user_id=s.user_id WHERE s.token_hash=%s",
                (token_hash,),
            )
            row = await cursor.fetchone()
        if row is None or not row["is_active"]:
            return None
        try:
            expires_at = datetime.fromisoformat(row["session_expires_at"])
        except (TypeError, ValueError):
            return None
        if expires_at <= datetime.now(timezone.utc):
            return None
        return to_user(row), token_hash

    async def logout(self, token_hash: str | None) -> None:
        if not token_hash or self.database is None:
            return
        async with self.database.transaction():
            await self.database.connection.execute(
                "DELETE FROM user_sessions WHERE token_hash=%s", (token_hash,)
            )

    async def change_password(
        self, user_id: str, current_password: str, new_password: str
    ):
        row = await self._row(user_id)
        if row is None:
            raise ChatError("user_not_found", "用户不存在。", 404)
        if not verify_password(current_password, row["password_hash"]):
            raise ChatError("invalid_password", "当前密码不正确。", 403)
        self.validate_password(new_password)
        if current_password == new_password:
            raise ChatError("weak_password", "新密码不能与当前密码相同。", 422)
        async with self.database.transaction():
            connection = self.database.connection
            await connection.execute(
                "UPDATE users SET password_hash=%s, updated_at=%s WHERE user_id=%s",
                (hash_password(new_password), utc_now(), user_id),
            )
            # 改密后全部旧会话失效，再为当前浏览器签发新令牌。
            await connection.execute("DELETE FROM user_sessions WHERE user_id=%s", (user_id,))
        token, expires_at = await self._issue_session(user_id)
        return token, expires_at, await self.get(user_id)


def _request_user(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if not isinstance(user, User):
        raise ChatError("unauthorized", "请先登录。", 401)
    return user


async def require_user(request: Request) -> User:
    return _request_user(request)


async def require_staff(request: Request) -> User:
    user = _request_user(request)
    if user.role not in INGEST_ROLES:
        raise ChatError("forbidden", "当前账号没有上传或管理知识库的权限。", 403)
    return user


async def require_admin(request: Request) -> User:
    user = _request_user(request)
    if user.role not in ADMIN_ROLES:
        raise ChatError("forbidden", "该操作仅限管理员。", 403)
    return user
