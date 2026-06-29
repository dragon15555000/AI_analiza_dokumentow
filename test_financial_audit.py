"""Testy modułu financial_audit."""

import unittest
from financial_audit import CellRef, FormulaParser, AuditAnalyzer


class TestFormulaParser(unittest.TestCase):
    """Testy parsera formuł."""

    def test_parse_simple_cell_ref(self):
        """Parsuj prostą referencję A1."""
        refs = FormulaParser.parse_cell_refs("=A1+B2")
        cell_strs = {str(r) for r in refs}
        self.assertIn("!A1", "".join(cell_strs))

    def test_parse_multiple_refs(self):
        """Parsuj wiele referencji."""
        refs = FormulaParser.parse_cell_refs("=SUM(A1:A10)")
        self.assertGreater(len(refs), 0)

    def test_parse_sheet_ref(self):
        """Parsuj referencję z arkuszem."""
        refs = FormulaParser.parse_cell_refs("=Sheet1!A1+Sheet2!B2")
        self.assertGreater(len(refs), 0)


class TestAuditAnalyzer(unittest.TestCase):
    """Testy analizatora audytu."""

    def setUp(self):
        self.analyzer = AuditAnalyzer()

    def test_add_cell(self):
        """Dodaj komórkę do analizy."""
        ref = CellRef("Sheet1", "A", 1)
        self.analyzer.add_cell(ref, "=5+3", value=8)
        self.assertEqual(len(self.analyzer.cells), 1)
        self.assertEqual(self.analyzer.cells["Sheet1!A1"].value, 8)

    def test_dependency_graph(self):
        """Buduj graf zależności."""
        ref_a = CellRef("Sheet1", "A", 1)
        ref_b = CellRef("Sheet1", "B", 1)

        self.analyzer.add_cell(ref_a, "=5", value=5)
        self.analyzer.add_cell(ref_b, "=A1+10", value=15)
        self.analyzer.build_dependency_graph()

        # B1 zależy od A1, więc A1 powinno mieć B1 w dependents
        self.assertIn(ref_b, self.analyzer.cells["Sheet1!A1"].dependents)

    def test_hidden_formula_anomaly(self):
        """Detektuj ukrytą formułę."""
        ref = CellRef("Sheet1", "A", 1)
        self.analyzer.add_cell(ref, "=SECRET_CALC()", value=999)
        self.analyzer.mark_hidden(ref)
        self.analyzer.detect_anomalies()

        anomalies = [a for a in self.analyzer.anomalies if a["type"] == "hidden_formula"]
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["severity"], "MEDIUM")

    def test_get_summary(self):
        """Podsumowanie audytu."""
        ref = CellRef("Sheet1", "A", 1)
        self.analyzer.add_cell(ref, "=100", value=100)
        self.analyzer.detect_anomalies()

        summary = self.analyzer.get_summary()
        self.assertEqual(summary["total_cells"], 1)
        self.assertEqual(summary["total_formulas"], 1)
        self.assertEqual(summary["risk_level"], "LOW")

    def test_html_output(self):
        """Wygeneruj HTML."""
        ref = CellRef("Sheet1", "A", 1)
        self.analyzer.add_cell(ref, "=50", value=50)
        html = self.analyzer.to_html_graph()

        self.assertIn("<html>", html)
        self.assertIn("Sheet1!A1", html)
        self.assertIn("=50", html)


if __name__ == "__main__":
    unittest.main()
