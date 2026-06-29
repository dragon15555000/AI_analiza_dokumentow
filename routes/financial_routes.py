"""
Routes dla audytu finansowego — analiza formuł arkuszy.
"""

from flask import Blueprint, request, jsonify
from pathlib import Path
import tempfile
import logging
from financial_audit import AuditAnalyzer, CellRef

logger = logging.getLogger("ai_analiza")

financial_bp = Blueprint("financial", __name__, url_prefix="/api/audit")


def _extract_formulas_from_excel(file_path: Path) -> dict:
    """Ekstraktuj formuły z pliku Excel."""
    try:
        import openpyxl
    except ImportError:
        return {"error": "openpyxl nie zainstalowany"}

    formulas_by_sheet = {}

    try:
        wb = openpyxl.load_workbook(file_path, data_only=False)

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            sheet_formulas = []

            for row in ws.iter_rows():
                for cell in row:
                    if cell.value and isinstance(cell.value, str) and cell.value.startswith("="):
                        sheet_formulas.append({
                            "cell": f"{cell.column_letter}{cell.row}",
                            "formula": cell.value,
                            "value": None
                        })

            if sheet_formulas:
                formulas_by_sheet[sheet_name] = sheet_formulas

        return {"success": True, "sheets": formulas_by_sheet}

    except Exception as e:
        logger.error(f"Błąd parsowania Excel: {e}")
        return {"error": str(e)[:200]}


@financial_bp.route("/financial", methods=["POST"])
def audit_financial():
    """
    POST /api/audit/financial
    Upload dokumentu i analiza formuł arkusza.
    """

    if "file" not in request.files:
        return jsonify({"error": "Brak pliku"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Nazwa pliku pusta"}), 400

    analysis_type = request.form.get("analysis_type", "full")

    with tempfile.NamedTemporaryFile(suffix=Path(file.filename).suffix, delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = Path(tmp.name)

    try:
        formulas_result = _extract_formulas_from_excel(tmp_path)

        if "error" in formulas_result:
            return jsonify(formulas_result), 400

        result = {
            "status": "success",
            "file_name": file.filename,
            "sheets": {},
            "summary": {
                "total_sheets": len(formulas_result.get("sheets", {})),
                "total_formulas": 0,
                "risk_level": "LOW",
                "anomalies_count": 0
            }
        }

        all_anomalies = []

        for sheet_name, formulas in formulas_result.get("sheets", {}).items():
            analyzer = AuditAnalyzer()

            for formula_info in formulas:
                cell_ref = CellRef(
                    sheet=sheet_name,
                    col="".join([c for c in formula_info["cell"] if c.isalpha()]),
                    row=int("".join([c for c in formula_info["cell"] if c.isdigit()]))
                )
                analyzer.add_cell(
                    cell_ref,
                    formula_info["formula"],
                    value=formula_info.get("value")
                )

            analyzer.build_dependency_graph()
            analyzer.detect_anomalies()

            sheet_data = {
                "formula_count": len(formulas),
                "cell_count": len(analyzer.cells),
                "anomalies": analyzer.anomalies,
                "risk_level": analyzer._calculate_risk_level()
            }

            if analysis_type in ["full", "formulas"]:
                sheet_data["formulas"] = [
                    {
                        "cell": str(cell.cell_ref),
                        "formula": cell.formula,
                        "dependencies": [str(d) for d in cell.dependencies]
                    }
                    for cell in analyzer.cells.values()
                ]

            result["sheets"][sheet_name] = sheet_data
            result["summary"]["total_formulas"] += len(formulas)
            all_anomalies.extend(analyzer.anomalies)

        high_count = sum(1 for a in all_anomalies if a.get("severity") == "HIGH")
        medium_count = sum(1 for a in all_anomalies if a.get("severity") == "MEDIUM")

        if high_count > 0:
            result["summary"]["risk_level"] = "CRITICAL"
        elif medium_count >= 3:
            result["summary"]["risk_level"] = "HIGH"
        elif all_anomalies:
            result["summary"]["risk_level"] = "MEDIUM"

        result["summary"]["anomalies_count"] = len(all_anomalies)

        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Błąd audytu: {e}", exc_info=True)
        return jsonify({"error": f"Błąd: {str(e)[:200]}"}), 500

    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass
