from collections.abc import Iterator

import httpx
import pytest
from authlib.integrations.starlette_client import (  # type: ignore[import-untyped]  # Authlib ships no type hints
    OAuthError,
)
from fastapi.testclient import TestClient

from tests.conftest import Harness, _run_app, with_settings
from zigbee2mqtt_secure_proxy.config import Settings


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def hold(admin: Harness, count: int = 1, tier: str = "read-write") -> str:
    token = admin.create_key(tier, for_review=True)
    for i in range(count):
        admin.client.post(
            f"/api/devices/lamp{i}/set", json={"state": "ON"}, headers=bearer(token)
        )
    return token


def decide(admin: Harness, ids: list[int], verdict: str) -> None:
    admin.client.post(
        "/_proxy/review",
        data={"csrf": admin.csrf(), "action_id": ids, "verdict": verdict},
    )
    admin.drain()


def test_pages_require_login(harness: Harness) -> None:
    for path in ("/_proxy/", "/_proxy/keys", "/_proxy/review", "/_proxy/log"):
        response = harness.client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/_proxy/login"


def test_login_redirects_to_the_provider(harness: Harness) -> None:
    response = harness.client.get("/_proxy/login", follow_redirects=False)
    assert response.status_code == 302
    assert "http://proxy.test/_proxy/auth/callback" in response.headers["location"]


def test_login_creates_an_admin_and_records_it(harness: Harness) -> None:
    harness.login()
    assert harness.client.get("/_proxy/keys").status_code == 200
    assert harness.rows("SELECT oidc_subject, email FROM admin_users") == [
        ("alice", "a@example.test")
    ]
    assert harness.rows("SELECT kind FROM admin_events") == [("login",)]


def test_failed_login_shows_an_error(harness: Harness) -> None:
    harness.oidc.error = OAuthError("bad")
    assert harness.client.get("/_proxy/auth/callback").status_code == 400


def test_missing_subject_is_refused(harness: Harness) -> None:
    harness.oidc.claims = {}
    assert harness.client.get("/_proxy/auth/callback").status_code == 403


@pytest.fixture
def grouped(settings: Settings) -> Iterator[Harness]:
    yield from _run_app(with_settings(settings, oidc_required_group="staff"))


def test_required_group_is_enforced(grouped: Harness) -> None:
    grouped.oidc.claims = {"sub": "bob", "groups": ["other"]}
    assert grouped.client.get("/_proxy/auth/callback").status_code == 403
    grouped.oidc.claims = {"sub": "bob", "groups": ["staff"]}
    grouped.login()


def test_logout(admin: Harness) -> None:
    admin.client.post("/_proxy/logout", data={"csrf": admin.csrf()})
    response = admin.client.get("/_proxy/keys", follow_redirects=False)
    assert response.status_code == 303


def test_forms_need_the_csrf_token(admin: Harness) -> None:
    response = admin.client.post(
        "/_proxy/keys", data={"csrf": "wrong", "name": "x", "tier": "read"}
    )
    assert response.status_code == 403
    assert admin.rows("SELECT id FROM api_keys") == []


def test_ui_sends_security_headers(admin: Harness) -> None:
    response = admin.client.get("/_proxy/keys")
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


def test_keys_are_shown_once_and_stored_hashed(admin: Harness) -> None:
    token = admin.create_key("read-write", for_review=True)
    stored = admin.rows("SELECT key_hash, tier, for_review FROM api_keys")[0]
    assert token not in stored[0]
    assert stored[1:] == ("read-write", True)
    assert token not in admin.client.get("/_proxy/keys").text
    assert admin.rows("SELECT kind FROM admin_events ORDER BY id")[-1] == (
        "key_created",
    )


@pytest.mark.parametrize(
    "data", [{"name": "", "tier": "read"}, {"name": "x", "tier": "god"}]
)
def test_create_key_validates(admin: Harness, data: dict[str, str]) -> None:
    response = admin.client.post("/_proxy/keys", data={"csrf": admin.csrf(), **data})
    assert response.status_code == 400


def test_changing_a_tier_takes_effect(admin: Harness) -> None:
    token = admin.create_key()
    headers = bearer(token)
    assert admin.client.post("/api/devices/l/set", headers=headers).status_code == 403
    admin.client.post(
        "/_proxy/keys/1", data={"csrf": admin.csrf(), "tier": "read-write"}
    )
    assert admin.client.post("/api/devices/l/set", headers=headers).status_code == 202


def test_revoked_keys_stop_working(admin: Harness) -> None:
    headers = bearer(admin.create_key())
    admin.client.post("/_proxy/keys/1/revoke", data={"csrf": admin.csrf()})
    assert admin.client.get("/api/bridge/info", headers=headers).status_code == 401
    again = admin.client.post("/_proxy/keys/1/revoke", data={"csrf": admin.csrf()})
    assert again.status_code == 404


def test_review_page_groups_held_actions(admin: Harness) -> None:
    hold(admin, 2)
    page = admin.client.get("/_proxy/review").text
    assert "lamp0/set" in page
    assert "lamp1/set" in page
    assert "All 2:" in page
    assert "Batch:" in page


def test_empty_review_page(admin: Harness) -> None:
    assert "Nothing is waiting" in admin.client.get("/_proxy/review").text


def test_approving_runs_the_action(admin: Harness) -> None:
    hold(admin)
    decide(admin, [1], "approve")
    assert admin.broker.published == [("lamp0/set", {"state": "ON"})]
    row = admin.rows("SELECT status, decided_by, executed_at FROM actions")[0]
    assert row[:2] == ("executed", 1)
    assert row[2] is not None
    assert admin.rows("SELECT kind FROM admin_events ORDER BY id")[-1] == (
        "review_approved",
    )


def test_rejecting_never_runs_the_action(admin: Harness) -> None:
    hold(admin)
    decide(admin, [1], "reject")
    assert admin.broker.published == []
    assert admin.rows("SELECT status FROM actions") == [("rejected",)]


def test_approve_applies_only_to_the_posted_ids(admin: Harness) -> None:
    hold(admin, 2)
    decide(admin, [1], "approve")
    assert admin.rows("SELECT status FROM actions ORDER BY id") == [
        ("executed",),
        ("pending",),
    ]


def test_approving_twice_runs_once(admin: Harness) -> None:
    hold(admin)
    decide(admin, [1], "approve")
    decide(admin, [1], "approve")
    assert len(admin.broker.published) == 1


def test_approve_all_keeps_request_order(admin: Harness) -> None:
    hold(admin, 3)
    decide(admin, [3, 1, 2], "approve")
    assert [topic for topic, _ in admin.broker.published] == [
        "lamp0/set",
        "lamp1/set",
        "lamp2/set",
    ]


def test_unknown_verdict_is_refused(admin: Harness) -> None:
    response = admin.client.post(
        "/_proxy/review", data={"csrf": admin.csrf(), "verdict": "maybe"}
    )
    assert response.status_code == 400


def test_approving_for_a_revoked_key_does_nothing(admin: Harness) -> None:
    hold(admin)
    admin.client.post("/_proxy/keys/1/revoke", data={"csrf": admin.csrf()})
    decide(admin, [1], "approve")
    assert admin.broker.published == []
    assert admin.rows("SELECT status FROM actions") == [("rejected",)]


def test_expired_actions_cannot_be_approved(admin: Harness) -> None:
    hold(admin)
    admin.proxy.settings = with_settings(admin.proxy.settings, review_ttl=0)
    decide(admin, [1], "approve")
    assert admin.broker.published == []
    assert admin.rows("SELECT status FROM actions") == [("expired",)]


def test_held_secrets_are_redacted_once_decided(admin: Harness) -> None:
    token = admin.create_key("read-write", for_review=True)
    admin.client.post(
        "/api/bridge/request/options",
        json={"options": {"mqtt": {"password": "hunter2"}}},  # not-a-secret
        headers=bearer(token),
    )
    decide(admin, [1], "reject")
    assert "hunter2" not in str(admin.rows("SELECT payload FROM actions"))


def test_log_shows_batches_and_filters(admin: Harness) -> None:
    hold(admin, 2)
    page = admin.client.get("/_proxy/log").text
    assert "2 actions" in page
    assert "lamp0" in page
    query = (
        "?key=1&status=pending&device=lamp0&from=2020-01-01T00:00&to=2999-01-01T00:00"
    )
    assert "2 actions" in admin.client.get(f"/_proxy/log{query}").text
    assert "No actions match" in admin.client.get("/_proxy/log?device=fan").text
    assert "No actions match" in admin.client.get("/_proxy/log?status=expired").text
    assert "No actions match" in admin.client.get("/_proxy/log?page=2").text
    assert admin.client.get("/_proxy/log?from=garbage&page=x").status_code == 200


def test_admin_events_page(admin: Harness) -> None:
    assert "login" in admin.client.get("/_proxy/events").text


def test_payloads_are_escaped(admin: Harness) -> None:
    token = admin.create_key("read-write", for_review=True)
    admin.client.post(
        "/api/devices/lamp/set", json={"x": "<script>"}, headers=bearer(token)
    )
    assert "<script>" not in admin.client.get("/_proxy/review").text


def test_home_redirects_to_review(admin: Harness) -> None:
    response = admin.client.get("/_proxy/", follow_redirects=False)
    assert response.headers["location"] == "/_proxy/review"


def frontend_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/boom":
        raise httpx.ConnectError("down")
    assert "cookie" not in request.headers
    return httpx.Response(
        200,
        content=f"file {request.url.path}".encode(),
        headers={"content-type": "text/html", "x-internal": "1"},
    )


@pytest.fixture
def frontend(settings: Settings) -> Iterator[TestClient]:
    changed = with_settings(settings, frontend_url="http://z2m.test")
    for harness in _run_app(changed, transport=httpx.MockTransport(frontend_handler)):
        yield harness.client


def test_frontend_is_served_through_the_proxy(frontend: TestClient) -> None:
    frontend.cookies.set("z2m_proxy_session", "x", domain="proxy.test")
    response = frontend.get("/assets/app.js")
    assert response.text == "file /assets/app.js"
    assert response.headers["content-type"].startswith("text/html")
    assert "x-internal" not in response.headers


def test_frontend_outage_is_502(frontend: TestClient) -> None:
    assert frontend.get("/boom").status_code == 502


def test_frontend_does_not_shadow_the_proxys_routes(frontend: TestClient) -> None:
    assert frontend.get("/healthz").json()["status"] == "ok"


def test_no_frontend_means_404(harness: Harness) -> None:
    assert harness.client.get("/index.html").status_code == 404
