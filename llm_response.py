"""Unified LLM response type."""

from typing import Any


class LLMResponse:
    __slots__ = ("text", "input_tokens", "output_tokens", "raw")

    def __init__(self, text: str, input_tokens, output_tokens, raw: Any = None) -> None:
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.raw = raw
