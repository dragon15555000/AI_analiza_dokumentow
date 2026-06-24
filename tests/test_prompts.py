from prompts import HEALTH_CHECK_PROMPT, SEARCH_MODES


def test_health_check_prompt():
    assert HEALTH_CHECK_PROMPT == "Czy system działa poprawnie?"


def test_legal_system_prompt_enhancements():
    legal_prompt = SEARCH_MODES["legal"]["system"]
    assert "doświadczonym prawnikiem" in legal_prompt
    assert "analizuj jej potencjalne konsekwencje prawne i finansowe" in legal_prompt
    assert "Zaproponuj konkretne działania naprawcze" in legal_prompt
    assert "Priorytetyzuj krytyczne naruszenia" in legal_prompt
    assert "[PRZEPIS_NIEAKTUALNY]" in legal_prompt
    assert "[PRZEPIS_NIEADEKWATNY]" in legal_prompt
    assert "[BŁĘDNE_ZASTOSOWANIE]" in legal_prompt
    assert "[PRZEPIS_NIEZGODNY]" in legal_prompt


def test_detective_system_prompt_task_suggestions():
    detective_prompt_system = SEARCH_MODES["detective"]["system"]
    detective_prompt_suffix = SEARCH_MODES["detective"]["prompt_suffix"]
    assert "wygeneruj zwięzłą listę 3-5 konkretnych, możliwych do wykonania zadań" in detective_prompt_system
    assert "Każde zadanie oznacz sugerowanym priorytetem: [PRIORYTET: WYSOKI], [PRIORYTET: ŚREDNI], [PRIORYTET: NISKI]" in detective_prompt_system
    assert "a następnie listę zadań" in detective_prompt_suffix
    assert "i sugestie zadań" in detective_prompt_suffix
