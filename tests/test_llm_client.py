from unittest.mock import Mock, patch

import pytest
import requests
from llm.llm_client import LLMClient


@pytest.fixture
def llm_client():
    with patch.dict(
        "os.environ", {"NEW_API_BASE_URL": "https://example.com", "NEW_API_KEY": "api_key"}
    ):
        yield LLMClient()


def test_llm_client_init(llm_client):
    assert llm_client.base_url == "https://example.com"
    assert llm_client.api_key == "api_key"


@patch("requests.post")
def test_llm_client_complete(mock_post, llm_client):
    mock_response = Mock()
    mock_response.json.return_value = {"completion": "example completion"}
    mock_response.raise_for_status = Mock()
    mock_post.return_value = mock_response

    completion = llm_client.complete("example prompt")
    assert completion == {"completion": "example completion"}
    mock_post.assert_called_once_with(
        "https://example.com/complete",
        headers={"Authorization": "Bearer api_key"},
        json={"prompt": "example prompt"},
    )


def _make_http_error(status_code: int) -> requests.HTTPError:
    """Tworzy requests.HTTPError z podanym kodem statusu."""
    response = Mock()
    response.status_code = status_code
    error = requests.HTTPError(response=response)
    return error


@patch("requests.post")
def test_retry_on_429_then_success(mock_post, llm_client):
    """Po błędzie 429 powinno nastąpić retry, które kończy się sukcesem."""
    error_response = Mock()
    error_response.status_code = 429
    error_response.raise_for_status = Mock(side_effect=_make_http_error(429))

    success_response = Mock()
    success_response.status_code = 200
    success_response.raise_for_status = Mock()
    success_response.json.return_value = {"completion": "ok"}

    mock_post.side_effect = [error_response, success_response]

    result = llm_client.complete("test prompt")

    assert result == {"completion": "ok"}
    assert mock_post.call_count == 2


@patch("requests.post")
def test_retry_on_500_then_success(mock_post, llm_client):
    """Po błędzie 500 powinno nastąpić retry, które kończy się sukcesem."""
    error_response = Mock()
    error_response.status_code = 500
    error_response.raise_for_status = Mock(side_effect=_make_http_error(500))

    success_response = Mock()
    success_response.status_code = 200
    success_response.raise_for_status = Mock()
    success_response.json.return_value = {"completion": "recovered"}

    mock_post.side_effect = [error_response, success_response]

    result = llm_client.complete("test prompt")

    assert result == {"completion": "recovered"}
    assert mock_post.call_count == 2
