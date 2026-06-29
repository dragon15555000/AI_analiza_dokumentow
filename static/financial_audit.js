/**
 * Financial Audit Frontend — analiza formuł arkuszy Excel
 */

let currentFinancialReport = null;

async function uploadFinancialFile(event) {
    const file = event.target.files[0];
    if (!file) return;

    const statusDiv = document.getElementById('financialUploadStatus');
    statusDiv.style.display = 'block';
    statusDiv.innerHTML = '<div class="alert alert-info"><span class="spinner-border spinner-border-sm me-2"></span>Wczytywanie...</div>';

    const formData = new FormData();
    formData.append('file', file);
    formData.append('analysis_type', 'full');

    try {
        const response = await fetch('/api/audit/financial', {
            method: 'POST',
            body: formData
        });

        if (!response.ok) {
            const error = await response.json();
            statusDiv.innerHTML = `<div class="alert alert-danger">❌ Błąd: ${error.error || 'Nieznany błąd'}</div>`;
            return;
        }

        const report = await response.json();
        currentFinancialReport = report;

        statusDiv.innerHTML = '<div class="alert alert-success">✓ Analiza zakończona pomyślnie</div>';
        displayFinancialResults(report);

    } catch (error) {
        statusDiv.innerHTML = `<div class="alert alert-danger">❌ Błąd sieciowy: ${error.message}</div>`;
    }
}

function displayFinancialResults(report) {
    const resultsDiv = document.getElementById('financialResults');
    resultsDiv.style.display = 'block';

    // Podsumowanie
    const summary = report.summary;
    const riskColor = {
        'LOW': '#16a34a',
        'MEDIUM': '#d97706',
        'HIGH': '#dc2626',
        'CRITICAL': '#7c2d12'
    };

    const summaryHtml = `
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:15px;">
            <div style="background:var(--c-surface);padding:15px;border-radius:var(--r-md);border-left:4px solid #4f46e5;">
                <div class="text-muted small">Arkuszy</div>
                <div style="font-size:1.5rem;font-weight:700;">${summary.total_sheets}</div>
            </div>
            <div style="background:var(--c-surface);padding:15px;border-radius:var(--r-md);border-left:4px solid #16a34a;">
                <div class="text-muted small">Formuł</div>
                <div style="font-size:1.5rem;font-weight:700;">${summary.total_formulas}</div>
            </div>
            <div style="background:var(--c-surface);padding:15px;border-radius:var(--r-md);border-left:4px solid #d97706;">
                <div class="text-muted small">Anomalii</div>
                <div style="font-size:1.5rem;font-weight:700;">${summary.anomalies_count}</div>
            </div>
            <div style="background:var(--c-surface);padding:15px;border-radius:var(--r-md);border-left:4px solid;"
                 style="border-left-color:${riskColor[summary.risk_level]};">
                <div class="text-muted small">Poziom Ryzyka</div>
                <div style="font-size:1.5rem;font-weight:700;color:${riskColor[summary.risk_level]};">${summary.risk_level}</div>
            </div>
        </div>
    `;
    document.getElementById('financialSummary').innerHTML = summaryHtml;

    // Anomalie
    if (summary.anomalies_count > 0) {
        let anomaliesHtml = '<h6 class="mb-3">⚠️ Wykryte Anomalie:</h6>';
        anomaliesHtml += '<div style="max-height:300px;overflow-y:auto;">';

        report.summary.anomalies = report.summary.anomalies || [];
        for (const anomaly of report.summary.anomalies.slice(0, 20)) {
            const severityBg = anomaly.severity === 'HIGH' ? '#fee' : '#fef3c7';
            const severityColor = anomaly.severity === 'HIGH' ? '#dc2626' : '#d97706';
            anomaliesHtml += `
                <div style="background:${severityBg};padding:10px;margin-bottom:8px;border-left:4px solid ${severityColor};border-radius:4px;">
                    <strong style="color:${severityColor};">${anomaly.type}</strong> <span class="badge" style="background:${severityColor};">${anomaly.severity}</span><br>
                    <small>${anomaly.message}</small>
                    ${anomaly.formula ? `<br><code style="font-size:0.75rem;background:#f1f5f9;padding:4px;border-radius:2px;display:inline-block;margin-top:4px;">${anomaly.formula}</code>` : ''}
                </div>
            `;
        }
        anomaliesHtml += '</div>';
        document.getElementById('financialAnomalies').innerHTML = anomaliesHtml;
    } else {
        document.getElementById('financialAnomalies').innerHTML = '<div class="alert alert-success">✓ Brak anomalii!</div>';
    }

    // Arkusze
    let sheetsHtml = '<h6 class="mb-3">📋 Arkusze:</h6>';
    for (const [sheetName, sheetData] of Object.entries(report.sheets)) {
        sheetsHtml += `
            <div style="background:var(--c-surface);padding:12px;border-radius:var(--r-md);margin-bottom:12px;">
                <strong>${sheetName}</strong>
                <div class="small text-muted mt-1">
                    Formuł: ${sheetData.formula_count} |
                    Komórek: ${sheetData.cell_count} |
                    Anomalii: ${sheetData.anomalies.length} |
                    Ryzyko: <span style="color:${riskColor[sheetData.risk_level]};font-weight:600;">${sheetData.risk_level}</span>
                </div>
            </div>
        `;
    }
    document.getElementById('financialSheets').innerHTML = sheetsHtml;
}

function downloadFinancialReport() {
    if (!currentFinancialReport) {
        alert('Brak raportu do pobrania');
        return;
    }

    const json = JSON.stringify(currentFinancialReport, null, 2);
    const blob = new Blob([json], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `financial_audit_${new Date().toISOString().split('T')[0]}.json`;
    a.click();
    URL.revokeObjectURL(url);
}
