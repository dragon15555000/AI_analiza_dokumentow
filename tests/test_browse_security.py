"""Testy regresyjne bezpieczenstwa endpointu /browse — path traversal.

Pokrycie:
- _path_is_allowed() blokuje traversal, absolutne sciezki poza SEARCH_ROOTS,
  projects_evil poza rootem oraz symlink escape
- lancuch normalize → Path → resolve → _path_is_allowed nie przepuszcza
  zadnego payload-u traversal do filesystemu
- poprawna sciezka wewnatrz SEARCH_ROOTS przechodzi
- Windows path ze spacjami przechodzi przez prefilter (shell=False w wslpath)

Zrodlo audytu:
  _path_is_allowed  — app.py linie 1326-1337  (robi resolve() wewnetrznie)
  /browse route     — app.py linie 3792-3850  (resolve() przed _path_is_browsable)
  Werdykt: brak exploitable path traversal; app.py bez zmian.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import _normalize_browse_path, _path_is_allowed


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_root(tmp_path):
    """Tymczasowy katalog udajacy jedyny wpis w SEARCH_ROOTS."""
    root = tmp_path / "allowed_root"
    root.mkdir()
    return root


@pytest.fixture()
def patched_roots(tmp_root):
    """Nadpisuje _resolve_allowed_roots() zwracajac tylko [tmp_root.resolve()]."""
    with patch("app._resolve_allowed_roots", return_value=[tmp_root.resolve()]):
        yield tmp_root


# ---------------------------------------------------------------------------
# _path_is_allowed — finalny guard
# ---------------------------------------------------------------------------


class TestPathIsAllowed:
    def test_blocks_etc_passwd(self, patched_roots):
        assert not _path_is_allowed(Path("/etc/passwd"))

    def test_blocks_dotdot_traversal(self, patched_roots, tmp_root):
        evil = tmp_root / ".." / ".." / "etc" / "passwd"
        assert not _path_is_allowed(evil)

    def test_blocks_backslash_traversal_pattern(self, patched_roots, tmp_root):
        # ..\\windows\\system32 jako posix po normalizacji
        evil = tmp_root / ".." / ".." / "windows" / "system32"
        assert not _path_is_allowed(evil)

    def test_blocks_absolute_outside_roots(self, patched_roots, tmp_path):
        outside = tmp_path / "outside_dir"
        outside.mkdir()
        assert not _path_is_allowed(outside)

    def test_blocks_projects_evil(self, patched_roots, tmp_path):
        projects_evil = tmp_path / "projects_evil"
        projects_evil.mkdir()
        assert not _path_is_allowed(projects_evil)

    def test_blocks_symlink_escape(self, patched_roots, tmp_root, tmp_path):
        """Symlink wewnątrz allowed_root wskazujacy na katalog poza rootem."""
        outside = tmp_path / "secret_outside"
        outside.mkdir()
        link = tmp_root / "escape_link"
        link.symlink_to(outside)
        # resolve() rozkłada symlink — wynik lezy poza rootem
        assert not _path_is_allowed(link)

    def test_allows_root_itself(self, patched_roots, tmp_root):
        assert _path_is_allowed(tmp_root)

    def test_allows_file_inside_root(self, patched_roots, tmp_root):
        valid = tmp_root / "raport.pdf"
        valid.touch()
        assert _path_is_allowed(valid)

    def test_allows_nested_subdir(self, patched_roots, tmp_root):
        deep = tmp_root / "a" / "b" / "c"
        deep.mkdir(parents=True)
        assert _path_is_allowed(deep)


# ---------------------------------------------------------------------------
# Lancuch kompletny: normalize → Path → resolve → _path_is_allowed
# Dokladnie jak w /browse (linie 3808, 3822, 3825)
# ---------------------------------------------------------------------------


class TestBrowseChain:
    """Symuluje przepływ endpointu /browse bez uruchamiania serwera Flask."""

    @staticmethod
    def _chain(raw: str) -> bool:
        normalized = _normalize_browse_path(raw)
        try:
            resolved = Path(normalized).expanduser().resolve()
        except OSError:
            return False
        return _path_is_allowed(resolved)

    def test_dotdot_slash_blocked(self, patched_roots, tmp_root):
        evil = str(tmp_root) + "/../../../etc/passwd"
        assert not self._chain(evil)

    def test_multiple_dotdot_blocked(self, patched_roots, tmp_root):
        evil = str(tmp_root) + "/../../.."
        assert not self._chain(evil)

    def test_percent_2e_dotdot_blocked(self, patched_roots, tmp_root):
        # Flask dekoduje %2e → '.' wiec %2e%2e staje sie '..' przed dotarciem do funkcji
        evil = str(tmp_root) + "/%2e%2e/%2e%2e/etc/passwd"
        assert not self._chain(evil)

    def test_double_percent_encoded_blocked(self, patched_roots, tmp_root):
        # %252e = literalny '%2e' po jednym URL-decode — Path nie traktuje go jako '..'
        # ale resolve() i relative_to() blokuja sciezke poza rootem
        evil = str(tmp_root) + "/%252e%252e/secret"
        assert not self._chain(evil)

    def test_absolute_path_outside_roots_blocked(self, patched_roots):
        assert not self._chain("/tmp/evil_payload")

    def test_projects_evil_outside_roots_blocked(self, patched_roots, tmp_path):
        pe = tmp_path / "projects_evil"
        pe.mkdir()
        assert not self._chain(str(pe))

    def test_symlink_escape_via_chain(self, patched_roots, tmp_root, tmp_path):
        outside = tmp_path / "secret_via_link"
        outside.mkdir()
        link = tmp_root / "link_to_outside"
        link.symlink_to(outside)
        # resolve() w lancuchu rozkłada symlink — wynik poza rootem
        assert not self._chain(str(link))

    def test_valid_path_inside_root_allowed(self, patched_roots, tmp_root):
        valid_dir = tmp_root / "dokumenty"
        valid_dir.mkdir()
        assert self._chain(str(valid_dir))

    def test_null_byte_does_not_raise_true(self, patched_roots):
        """Null byte — musi zwrocic False lub rzucic wyjatek, nigdy True."""
        try:
            result = self._chain("/mnt/g/foo\x00bar")
            assert result is False
        except (ValueError, OSError):
            pass  # rzucenie wyjatku jest akceptowalne

    def test_newline_in_path_does_not_raise_true(self, patched_roots):
        """Newline w sciezce — musi zwrocic False lub rzucic wyjatek, nigdy True."""
        try:
            result = self._chain("/mnt/g/foo\nbar")
            assert result is False
        except (ValueError, OSError):
            pass

    def test_windows_path_with_spaces_prefilter(self):
        """Windows path ze spacjami przechodzi przez normalize bez bledu (shell=False)."""
        # Sprawdzamy tylko ze normalize nie rzuca — finalny guard i tak blokuje
        # sciezke poza SEARCH_ROOTS; test potwierdza brak blokady na poziomie prefiltra
        try:
            result = _normalize_browse_path("C:\\Program Files\\data.db")
            assert isinstance(result, str) and len(result) > 0
        except Exception as e:
            pytest.fail(f"_normalize_browse_path rzucił wyjątek dla legalnej ścieżki Windows: {e}")
