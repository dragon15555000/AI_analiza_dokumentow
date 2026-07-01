from utils.http import json_error, json_success, sse_event


def test_json_success_wraps_success_flag():
    from app import app

    with app.app_context():
        response, status = json_success(message="ok", payload=123)

    assert status == 200
    assert response.get_json() == {"success": True, "message": "ok", "payload": 123}


def test_json_error_wraps_error_flag():
    from app import app

    with app.app_context():
        response, status = json_error("bad request", status=422, field="query")

    assert status == 422
    assert response.get_json() == {"success": False, "error": "bad request", "field": "query"}


def test_sse_event_uses_standard_format():
    payload = {"value": "zażółć"}

    assert sse_event("update", payload) == 'event: update\ndata: {"value": "zażółć"}\n\n'
