from fastapi.testclient import TestClient

from app.main import app


def test_chatgpt_openapi_components_schemas_is_object():
    client = TestClient(app)

    response = client.get("/chatgpt/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["components"]["schemas"] == {}
