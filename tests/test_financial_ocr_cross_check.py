from io import BytesIO
from unittest.mock import MagicMock, patch

from openpyxl import Workbook

from app import app


def _post_financial_audit(workbook: Workbook, filename: str = "audit.xlsx"):
    payload = BytesIO()
    workbook.save(payload)
    payload.seek(0)
    client = app.test_client()
    return client.post(
        "/api/audit/financial",
        data={"file": (payload, filename), "analysis_type": "full"},
        content_type="multipart/form-data",
    )


def _high_signal_workbook() -> Workbook:
    wb = Workbook()
    low = wb.active
    low.title = "Kontrola"
    low["A1"] = "Kontrola"
    low["A2"] = '=IF(1=1,"OK","BŁĄD")'

    high = wb.create_sheet("Wysoki")
    high["A1"] = "Kwota"
    high["A2"] = "=10"
    high["A3"] = 15
    high["A4"] = "=20"
    return wb


def _evidence_pack():
    audit_response = _post_financial_audit(_high_signal_workbook(), "cross-check.xlsx")
    return audit_response.get_json()["ai_evidence_pack"]


def _fake_qdrant_point(file_name: str, text: str, score: float):
    point = MagicMock()
    point.payload = {"file": file_name, "text": text}
    point.score = score
    return point


@patch("app.get_embedding")
@patch("app.get_qdrant_client")
def test_ocr_cross_check_match(mock_get_client, mock_get_embedding):
    mock_get_embedding.return_value = [0.1, 0.2]
    mock_client = MagicMock()
    mock_client.query_points.return_value.points = [
        _fake_qdrant_point("faktura_001.pdf", "Kwota do zapłaty: 15.00 PLN", 0.9)
    ]
    mock_get_client.return_value = mock_client

    evidence_pack = _evidence_pack()
    client = app.test_client()
    response = client.post("/api/audit/financial/ocr-cross-check", json={"evidence_pack": evidence_pack})
    data = response.get_json()

    assert response.status_code == 200
    assert data["success"] is True
    assert data["cross_check_available"] is True
    assert data["cross_check"]
    statuses = {item["status"] for item in data["cross_check"]}
    assert "match" in statuses
    matched = next(item for item in data["cross_check"] if item["status"] == "match")
    assert matched["matched_ocr_document"] == "faktura_001.pdf"
    assert matched["ocr_amount"] == 15.0


@patch("app.get_embedding")
@patch("app.get_qdrant_client")
def test_ocr_cross_check_mismatch(mock_get_client, mock_get_embedding):
    mock_get_embedding.return_value = [0.1, 0.2]
    mock_client = MagicMock()
    mock_client.query_points.return_value.points = [
        _fake_qdrant_point("faktura_002.pdf", "Kwota do zapłaty: 999.00 PLN", 0.8)
    ]
    mock_get_client.return_value = mock_client

    evidence_pack = _evidence_pack()
    client = app.test_client()
    response = client.post("/api/audit/financial/ocr-cross-check", json={"evidence_pack": evidence_pack})
    data = response.get_json()

    assert response.status_code == 200
    statuses = {item["status"] for item in data["cross_check"]}
    assert "mismatch" in statuses
    mismatched = next(item for item in data["cross_check"] if item["status"] == "mismatch")
    assert mismatched["ocr_amount"] == 999.0
    assert mismatched["excel_amount"] != mismatched["ocr_amount"]


@patch("app.get_embedding")
@patch("app.get_qdrant_client")
def test_ocr_cross_check_not_found(mock_get_client, mock_get_embedding):
    mock_get_embedding.return_value = [0.1, 0.2]
    mock_client = MagicMock()
    mock_client.query_points.return_value.points = []
    mock_get_client.return_value = mock_client

    evidence_pack = _evidence_pack()
    client = app.test_client()
    response = client.post("/api/audit/financial/ocr-cross-check", json={"evidence_pack": evidence_pack})
    data = response.get_json()

    assert response.status_code == 200
    assert all(item["status"] == "not_found" for item in data["cross_check"])


@patch("app.get_qdrant_client")
def test_ocr_cross_check_fail_closed_when_qdrant_unavailable(mock_get_client):
    mock_get_client.side_effect = RuntimeError("connection refused")

    evidence_pack = _evidence_pack()
    client = app.test_client()
    response = client.post("/api/audit/financial/ocr-cross-check", json={"evidence_pack": evidence_pack})
    data = response.get_json()

    assert response.status_code == 200
    assert data["success"] is True
    assert data["cross_check_available"] is True
    assert all(item["status"] == "unavailable" for item in data["cross_check"])


def test_ocr_cross_check_missing_evidence_pack_returns_400():
    client = app.test_client()
    response = client.post("/api/audit/financial/ocr-cross-check", json={})
    data = response.get_json()

    assert response.status_code == 400
    assert data["success"] is False
    assert data["cross_check_available"] is False


def test_financial_audit_endpoint_works_without_ocr_module_being_called():
    response = _post_financial_audit(_high_signal_workbook(), "no-ocr-dependency.xlsx")
    data = response.get_json()

    assert response.status_code == 200
    assert data["success"] is True
    assert "ai_evidence_pack" in data
