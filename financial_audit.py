"""
Moduł audytu finansowego — analiza formuł arkuszy i detekcja nadużyć.
Funkcjonalność:
- Parser formuł Excel → dependency graph
- Detekcja anomalii (circular refs, manipulacje)
- Wizualizacja logiki arkusza
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional
from collections import deque


@dataclass(frozen=True)
class CellRef:
    """Odniesienie do komórki arkusza."""
    sheet: str
    col: str
    row: int

    def __repr__(self):
        return f"{self.sheet}!{self.col}{self.row}"


@dataclass
class FormulaCell:
    """Komórka z formułą."""
    cell_ref: CellRef
    formula: str
    value: Optional[float] = None
    dependencies: Set[CellRef] = field(default_factory=set)
    dependents: Set[CellRef] = field(default_factory=set)


class FormulaParser:
    """Parser formuł Excel — ekstraktuj referencje do komórek."""

    CELL_PATTERN = re.compile(
        r"(?P<sheet>[A-Za-z0-9_\-']+)?!?"
        r"\$?(?P<col>[A-Z]+)\$?(?P<row>\d+)"
    )

    @staticmethod
    def parse_cell_refs(formula: str, default_sheet: str = "") -> Set[CellRef]:
        """Ekstraktuj wszystkie referencje komórek z formuły."""
        refs = set()
        for match in FormulaParser.CELL_PATTERN.finditer(formula):
            sheet = match.group("sheet") or default_sheet
            col = match.group("col")
            row = int(match.group("row"))
            refs.add(CellRef(sheet=sheet, col=col, row=row))
        return refs


class AuditAnalyzer:
    """Analizator formuł — buduje graf zależności, detektuje anomalie."""

    def __init__(self):
        self.cells: Dict[str, FormulaCell] = {}
        self.anomalies: List[Dict] = []
        self.hidden_cells: Set[str] = set()

    def add_cell(self, cell_ref: CellRef, formula: str, value: Optional[float] = None):
        """Dodaj komórkę do analizy."""
        key = str(cell_ref)
        deps = FormulaParser.parse_cell_refs(formula, default_sheet=cell_ref.sheet)
        self.cells[key] = FormulaCell(
            cell_ref=cell_ref, formula=formula, value=value, dependencies=deps
        )

    def mark_hidden(self, cell_ref: CellRef):
        """Oznacz komórkę jako ukrytą."""
        self.hidden_cells.add(str(cell_ref))

    def build_dependency_graph(self):
        """Buduj graf zależności."""
        for cell_key, cell in self.cells.items():
            for dep in cell.dependencies:
                dep_key = str(dep)
                if dep_key in self.cells:
                    self.cells[dep_key].dependents.add(cell.cell_ref)

    def detect_anomalies(self):
        """Detektuj anomalie — circular refs, ukryte formuły."""
        self.anomalies = []

        for cell_key, cell in self.cells.items():
            if self._has_circular_ref(cell):
                self.anomalies.append({
                    "type": "circular_reference",
                    "cell": cell_key,
                    "severity": "HIGH",
                    "message": f"Zależność cykliczna w {cell_key}"
                })

        for cell_key in self.hidden_cells:
            if cell_key in self.cells and self.cells[cell_key].formula:
                self.anomalies.append({
                    "type": "hidden_formula",
                    "cell": cell_key,
                    "severity": "MEDIUM",
                    "message": f"Ukryta komórka z formułą: {cell_key}",
                    "formula": self.cells[cell_key].formula
                })

        for cell_key, cell in self.cells.items():
            if "http" in cell.formula.lower() or "external" in cell.formula.lower():
                self.anomalies.append({
                    "type": "external_reference",
                    "cell": cell_key,
                    "severity": "MEDIUM",
                    "message": f"Odniesienie zewnętrzne w {cell_key}",
                    "formula": cell.formula
                })

    def _has_circular_ref(self, cell: FormulaCell) -> bool:
        """DFS — sprawdź cykl."""
        visited = set()
        stack = deque([cell.cell_ref])

        while stack:
            current = stack.popleft()
            current_key = str(current)

            if current_key in visited:
                return True

            visited.add(current_key)

            if current_key in self.cells:
                for dep in self.cells[current_key].dependencies:
                    if str(dep) == str(cell.cell_ref):
                        return True
                    stack.append(dep)

        return False

    def get_formula_chain(self, cell_ref: CellRef) -> Dict:
        """Pokaż łańcuch formuł."""
        cell_key = str(cell_ref)
        if cell_key not in self.cells:
            return {"error": "Komórka nie znaleziona"}

        chain = {
            "cell": cell_key,
            "formula": self.cells[cell_key].formula,
            "value": self.cells[cell_key].value,
            "dependencies": [],
            "dependents": []
        }

        for dep in list(self.cells[cell_key].dependencies)[:5]:
            dep_key = str(dep)
            if dep_key in self.cells:
                chain["dependencies"].append({
                    "cell": dep_key,
                    "formula": self.cells[dep_key].formula,
                    "value": self.cells[dep_key].value
                })

        for dependent in list(self.cells[cell_key].dependents)[:5]:
            dep_key = str(dependent)
            if dep_key in self.cells:
                chain["dependents"].append({
                    "cell": dep_key,
                    "formula": self.cells[dep_key].formula,
                    "value": self.cells[dep_key].value
                })

        return chain

    def get_summary(self) -> Dict:
        """Podsumowanie audytu."""
        return {
            "total_cells": len(self.cells),
            "total_formulas": sum(1 for c in self.cells.values() if c.formula),
            "hidden_cells": len(self.hidden_cells),
            "anomalies_count": len(self.anomalies),
            "anomalies": self.anomalies,
            "risk_level": self._calculate_risk_level()
        }

    def _calculate_risk_level(self) -> str:
        """Oblicz poziom ryzyka."""
        high_count = sum(1 for a in self.anomalies if a.get("severity") == "HIGH")
        medium_count = sum(1 for a in self.anomalies if a.get("severity") == "MEDIUM")

        if high_count > 0:
            return "CRITICAL"
        if medium_count >= 3:
            return "HIGH"
        if medium_count > 0:
            return "MEDIUM"
        return "LOW"

    def to_html_graph(self) -> str:
        """Wygeneruj HTML graph formuł."""
        html = """<html><head><title>Analiza Formuł</title><style>
body { font-family: monospace; margin: 20px; }
.anomaly { background: #fee; padding: 10px; margin: 5px 0; border-left: 4px solid #f00; }
.anomaly.medium { border-left-color: #f90; }
.high { color: red; }
table { border-collapse: collapse; width: 100%; }
td, th { border: 1px solid #ccc; padding: 8px; text-align: left; }
</style></head><body><h1>Analiza Finansowa — Formuły Arkusza</h1>"""

        summary = self.get_summary()
        risk_class = "high" if summary['risk_level'] in ['HIGH', 'CRITICAL'] else ""
        html += f"""<h2>Podsumowanie</h2>
<p><strong>Poziom ryzyka:</strong> <span class="{risk_class}">{summary['risk_level']}</span></p>
<p>Komórki: {summary['total_cells']} | Formuły: {summary['total_formulas']} | Anomalie: {summary['anomalies_count']}</p>"""

        if summary["anomalies"]:
            html += "<h2>Anomalie Wykryte</h2>"
            for anomaly in summary["anomalies"]:
                severity_class = "high" if anomaly.get("severity") == "HIGH" else "medium"
                html += f"""<div class="anomaly {severity_class}"><strong>{anomaly['type']}</strong> [{anomaly['severity']}]<br>
{anomaly['message']}<br><span class="high">{anomaly.get('cell', 'N/A')}</span>"""
                if anomaly.get('formula'):
                    html += f"<br>Formuła: <code>{anomaly.get('formula')}</code>"
                html += "</div>"

        html += "<h2>Formułach Arkusza</h2><table><tr><th>Komórka</th><th>Formuła</th><th>Wartość</th></tr>"
        for cell_key, cell in sorted(self.cells.items()):
            if cell.formula:
                html += f"<tr><td>{cell_key}</td><td><code>{cell.formula}</code></td><td>{cell.value}</td></tr>"
        html += "</table></body></html>"

        return html
