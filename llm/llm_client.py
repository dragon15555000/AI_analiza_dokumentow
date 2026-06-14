import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from retry_utils import retry_api_call


class LLMClient:
    def __init__(self):
        self.base_url = os.environ["NEW_API_BASE_URL"]
        self.api_key = os.environ["NEW_API_KEY"]

    @retry_api_call(
        attempts=5,
        max_delay=10.0,
        retry_statuses={429, 500, 502, 503, 504},
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
