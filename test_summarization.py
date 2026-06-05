# -*- coding: utf-8 -*-
"""
Unit testy dla auto-streszczenia i adaptacyjnych promptów.
"""

import unittest
from unittest.mock import MagicMock, patch
from app import _get_collection_profile_cached, _generate_file_summary_helper, _COLLECTION_PROFILE_CACHE

class TestSummarizationAndAdaptiveContext(unittest.TestCase):
    def setUp(self):
        _COLLECTION_PROFILE_CACHE.clear()

    @patch('app.get_qdrant_client')
    def test_get_collection_profile_cached_financial(self, mock_get_client):
        # Symuluj rekordy zawierające głównie pliki xlsx / csv
        # Chcemy top_ratio > 0.5, więc weźmy 2 xlsx i 1 csv (top_ratio = 2/3 = 0.67)
        mock_client = MagicMock()
        mock_record_1 = MagicMock()
        mock_record_1.payload = {"file": "finanse_2026.xlsx"}
        mock_record_2 = MagicMock()
        mock_record_2.payload = {"file": "raport.csv"}
        mock_record_3 = MagicMock()
        mock_record_3.payload = {"file": "bilans.xlsx"}
        
        mock_client.scroll.return_value = ([mock_record_1, mock_record_2, mock_record_3], None)
        mock_get_client.return_value = mock_client
        
        profile = _get_collection_profile_cached("test_financial_coll")
        self.assertEqual(profile, "financial")
        
        # Sprawdź czy pobrało z cache za drugim razem (scroll nie powinien być wywołany)
        mock_client.scroll.reset_mock()
        profile_cached = _get_collection_profile_cached("test_financial_coll")
        self.assertEqual(profile_cached, "financial")
        mock_client.scroll.assert_not_called()

    @patch('app.get_qdrant_client')
    def test_get_collection_profile_cached_legal(self, mock_get_client):
        # Symuluj rekordy zawierające dokumenty prawne / pdf
        # Chcemy top_ratio > 0.4, więc weźmy 2 pdf i 1 docx
        mock_client = MagicMock()
        mock_record_1 = MagicMock()
        mock_record_1.payload = {"file": "umowa_ramowa.pdf"}
        mock_record_2 = MagicMock()
        mock_record_2.payload = {"file": "decyzja.docx"}
        mock_record_3 = MagicMock()
        mock_record_3.payload = {"file": "statut.pdf"}
        
        mock_client.scroll.return_value = ([mock_record_1, mock_record_2, mock_record_3], None)
        mock_get_client.return_value = mock_client
        
        profile = _get_collection_profile_cached("test_legal_coll")
        self.assertEqual(profile, "legal")

    @patch('app.call_llm')
    @patch('app._llm_response_text')
    def test_generate_file_summary_helper(self, mock_response_text, mock_call_llm):
        mock_call_llm.return_value = {"choices": [{"message": {"content": "To jest streszczenie pliku testowego."}}]}
        mock_response_text.return_value = "To jest streszczenie pliku testowego."
        
        text = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua."
        summary = _generate_file_summary_helper(text, "test.pdf")
        self.assertEqual(summary, "To jest streszczenie pliku testowego.")
        mock_call_llm.assert_called_once()

if __name__ == '__main__':
    unittest.main()
