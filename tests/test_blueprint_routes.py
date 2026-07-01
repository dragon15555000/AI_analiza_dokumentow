from app import app


def test_extracted_blueprint_routes_are_registered():
    rules = {rule.rule for rule in app.url_map.iter_rules()}

    assert "/health" in rules
    assert "/api/update/status" in rules
    assert "/api/update/pull" in rules
    assert "/api/update/restart" in rules
    assert "/api/service/status" in rules
    assert "/api/service/restart" in rules
