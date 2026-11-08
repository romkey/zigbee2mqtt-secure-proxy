from fastapi.testclient import TestClient

from zigbee2mqtt_secure_proxy import __version__
from zigbee2mqtt_secure_proxy.app import create_app
from zigbee2mqtt_secure_proxy.config import Settings


def test_healthz_needs_no_dependencies(settings: Settings) -> None:
    # No lifespan: neither the database nor MQTT has been touched.
    response = TestClient(create_app(settings)).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_unknown_path_is_404(settings: Settings) -> None:
    assert TestClient(create_app(settings)).get("/nope").status_code == 404


def test_generated_api_docs_are_not_served(settings: Settings) -> None:
    client = TestClient(create_app(settings))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
