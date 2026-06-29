"""
Testy hardening fallback do Ollama w llm_client.py
"""

import unittest
from unittest.mock import patch, MagicMock
import llm_client


class TestOpenRouterFallbackHardening(unittest.TestCase):
    """Testy guardu fallback OpenRouter → Ollama"""

    def setUp(self):
        """Przywróć domyślne wartości LLM_MODEL."""
        self.original_llm_model = llm_client.LLM_MODEL
        self.original_ollama_url = llm_client.OLLAMA_URL

    def tearDown(self):
        """Przywróć zmienne."""
        llm_client.LLM_MODEL = self.original_llm_model
        llm_client.OLLAMA_URL = self.original_ollama_url

    def test_fallback_fails_when_llm_model_not_set(self):
        """Fallback powinien rzucić błąd gdy LLM_MODEL jest pusty."""
        llm_client.LLM_MODEL = None

        with self.assertRaises(RuntimeError) as ctx:
            llm_client._fallback_openrouter_to_ollama(
                prompt="test prompt",
                system="test system",
                stream=False,
                reason="test reason"
            )

        self.assertIn("LLM_MODEL", str(ctx.exception))
        self.assertIn("nie jest ustawiony", str(ctx.exception))

    def test_fallback_fails_when_ollama_offline(self):
        """Fallback powinien rzucić błąd gdy Ollama jest offline."""
        llm_client.LLM_MODEL = "llama2"

        with patch.object(llm_client, '_check_ollama_health') as mock_health:
            mock_health.return_value = {
                "ok": False,
                "url": "http://localhost:11434",
                "error": "Connection refused"
            }

            with self.assertRaises(RuntimeError) as ctx:
                llm_client._fallback_openrouter_to_ollama(
                    prompt="test prompt",
                    system="test system",
                    stream=False,
                    reason="test reason"
                )

            self.assertIn("niedostępna", str(ctx.exception))
            self.assertIn("Connection refused", str(ctx.exception))

    def test_fallback_succeeds_with_valid_config(self):
        """Fallback powinien się wykonać gdy Ollama jest dostępna."""
        llm_client.LLM_MODEL = "llama2"

        with patch.object(llm_client, '_check_ollama_health') as mock_health, \
             patch.object(llm_client, '_call_ollama') as mock_call:

            mock_health.return_value = {
                "ok": True,
                "url": "http://localhost:11434",
                "models_available": 1,
                "has_llm": True,
                "error": None
            }
            mock_call.return_value = {"response": "test response"}

            result = llm_client._fallback_openrouter_to_ollama(
                prompt="test prompt",
                system="test system",
                stream=False,
                reason="test reason"
            )

            self.assertEqual(result, {"response": "test response"})
            mock_call.assert_called_once()

    def test_fallback_stream_fails_when_llm_model_not_set(self):
        """Fallback streaming powinien rzucić błąd gdy LLM_MODEL jest pusty."""
        llm_client.LLM_MODEL = None

        with self.assertRaises(RuntimeError) as ctx:
            llm_client._fallback_openrouter_to_ollama(
                prompt="test prompt",
                system="test system",
                stream=True,
                reason="test reason"
            )

        self.assertIn("LLM_MODEL", str(ctx.exception))


class TestProviderPoolGuard(unittest.TestCase):
    """Testy guardu _load_provider_pool"""

    def test_load_provider_pool_requires_callback(self):
        """_load_provider_pool powinien rzucić błąd gdy callback nie jest ustawiony."""
        # Ustawić callback na None aby symulować niezainicjalizowany stan
        original_func = llm_client._load_provider_pool_func
        llm_client._load_provider_pool_func = None

        try:
            with self.assertRaises(RuntimeError) as ctx:
                llm_client._load_provider_pool()

            self.assertIn("_load_provider_pool", str(ctx.exception))
            self.assertIn("nie został ustawiony", str(ctx.exception))
        finally:
            llm_client._load_provider_pool_func = original_func

    def test_load_provider_pool_calls_callback(self):
        """_load_provider_pool powinien wywołać callback gdy jest ustawiony."""
        mock_func = MagicMock(return_value={"openrouter_keys": []})
        original_func = llm_client._load_provider_pool_func
        llm_client._load_provider_pool_func = mock_func

        try:
            result = llm_client._load_provider_pool()

            mock_func.assert_called_once()
            self.assertEqual(result, {"openrouter_keys": []})
        finally:
            llm_client._load_provider_pool_func = original_func


if __name__ == "__main__":
    unittest.main()
