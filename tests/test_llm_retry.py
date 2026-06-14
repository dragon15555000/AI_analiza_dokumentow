from unittest.mock import Mock, patch

import pytest
import requests
from llm.llm_client import LLMClient


@pytest.fixture
def client():
    with patch.dict(
        "os.environ",
        {"NEW_API_BASE_URL": "https://example.com", "NEW_API_KEY": "test_key"},
    ):
        yield LLMClient()


def _http_error(status_code: int) -> requests.HTTPError:
    response = Mock()
    response.status_code = status_code
    return requests.HTTPError(response=response)


@patch("time.sleep")
@patch("requests.post")
def test_retry_then_success(mock_post, mock_sleep, client):
    """Po błędzie 429 retry kończy się sukcesem."""
    bad = Mock()
    bad.raise_for_status = Mock(side_effect=_http_error(429))

    ok = Mock()
    ok.raise_for_status = Mock()
    ok.json.return_value = {"completion": "ok"}

    mock_post.side_effect = [bad, ok]

    result = client.complete("prompt")

    assert result == {"completion": "ok"}
    assert mock_post.call_count == 2
    mock_sleep.assert_called_once()


@patch("time.sleep")
@patch("requests.post")
def test_no_retry_on_400(mock_post, mock_sleep, client):
    """Błąd 400 nie powoduje retry."""
    bad = Mock()
    bad.raise_for_status = Mock(side_effect=_http_error(400))
    mock_post.return_value = bad

    with pytest.raises(requests.HTTPError):
        client.complete("prompt")

    assert mock_post.call_count == 1
    mock_sleep.assert_not_called()


@patch("time.sleep")
@patch("requests.post")
def test_no_retry_on_404(mock_post, mock_sleep, client):
    """Błąd 404 nie powoduje retry."""
    bad = Mock()
    bad.raise_for_status = Mock(side_effect=_http_error(404))
    mock_post.return_value = bad

    with pytest.raises(requests.HTTPError):
        client.complete("prompt")

    assert mock_post.call_count == 1
    mock_sleep.assert_not_called()


@patch("time.sleep")
@patch("requests.post")
def test_exhausted_retries_raises(mock_post, mock_sleep, client):
    """Po wyczerpaniu 5 prób wyjątek jest propagowany."""
    bad = Mock()
    bad.raise_for_status = Mock(side_effect=_http_error(503))
    mock_post.return_value = bad

    with pytest.raises(requests.HTTPError):
        client.complete("prompt")

    assert mock_post.call_count == 5
    assert mock_sleep.call_count == 4  # sleep między próbami, nie po ostatniej
