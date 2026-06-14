import os

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential


def _is_retryable_http_error(exc: BaseException) -> bool:
    """Zwraca True dla błędów HTTP 429 i 5xx."""
    if isinstance(exc, requests.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return status == 429 or (status is not None and status >= 500)
    return False


class LLMClient:
    def __init__(self):
        self.base_url = os.environ["NEW_API_BASE_URL"]
        self.api_key = os.environ["NEW_API_KEY"]

    @retry(
        retry=retry_if_exception(_is_retryable_http_error),
        wait=wait_exponential(multiplier=1, min=1, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def complete(self, prompt):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        response = requests.post(
            f"{self.base_url}/complete",
            headers=headers,
            json={"prompt": prompt},
        )
        response.raise_for_status()
        return response.json()
