import os
import sys
import pytest
from pathlib import Path

# Add project root to path to allow importing app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Import the Flask app instance
from app import app

TEST_API_KEY = "test-secret-key-for-path-traversal"

@pytest.fixture
def client():
    """Create a test client for the Flask app."""
    app.config['TESTING'] = True
    # This context is needed to have access to request context for tests
    with app.test_request_context():
        with app.test_client() as client:
            yield client

@pytest.fixture(autouse=True)
def test_environment(monkeypatch, tmp_path: Path):
    """Set up a controlled test environment for path validation."""
    safe_root = tmp_path / "safe_root"
    safe_root.mkdir()
    (safe_root / "test_file.txt").write_text("content")

    # This file should be inaccessible
    forbidden_file = tmp_path / "secret.txt"
    forbidden_file.write_text("secret")

    # Mock the function that resolves allowed roots to ensure isolation
    monkeypatch.setattr('app._resolve_allowed_roots', lambda: [safe_root.resolve()])
    
    # Mock the default browse path to start inside our safe temp directory
    monkeypatch.setattr('app._default_browse_path', lambda: safe_root.resolve())
    
    # Set a test API key for the app to use
    monkeypatch.setenv("APP_API_KEY", TEST_API_KEY)
    monkeypatch.setattr('app.APP_API_KEY', TEST_API_KEY)


def test_browse_valid_path_and_no_name_error(client, tmp_path):
    """
    Tests browsing a valid path, confirming the NameError bug is fixed.
    The test will fail if the endpoint returns a 500 error.
    """
    headers = {"X-API-Key": TEST_API_KEY}
    rv = client.get('/browse', headers=headers)
    assert rv.status_code == 200
    json_data = rv.get_json()
    assert json_data['success'] is True
    # Check if the response contains the test file
    assert 'test_file.txt' in [e['name'] for e in json_data['entries']]
    # Check if the current path is correct
    assert Path(json_data['current']).name == 'safe_root'


def test_browse_path_traversal_attack(client):
    """Tests a classic path traversal attempt."""
    headers = {"X-API-Key": TEST_API_KEY}
    # Attempt to access the parent directory to find the sibling 'secret.txt'
    rv = client.get('/browse?path=../', headers=headers)
    assert rv.status_code == 403
    json_data = rv.get_json()
    assert json_data['error'] == "Dostęp do tej ścieżki jest zabroniony"


def test_browse_absolute_path_attack(client):
    """Tests browsing an absolute path outside the allowed root."""
    headers = {"X-API-Key": TEST_API_KEY}
    rv = client.get('/browse?path=/etc/passwd', headers=headers)
    # This path is outside the tmp_path used for the safe root
    assert rv.status_code == 403
    json_data = rv.get_json()
    assert json_data['error'] == "Dostęp do tej ścieżki jest zabroniony"


def test_browse_encoded_traversal_attack(client, tmp_path):
    """Tests a URL-encoded path traversal attempt."""
    headers = {"X-API-Key": TEST_API_KEY}
    # Path to the secret file relative to the safe_root
    # tmp_path -> safe_root
    # tmp_path -> secret.txt
    # So from safe_root we need to go one level up.
    # %2e is '.'
    rv = client.get('/browse?path=%2e%2e/', headers=headers)
    assert rv.status_code == 403
    json_data = rv.get_json()
    assert json_data['error'] == "Dostęp do tej ścieżki jest zabroniony"
