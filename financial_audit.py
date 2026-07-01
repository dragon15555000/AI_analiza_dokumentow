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


def _pl_count(value: int, one: str, few: str, many: str) -> str:
    """Prosta polska odmiana liczebników."""
    value = abs(int(value))
    if value == 1:
        return one
    if value % 100 in (12, 13, 14):
        return many
    if value % 10 in (2, 3, 4):
        return few
    return many


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

        for cell_key, cell in self.cells.items():
            cosmetic_reason = self._detect_cosmetic_check(cell.formula)
            if cosmetic_reason:
                self.anomalies.append({
                    "type": "cosmetic_check",
                    "cell": cell_key,
                    "severity": "MEDIUM",
                    "message": f"Formuła kontrolna w {cell_key} wygląda na pozorną: {cosmetic_reason}",
                    "formula": cell.formula,
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

    def _detect_cosmetic_check(self, formula: str) -> Optional[str]:
        """Wykrywa pozorne kontrole: warunki zawsze prawdziwe lub zwracające ten sam wynik."""
        expr = (formula or "").strip()
        if expr.startswith("="):
            expr = expr[1:].strip()
        if not expr:
            return None

        upper_expr = expr.upper()
        if self._condition_is_tautology(upper_expr):
            return "warunek logiczny jest zawsze prawdziwy i nie wnosi realnej kontroli"

        if_match = re.match(r"^(IF|JEŻELI)\((.*)\)$", expr, re.IGNORECASE)
        if not if_match:
            return None

        args = self._split_excel_args(if_match.group(2))
        if len(args) < 3:
            return None

        condition = args[0].strip()
        true_branch = args[1].strip()
        false_branch = args[2].strip()
        normalized_true = self._normalize_formula_token(true_branch)
        normalized_false = self._normalize_formula_token(false_branch)

        if normalized_true and normalized_true == normalized_false:
            return "obie gałęzie zwracają ten sam wynik, więc kontrola niczego nie rozróżnia"

        condition_true = self._condition_is_tautology(condition)
        condition_false = self._condition_is_contradiction(condition)
        true_kind = self._status_branch_kind(true_branch)
        false_kind = self._status_branch_kind(false_branch)

        if condition_true and true_kind == "positive" and false_kind == "negative":
            return "warunek zawsze prowadzi do pozytywnego komunikatu"
        if condition_false and false_kind == "positive" and true_kind == "negative":
            return "warunek nigdy nie przełącza się na negatywny komunikat"

        if condition_true and true_kind != "unknown":
            return "kontrola ma warunek zawsze prawdziwy, więc wynik jest z góry przesądzony"
        if condition_false and false_kind != "unknown":
            return "kontrola ma warunek zawsze fałszywy, więc jedna z gałęzi nigdy nie zostanie użyta"

        return None

    def _condition_is_tautology(self, expr: str) -> bool:
        expr = self._normalize_formula_token(expr)
        if not expr:
            return False

        if expr in {"TRUE", "PRAWDA"}:
            return True
        if self._same_side_comparison(expr, {"=", "<=", ">="}):
            return True

        func = self._extract_function_call(expr)
        if not func:
            return False
        func_name, raw_args = func
        args = self._split_excel_args(raw_args)
        if not args:
            return False
        if func_name in {"AND", "ORAZ"}:
            return all(self._condition_is_tautology(arg) for arg in args)
        if func_name in {"OR", "LUB"}:
            return any(self._condition_is_tautology(arg) for arg in args)
        return False

    def _condition_is_contradiction(self, expr: str) -> bool:
        expr = self._normalize_formula_token(expr)
        if not expr:
            return False

        if expr in {"FALSE", "FAŁSZ"}:
            return True
        if self._same_side_comparison(expr, {"<>", "!="}):
            return True

        func = self._extract_function_call(expr)
        if not func:
            return False
        func_name, raw_args = func
        args = self._split_excel_args(raw_args)
        if not args:
            return False
        if func_name in {"AND", "ORAZ"}:
            return any(self._condition_is_contradiction(arg) for arg in args)
        if func_name in {"OR", "LUB"}:
            return all(self._condition_is_contradiction(arg) for arg in args)
        return False

    def _same_side_comparison(self, expr: str, operators: Set[str]) -> bool:
        for op in ("<=", ">=", "<>", "!=", "="):
            if op not in operators:
                continue
            parts = expr.split(op)
            if len(parts) != 2:
                continue
            left = self._normalize_formula_token(parts[0])
            right = self._normalize_formula_token(parts[1])
            if left and right and left == right:
                return True
        return False

    def _extract_function_call(self, expr: str) -> Optional[tuple[str, str]]:
        match = re.match(r"^([A-ZĄĆĘŁŃÓŚŹŻ]+)\((.*)\)$", expr, re.IGNORECASE)
        if not match:
            return None
        return match.group(1).upper(), match.group(2)

    def _split_excel_args(self, args_text: str) -> List[str]:
        args: List[str] = []
        current: List[str] = []
        depth = 0
        in_string = False
        i = 0
        while i < len(args_text):
            ch = args_text[i]
            if ch == '"':
                in_string = not in_string
                current.append(ch)
            elif not in_string and ch == "(":
                depth += 1
                current.append(ch)
            elif not in_string and ch == ")":
                depth = max(0, depth - 1)
                current.append(ch)
            elif not in_string and depth == 0 and ch in {",", ";"}:
                args.append("".join(current).strip())
                current = []
            else:
                current.append(ch)
            i += 1
        tail = "".join(current).strip()
        if tail:
            args.append(tail)
        return args

    def _normalize_formula_token(self, token: str) -> str:
        token = (token or "").strip().upper()
        token = token.replace("$", "")
        token = re.sub(r"\s+", "", token)
        return token

    def _status_branch_kind(self, branch: str) -> str:
        normalized = self._normalize_formula_token(branch).strip('"')
        positive_markers = ("OK", "ZGOD", "POPRAW", "PASS", "TRUE", "PRAWDA")
        negative_markers = ("BŁĄD", "BLAD", "ERROR", "FAIL", "NIEZGOD", "FALSE", "FAŁSZ")
        if any(marker in normalized for marker in positive_markers):
            return "positive"
        if any(marker in normalized for marker in negative_markers):
            return "negative"
        return "unknown"

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
            "risk_level": self._calculate_risk_level(),
            "logic_summary": self.get_logic_summary(),
        }

    def get_logic_summary(self) -> Dict:
        """Opisuje logikę arkusza: wejścia, wyjścia, węzły centralne i złożoność."""
        formula_cells = list(self.cells.values())
        if not formula_cells:
            return {
                "input_like_cells": 0,
                "output_like_cells": 0,
                "cross_sheet_references": 0,
                "max_dependency_depth": 0,
                "hub_cells": [],
                "output_cells": [],
                "description": "Brak formuł do analizy logiki arkusza.",
            }

        input_like_cells = []
        output_cells = []
        cross_sheet_references = 0

        for cell in formula_cells:
            internal_deps = [dep for dep in cell.dependencies if str(dep) in self.cells]
            if not internal_deps:
                input_like_cells.append(str(cell.cell_ref))
            if not cell.dependents:
                output_cells.append(str(cell.cell_ref))
            cross_sheet_references += sum(
                1 for dep in cell.dependencies if dep.sheet and dep.sheet != cell.cell_ref.sheet
            )

        hub_cells = sorted(
            formula_cells,
            key=lambda cell: (len(cell.dependents), len(cell.dependencies)),
            reverse=True,
        )[:5]
        max_depth = max(self._dependency_depth(cell.cell_ref) for cell in formula_cells)

        description_parts = []
        if output_cells:
            description_parts.append(
                f"Arkusz buduje {len(output_cells)} {_pl_count(len(output_cells), 'komórkę wynikową', 'komórki wynikowe', 'komórek wynikowych')}, na {_pl_count(len(output_cells), 'której', 'których', 'których')} kończą się obliczenia."
            )
        if input_like_cells:
            description_parts.append(
                f"{len(input_like_cells)} {_pl_count(len(input_like_cells), 'formuła działa', 'formuły działają', 'formuł działa')} jak punkty wejścia lub proste przeliczenia bazowe."
            )
        if hub_cells and len(hub_cells[0].dependents) > 0:
            description_parts.append(
                f"Najbardziej centralna logika skupia się wokół {hub_cells[0].cell_ref}, od której zależy {len(hub_cells[0].dependents)} {_pl_count(len(hub_cells[0].dependents), 'kolejne obliczenie', 'kolejne obliczenia', 'kolejnych obliczeń')}."
            )
        if cross_sheet_references:
            description_parts.append(
                f"Wykryto {cross_sheet_references} {_pl_count(cross_sheet_references, 'odwołanie', 'odwołania', 'odwołań')} między arkuszami, więc logika jest rozproszona."
            )
        if max_depth >= 4:
            description_parts.append(
                f"Łańcuch zależności jest głęboki (maksymalnie {max_depth} {_pl_count(max_depth, 'poziom', 'poziomy', 'poziomów')}), co utrudnia ręczne śledzenie wyników."
            )

        return {
            "input_like_cells": len(input_like_cells),
            "output_like_cells": len(output_cells),
            "cross_sheet_references": cross_sheet_references,
            "max_dependency_depth": max_depth,
            "hub_cells": [
                {
                    "cell": str(cell.cell_ref),
                    "dependents_count": len(cell.dependents),
                    "dependencies_count": len(cell.dependencies),
                    "formula": cell.formula,
                }
                for cell in hub_cells
            ],
            "output_cells": output_cells[:8],
            "description": " ".join(description_parts)
            or "Logika arkusza jest prosta i nie wykazuje złożonych łańcuchów zależności.",
        }

    def _dependency_depth(self, cell_ref: CellRef, visited: Optional[Set[str]] = None) -> int:
        """Szacuje maksymalną głębokość zależności dla komórki."""
        visited = visited or set()
        cell_key = str(cell_ref)
        if cell_key in visited:
            return 1
        visited = set(visited)
        visited.add(cell_key)
        cell = self.cells.get(cell_key)
        if not cell:
            return 1
        internal_deps = [dep for dep in cell.dependencies if str(dep) in self.cells]
        if not internal_deps:
            return 1
        return 1 + max(self._dependency_depth(dep, visited) for dep in internal_deps)

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
