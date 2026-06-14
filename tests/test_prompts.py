from prompts import HEALTH_CHECK_PROMPT


def test_health_check_prompt():
    assert HEALTH_CHECK_PROMPT == "Czy system działa poprawnie?"
