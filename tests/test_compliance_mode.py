"""Tests for SEARCH_MODES['compliance'] — active legal risk detection mode."""

import pytest
from prompts import SEARCH_MODES


# --- presence & structure ---

def test_compliance_mode_exists():
    assert "compliance" in SEARCH_MODES


def test_compliance_mode_has_required_fields():
    mode = SEARCH_MODES["compliance"]
    assert "label" in mode
    assert "system" in mode
    assert "prompt_suffix" in mode


def test_compliance_mode_label_is_nonempty():
    assert SEARCH_MODES["compliance"]["label"].strip() != ""


# --- flags ---

def test_compliance_mode_has_ryzyko_prawne_flag():
    assert "[RYZYKO_PRAWNE]" in SEARCH_MODES["compliance"]["system"]


def test_compliance_mode_has_wymaga_weryfikacji_flag():
    assert "[WYMAGA_WERYFIKACJI_PRAWNEJ]" in SEARCH_MODES["compliance"]["system"]


# --- active detection requirement ---

def test_compliance_mode_is_active_not_reactive():
    """Prompt must instruct model to detect risks proactively, not just react to cited statutes."""
    system = SEARCH_MODES["compliance"]["system"]
    assert "aktywne" in system or "samodzielnie" in system


# --- legal areas coverage ---

def test_compliance_mode_covers_rodo():
    assert "RODO" in SEARCH_MODES["compliance"]["system"]


def test_compliance_mode_covers_pzp():
    assert "PZP" in SEARCH_MODES["compliance"]["system"]


def test_compliance_mode_covers_kodeks_pracy():
    assert "Kodeks pracy" in SEARCH_MODES["compliance"]["system"]


def test_compliance_mode_covers_ksh():
    assert "KSH" in SEARCH_MODES["compliance"]["system"]


# --- safety guardrail ---

def test_compliance_mode_has_disclaimer():
    """Prompt must include a disclaimer that output is not a legal opinion."""
    system = SEARCH_MODES["compliance"]["system"]
    assert "ZASTRZEŻENIE" in system
    assert "nie stanowi porady prawnej" in system
