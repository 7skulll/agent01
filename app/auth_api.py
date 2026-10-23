"""登录、账号与角色管理接口。"""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.errors import ChatError
from app.users import (
    AuthService,
    Permissions,
    User,
    permissions_for,
    require_admin,
    require_user,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
users_router = APIRouter(prefix="/api/v1/users", tags=["users"])


class AuthStatus(BaseModel):
    has_users: bool
    setup_required: bool
    registration_enabled: bool
    password_min_length: int


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class SetupRequest(LoginRequest):
    display_name: str = Field(default="", max_length=64)


class RegisterRequest(LoginRequest):
    display_name: str = Field(default="", max_length=64)


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class SessionInfo(BaseModel):
    user: User
    permissions: Permissions


class LoginResponse(SessionInfo):
    token: str
    expires_at: str


class LogoutResult(BaseModel):
    ok: bool = True


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(default="", max_length=64)
    role: str = Field(default="viewer", pattern="^(admin|staff|viewer)$")
    password: str = Field(min_length=1, max_length=128)


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, max_length=64)
    role: str | None = Field(default=None, pattern="^(admin|staff|viewer)$")
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=1, max_length=128)


class UserDelete(BaseModel):
    user_id: str
    deleted: bool = True


def service(request: Request) -> AuthService:
    return request.app.state.auth


def session(user: User) -> SessionInfo:
    return SessionInfo(user=user, permissions=permissions_for(user.role))


def login_response(token, expires_at, user) -> LoginResponse:
    return LoginResponse(
        token=token,
        expires_at=expires_at,
        user=user,
        permissions=permissions_for(user.role),
    )


@router.get("/status", response_model=AuthStatus)
async def auth_status(request: Request):
    auth = service(request)
    has_users = await auth.has_users()
    return AuthStatus(
        has_users=has_users,
        setup_required=not has_users,
        registration_enabled=auth.settings.registration_enabled,
        password_min_length=auth.password_min_length,
    )


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request):
    token, expires_at, user = await service(request).authenticate(body.username, body.password)
    return login_response(token, expires_at, user)


@router.post("/setup", response_model=LoginResponse, status_code=201)
async def setup(body: SetupRequest, request: Request):
    """首次初始化：仅在系统没有任何账号时创建管理员。"""
    token, expires_at, user = await service(request).setup(
        username=body.username,
        password=body.password,
        display_name=body.display_name,
    )
    return login_response(token, expires_at, user)


@router.post("/register", response_model=LoginResponse, status_code=201)
async def register(body: RegisterRequest, request: Request):
    """自助注册固定为普通用户；员工与管理员仍由管理员分配。"""
    token, expires_at, user = await service(request).register(
        username=body.username,
        password=body.password,
        display_name=body.display_name,
    )
    return login_response(token, expires_at, user)


@router.post("/logout", response_model=LogoutResult)
async def logout(request: Request):
    await service(request).logout(getattr(request.state, "session_hash", None))
    return LogoutResult()


@router.get("/me", response_model=SessionInfo)
async def me(user: User = Depends(require_user)):
    return session(user)


@router.post("/password", response_model=LoginResponse)
async def change_password(
    body: PasswordChange, request: Request, user: User = Depends(require_user)
):
    token, expires_at, updated = await service(request).change_password(
        user.user_id, body.current_password, body.new_password
    )
    return login_response(token, expires_at, updated)


@users_router.get("", response_model=list[User])
async def list_users(request: Request, _: User = Depends(require_admin)):
    return await service(request).list_users()


@users_router.post("", response_model=User, status_code=201)
async def create_user(body: UserCreate, request: Request, _: User = Depends(require_admin)):
    return await service(request).create_user(
        username=body.username,
        password=body.password,
        role=body.role,
        display_name=body.display_name,
    )


@users_router.patch("/{user_id}", response_model=User)
async def update_user(
    user_id: str, body: UserUpdate, request: Request, _: User = Depends(require_admin)
):
    if not body.model_fields_set:
        raise ChatError("no_changes", "没有需要更新的字段。", 422)
    return await service(request).update_user(
        user_id,
        display_name=body.display_name,
        role=body.role,
        is_active=body.is_active,
        password=body.password,
    )


@users_router.delete("/{user_id}", response_model=UserDelete)
async def delete_user(user_id: str, request: Request, actor: User = Depends(require_admin)):
    await service(request).delete_user(user_id, actor_id=actor.user_id)
    return UserDelete(user_id=user_id)
