from fastapi.testclient import TestClient

from app.main import app
from app.config import Settings, get_settings


def test_chatgpt_openapi_components_schemas_is_object():
    client = TestClient(app)

    response = client.get("/chatgpt/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["components"]["schemas"] == {}


def test_chatgpt_openapi_server_url_has_no_trailing_slash():
    app.dependency_overrides[get_settings] = lambda: Settings(
        PUBLIC_BASE_URL="https://stravagpt.onrender.com/"
    )
    client = TestClient(app)

    try:
        response = client.get("/chatgpt/openapi.json")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    schema = response.json()
    assert schema["servers"] == [{"url": "https://stravagpt.onrender.com"}]
