"""用户登录、会话吊销与角色权限。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from support import settings, test_database

from app.main import create_app

ADMIN = {"username": "boss", "password": "boss-password", "display_name": "管理员"}
STAFF = {"username": "alice", "password": "alice-password", "display_name": "员工 A"}
VIEWER = {"username": "carol", "password": "carol-password", "display_name": "普通用户 C"}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def bootstrap(client, account=ADMIN):
    response = client.post("/api/v1/auth/setup", json=account)
    assert response.status_code == 201, response.text
    return response.json()


def create(client, token, account, role):
    response = client.post(
        "/api/v1/users",
        json={**account, "role": role},
        headers=bearer(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def login(client, account):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def upload(client, token, name="制度.txt", content="员工年假为十天。"):
    return client.post(
        "/api/v1/knowledge-bases/default/files",
        files={"file": (name, content.encode())},
        headers={"Idempotency-Key": uuid4().hex, **bearer(token)},
    )


def test_setup_is_one_time_and_then_login_is_required():
    with TestClient(create_app(settings())) as client:
        status = client.get("/api/v1/auth/status")
        assert status.status_code == 200
        assert status.json() == {
            "has_users": False,
            "setup_required": True,
            "registration_enabled": True,
            "password_min_length": 8,
        }
        # 初始化前本地放行，便于创建首个管理员。
        assert client.get("/api/v1/conversations").status_code == 200

        created = bootstrap(client)
        assert created["user"]["role"] == "admin"
        assert created["user"]["username"] == "boss"
        assert created["permissions"] == {
            "ingest_files": True,
            "manage_knowledge_bases": True,
            "manage_models": True,
            "manage_users": True,
        }
        assert client.get("/api/v1/auth/status").json()["setup_required"] is False

        # 创建账号后自动收紧：无令牌请求被拒绝。
        unauthorized = client.get("/api/v1/conversations")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["detail"]["code"] == "unauthorized"
        assert unauthorized.headers["www-authenticate"] == "Bearer"
        assert unauthorized.headers["x-request-id"] == unauthorized.json()["detail"]["request_id"]

        again = client.post("/api/v1/auth/setup", json={**ADMIN, "username": "other"})
        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "setup_completed"

        me = client.get("/api/v1/auth/me", headers=bearer(created["token"]))
        assert me.status_code == 200
        assert me.json()["user"]["display_name"] == "管理员"


@pytest.mark.parametrize("action", ["demote", "disable", "delete"])
def test_concurrent_admin_changes_keep_one_active_admin(action):
    service_token = "test-admin-service-token"
    with TestClient(create_app(settings(api_token=service_token))) as client:
        first = bootstrap(client)
        second = create(client, first["token"], STAFF, "admin")
        barrier = Barrier(2)

        def change(user_id):
            barrier.wait(timeout=5)
            if action == "delete":
                return client.delete(f"/api/v1/users/{user_id}", headers=bearer(service_token))
            return client.patch(
                f"/api/v1/users/{user_id}",
                json={"role": "staff"} if action == "demote" else {"is_active": False},
                headers=bearer(service_token),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(change, [first["user"]["user_id"], second["user_id"]]))
        assert sum(response.status_code == 200 for response in responses) == 1
        rejected = next(response for response in responses if response.status_code != 200)
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "last_admin"
        with test_database() as connection:
            remaining = connection.execute(
                "SELECT count(*) FROM users WHERE role='admin' AND is_active"
            ).fetchone()[0]
        assert remaining == 1


def test_repeated_login_failures_are_throttled():
    with TestClient(create_app(settings())) as client:
        bootstrap(client)
        for _ in range(10):
            failed = client.post(
                "/api/v1/auth/login", json={"username": "boss", "password": "wrong-password"}
            )
            assert failed.status_code == 401
        blocked = client.post(
            "/api/v1/auth/login", json={"username": "boss", "password": "wrong-password"}
        )
        assert blocked.status_code == 429
        assert blocked.json()["detail"]["code"] == "too_many_attempts"
        # 限流期内即使密码正确也先拒绝，避免边试边猜。
        correct = {"username": ADMIN["username"], "password": ADMIN["password"]}
        assert client.post("/api/v1/auth/login", json=correct).status_code == 429


def test_registration_creates_read_only_viewer():
    with TestClient(create_app(settings())) as client:
        early = client.post(
            "/api/v1/auth/register",
            json={"username": "early", "password": "early-password"},
        )
        assert early.status_code == 409
        assert early.json()["detail"]["code"] == "setup_required"

        bootstrap(client)
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "dave",
                "password": "dave-password",
                "display_name": "访客 D",
            },
        )
        assert registered.status_code == 201, registered.text
        body = registered.json()
        assert body["user"]["role"] == "viewer"
        assert body["user"]["display_name"] == "访客 D"
        assert body["permissions"]["ingest_files"] is False

        token = body["token"]
        assert client.get("/api/v1/auth/me", headers=bearer(token)).status_code == 200
        assert upload(client, token).status_code == 403

        duplicate = client.post(
            "/api/v1/auth/register",
            json={"username": "DAVE", "password": "dave-password"},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["code"] == "user_exists"

        assert client.post(
            "/api/v1/auth/login",
            json={"username": "dave", "password": "dave-password"},
        ).status_code == 200


def test_registration_can_be_disabled():
    with TestClient(create_app(settings(registration_enabled=False))) as client:
        bootstrap(client)
        assert client.get("/api/v1/auth/status").json()["registration_enabled"] is False
        blocked = client.post(
            "/api/v1/auth/register",
            json={"username": "dave", "password": "dave-password"},
        )
        assert blocked.status_code == 403
        assert blocked.json()["detail"]["code"] == "registration_disabled"


def test_admin_can_be_bootstrapped_from_environment():
    config = settings(
        admin_username="root",
        admin_password="root-password",
        admin_display_name="根管理员",
    )
    with TestClient(create_app(config)) as client:
        status = client.get("/api/v1/auth/status").json()
        assert status["has_users"] is True and status["setup_required"] is False
        token = login(client, {"username": "root", "password": "root-password"})
        me = client.get("/api/v1/auth/me", headers=bearer(token)).json()
        assert me["user"]["role"] == "admin"
        assert me["user"]["display_name"] == "根管理员"

    # 已有账号时不再根据环境变量创建第二个管理员。
    with TestClient(
        create_app(settings(admin_username="other", admin_password="other-password"))
    ) as client:
        assert client.get("/api/v1/auth/status").json()["has_users"] is True
        assert client.post(
            "/api/v1/auth/login",
            json={"username": "other", "password": "other-password"},
        ).status_code == 401


def test_invalid_bootstrap_password_keeps_setup_flow():
    with TestClient(create_app(settings(admin_password="short"))) as client:
        status = client.get("/api/v1/auth/status").json()
        assert status["has_users"] is False and status["setup_required"] is True


def test_login_logout_and_wrong_password():
    with TestClient(create_app(settings())) as client:
        bootstrap(client)
        wrong = client.post(
            "/api/v1/auth/login", json={"username": "boss", "password": "wrong-password"}
        )
        assert wrong.status_code == 401
        assert wrong.json()["detail"]["code"] == "invalid_credentials"

        token = login(client, ADMIN)
        assert client.get("/api/v1/auth/me", headers=bearer(token)).status_code == 200
        assert client.post("/api/v1/auth/logout", headers=bearer(token)).status_code == 200
        assert client.get("/api/v1/auth/me", headers=bearer(token)).status_code == 401

        # 用户名不区分大小写，密码仍区分。
        upper = client.post(
            "/api/v1/auth/login", json={"username": "BOSS", "password": ADMIN["password"]}
        )
        assert upper.status_code == 200


def test_viewer_cannot_ingest_but_staff_can():
    with TestClient(create_app(settings())) as client:
        admin = bootstrap(client)["token"]
        staff = create(client, admin, STAFF, "staff")
        create(client, admin, VIEWER, "viewer")
        viewer_token = login(client, VIEWER)
        staff_token = login(client, STAFF)

        viewer_me = client.get("/api/v1/auth/me", headers=bearer(viewer_token)).json()
        assert viewer_me["permissions"] == {
            "ingest_files": False,
            "manage_knowledge_bases": False,
            "manage_models": False,
            "manage_users": False,
        }

        forbidden = upload(client, viewer_token)
        assert forbidden.status_code == 403
        assert forbidden.json()["detail"]["code"] == "forbidden"
        assert upload(client, staff_token).status_code == 201
        assert staff["role"] == "staff"

        # 普通用户仍可读取资料、发起问答和检索。
        files = client.get(
            "/api/v1/knowledge-bases/default/files", headers=bearer(viewer_token)
        )
        assert files.status_code == 200
        assert client.post(
            "/api/v1/conversations", headers=bearer(viewer_token)
        ).status_code == 201

        # 员工可以创建知识库，普通用户不可以。
        assert client.post(
            "/api/v1/knowledge-bases", json={"name": "员工库"}, headers=bearer(staff_token)
        ).status_code == 201
        denied = client.post(
            "/api/v1/knowledge-bases", json={"name": "普通用户库"}, headers=bearer(viewer_token)
        )
        assert denied.status_code == 403


def test_admin_only_user_management_and_model_settings():
    with TestClient(create_app(settings())) as client:
        admin = bootstrap(client)["token"]
        create(client, admin, VIEWER, "viewer")
        viewer_token = login(client, VIEWER)

        assert client.get("/api/v1/users", headers=bearer(admin)).status_code == 200
        assert client.get("/api/v1/users", headers=bearer(viewer_token)).status_code == 403
        models = client.get("/api/v1/settings/models", headers=bearer(viewer_token))
        assert models.status_code == 403
        assert client.get("/api/v1/settings/models", headers=bearer(admin)).status_code == 200
        assert client.post(
            "/api/v1/maintenance/files/gc", headers=bearer(viewer_token)
        ).status_code == 403

        duplicate = client.post(
            "/api/v1/users",
            json={**VIEWER, "username": "CAROL", "role": "viewer"},
            headers=bearer(admin),
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["code"] == "user_exists"

        weak = client.post(
            "/api/v1/users",
            json={**VIEWER, "username": "shortpass", "password": "123", "role": "viewer"},
            headers=bearer(admin),
        )
        assert weak.status_code == 422
        assert weak.json()["detail"]["code"] == "weak_password"

        bad_role = client.post(
            "/api/v1/users",
            json={**VIEWER, "username": "roleless", "role": "owner"},
            headers=bearer(admin),
        )
        assert bad_role.status_code == 422

        users = client.get("/api/v1/users", headers=bearer(admin)).json()
        assert [user["username"] for user in users] == ["boss", "carol"]


def test_deactivation_and_password_change_revoke_sessions():
    with TestClient(create_app(settings())) as client:
        admin = bootstrap(client)["token"]
        viewer = create(client, admin, VIEWER, "viewer")
        first = login(client, VIEWER)

        changed = client.post(
            "/api/v1/auth/password",
            json={"current_password": VIEWER["password"], "new_password": "carol-password-2"},
            headers=bearer(first),
        )
        assert changed.status_code == 200, changed.text
        second = changed.json()["token"]
        assert second != first
        assert client.get("/api/v1/auth/me", headers=bearer(first)).status_code == 401
        assert client.get("/api/v1/auth/me", headers=bearer(second)).status_code == 200

        wrong = client.post(
            "/api/v1/auth/password",
            json={"current_password": "nope-nope", "new_password": "another-password"},
            headers=bearer(second),
        )
        assert wrong.status_code == 403

        disabled = client.patch(
            f"/api/v1/users/{viewer['user_id']}",
            json={"is_active": False},
            headers=bearer(admin),
        )
        assert disabled.status_code == 200
        assert client.get("/api/v1/auth/me", headers=bearer(second)).status_code == 401
        blocked = client.post(
            "/api/v1/auth/login",
            json={"username": VIEWER["username"], "password": "carol-password-2"},
        )
        assert blocked.status_code == 403
        assert blocked.json()["detail"]["code"] == "user_disabled"


def test_admin_role_changes_apply_immediately_and_last_admin_is_protected():
    with TestClient(create_app(settings())) as client:
        admin = bootstrap(client)["token"]
        me = client.get("/api/v1/auth/me", headers=bearer(admin)).json()["user"]

        last = client.patch(
            f"/api/v1/users/{me['user_id']}", json={"role": "viewer"}, headers=bearer(admin)
        )
        assert last.status_code == 409
        assert last.json()["detail"]["code"] == "last_admin"
        assert client.delete(
            f"/api/v1/users/{me['user_id']}", headers=bearer(admin)
        ).json()["detail"]["code"] == "cannot_delete_self"

        second = create(
            client, admin, {"username": "boss2", "password": "boss2-password"}, "admin"
        )
        second_token = login(
            client, {"username": "boss2", "password": "boss2-password"}
        )
        assert client.get("/api/v1/users", headers=bearer(second_token)).status_code == 200
        demoted = client.patch(
            f"/api/v1/users/{second['user_id']}", json={"role": "viewer"}, headers=bearer(admin)
        )
        assert demoted.status_code == 200
        # 角色变更立即生效：旧令牌仍有效，但权限已降级。
        assert client.get("/api/v1/users", headers=bearer(second_token)).status_code == 403
        assert client.patch(
            f"/api/v1/users/{second['user_id']}", json={"is_active": False}, headers=bearer(admin)
        ).status_code == 200


def test_expired_session_is_rejected():
    with TestClient(create_app(settings())) as client:
        bootstrap(client)
        token = login(client, ADMIN)
        with test_database() as connection:
            connection.execute(
                "UPDATE user_sessions SET expires_at=%s", ("2000-01-01T00:00:00+00:00",)
            )
        assert client.get("/api/v1/auth/me", headers=bearer(token)).status_code == 401


def test_service_token_acts_as_admin_and_blocks_anonymous():
    with TestClient(create_app(settings(api_token="s3cret-token"))) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/conversations").status_code == 401
        assert client.get("/api/v1/conversations", headers=bearer("wrong")).status_code == 401
        service = client.get("/api/v1/conversations", headers=bearer("s3cret-token"))
        assert service.status_code == 200
        me = client.get("/api/v1/auth/me", headers=bearer("s3cret-token")).json()
        assert me["user"]["role"] == "admin" and me["user"]["service"] is True

        bootstrap(client)
        assert client.get("/api/v1/auth/me", headers=bearer("s3cret-token")).status_code == 200
        assert client.get("/api/v1/conversations").status_code == 401


def test_public_endpoints_and_cors_preflight_stay_open():
    with TestClient(create_app(settings())) as client:
        bootstrap(client)
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/auth/status").status_code == 200
        preflight = client.options(
            "/api/v1/conversations",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "*"
