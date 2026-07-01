/**
 * Financial Audit Frontend — analiza formuł arkuszy Excel
 */

let currentFinancialReport = null;
let currentFinancialFile = null;
let currentFinancialFileToken = '';
let currentFinancialReportToken = '';

function financialEscape(value) {
    if (value == null) return '';
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function financialRiskLabel(level) {
    return {
        LOW: 'Niskie ryzyko',
        MEDIUM: 'Umiarkowane ryzyko',
        HIGH: 'Wysokie ryzyko',
        CRITICAL: 'Krytyczne ryzyko'
    }[level] || level || 'Brak oceny';
}

function financialSheetPriorityLabel(level) {
    return {
        LOW: 'niski',
        MEDIUM: 'umiarkowany',
        HIGH: 'wysoki',
        CRITICAL: 'krytyczny'
    }[level] || level || 'brak';
}

function financialFlowColor(kind) {
    return {
        output: { fill: '#fee2e2', stroke: '#dc2626', text: '#7f1d1d' },
        anomaly: { fill: '#fff7ed', stroke: '#ea580c', text: '#9a3412' },
        hub: { fill: '#dbeafe', stroke: '#2563eb', text: '#1e3a8a' },
        formula: { fill: '#e2e8f0', stroke: '#475569', text: '#0f172a' },
        input: { fill: '#dcfce7', stroke: '#16a34a', text: '#166534' },
        external: { fill: '#fef3c7', stroke: '#d97706', text: '#92400e' }
    }[kind] || { fill: '#f8fafc', stroke: '#64748b', text: '#0f172a' };
}

function financialAnalysisModeLabel(mode) {
    return {
        quick: 'szybki',
        targeted: 'normalny',
        full: 'dokładny'
    }[mode] || mode || 'nieznany';
}

function financialModeRank(mode) {
    return {
        quick: 1,
        targeted: 2,
        full: 3
    }[mode] || 0;
}

function financialBuildFileToken(file) {
    if (!file) return '';
    return [file.name || '', file.size || 0, file.lastModified || 0].join('::');
}

function setFinancialStatus(html, type = 'info') {
    const statusDiv = document.getElementById('financialUploadStatus');
    statusDiv.style.display = 'block';
    statusDiv.innerHTML = `<div class="alert alert-${type}">${html}</div>`;
}

function syncFinancialSelectionUi() {
    const selectedFileBox = document.getElementById('financialSelectedFile');
    const runButton = document.getElementById('financialRunButton');
    if (selectedFileBox) {
        selectedFileBox.textContent = currentFinancialFile
            ? `Wybrany plik: ${currentFinancialFile.name}`
            : 'Najpierw wybierz plik.';
    }
    if (runButton) {
        runButton.disabled = !currentFinancialFile;
    }
}

function handleFinancialFileSelection(event) {
    const file = event.target.files[0];
    if (!file) return;
    currentFinancialFile = file;
    currentFinancialFileToken = financialBuildFileToken(file);
    syncFinancialSelectionUi();
    setFinancialStatus(
        `📄 Wybrano plik <strong>${financialEscape(file.name)}</strong>. Wybierz tryb i kliknij <strong>GO</strong>.`,
        'secondary'
    );
    event.target.value = '';
}

async function runFinancialAnalysis() {
    const file = currentFinancialFile;
    if (!file) {
        setFinancialStatus('❌ Najpierw wybierz plik Excel.', 'danger');
        return;
    }
    const analysisMode = document.getElementById('financialAnalysisMode')?.value || 'targeted';
    const runButton = document.getElementById('financialRunButton');
    const sameFileAsCurrentReport = Boolean(
        currentFinancialReport &&
        currentFinancialReportToken &&
        currentFinancialReportToken === currentFinancialFileToken
    );
    const currentMode = sameFileAsCurrentReport ? (currentFinancialReport.analysis?.requested_mode || '') : '';

    if (sameFileAsCurrentReport && financialModeRank(currentMode) >= financialModeRank(analysisMode)) {
        if (financialModeRank(currentMode) > financialModeRank(analysisMode)) {
            const modeSelect = document.getElementById('financialAnalysisMode');
            if (modeSelect) modeSelect.value = currentMode;
            setFinancialStatus(
                `ℹ️ Ten plik ma już analizę w trybie <strong>${financialEscape(financialAnalysisModeLabel(currentMode))}</strong>. Nie cofam wyniku do słabszego trybu.`,
                'secondary'
            );
        } else {
            setFinancialStatus(
                `ℹ️ Ten plik jest już policzony w trybie <strong>${financialEscape(financialAnalysisModeLabel(currentMode))}</strong>.`,
                'secondary'
            );
        }
        return;
    }

    if (runButton) {
        runButton.disabled = true;
        runButton.textContent = 'Liczę...';
    }
    setFinancialStatus('<span class="spinner-border spinner-border-sm me-2"></span>Analizuję plik...', 'info');

    const formData = new FormData();
    formData.append('file', file);
    formData.append('analysis_type', analysisMode);
    formData.append('client_last_modified', String(file.lastModified || ''));
    formData.append('client_size', String(file.size || ''));
    formData.append('client_type', file.type || '');

    const baseUrl = window.location.origin || 'http://localhost:5000';
    const controller = new AbortController();
    const timeoutMs = file.size > 8 * 1024 * 1024 ? 900000 : 420000;
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

    try {
        const response = await fetch(baseUrl + '/api/audit/financial', {
            method: 'POST',
            body: formData,
            signal: controller.signal
        });

        if (!response.ok) {
            const error = await response.json();
            statusDiv.innerHTML = `<div class="alert alert-danger">❌ Błąd: ${error.error || 'Nieznany błąd'}</div>`;
            return;
        }

        const report = await response.json();
        currentFinancialReport = report;
        currentFinancialReportToken = currentFinancialFileToken;

        const upgradedFrom = sameFileAsCurrentReport ? currentMode : '';
        if (upgradedFrom && financialModeRank(analysisMode) > financialModeRank(upgradedFrom)) {
            const reuseNote = report.analysis?.screening_reused
                ? ' Użyto wcześniej policzonego screeningu i dołożono głębszy odczyt.'
                : '';
            setFinancialStatus(
                `✓ Rozszerzono analizę pliku <strong>${financialEscape(file.name)}</strong> z trybu <strong>${financialEscape(financialAnalysisModeLabel(upgradedFrom))}</strong> do <strong>${financialEscape(financialAnalysisModeLabel(report.analysis?.requested_mode || analysisMode))}</strong>.${reuseNote}`,
                'success'
            );
        } else {
            setFinancialStatus(
                `✓ Analiza zakończona w trybie <strong>${financialEscape(financialAnalysisModeLabel(report.analysis?.requested_mode || analysisMode))}</strong>. Przeanalizowano plik <strong>${financialEscape(file.name)}</strong>.`,
                'success'
            );
        }
        displayFinancialResults(report);

    } catch (error) {
        if (error.name === 'AbortError') {
            setFinancialStatus('❌ Timeout: Analiza trwa zbyt długo. Dla dużych plików wybierz najpierw tryb szybki, a potem rozszerz wynik do normalnego albo dokładnego.', 'danger');
        } else {
            setFinancialStatus(`❌ Błąd sieciowy: ${financialEscape(error.message)}`, 'danger');
        }
    } finally {
        clearTimeout(timeoutId);
        if (runButton) {
            runButton.disabled = !currentFinancialFile;
            runButton.textContent = 'GO';
        }
    }
}

function displayFinancialResults(report) {
    const resultsDiv = document.getElementById('financialResults');
    resultsDiv.style.display = 'block';

    // Podsumowanie
    const summary = report.summary;
    const fileMeta = report.file_metadata || {};
    const workbookMeta = fileMeta.workbook || {};
    const logicOverview = report.logic_overview || {};
    const analysisMeta = report.analysis || {};
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
                <div class="text-muted small">Ocena ryzyka</div>
                <div style="font-size:1.25rem;font-weight:700;color:${riskColor[summary.risk_level]};">${financialRiskLabel(summary.risk_level)}</div>
            </div>
        </div>
    `;
    document.getElementById('financialSummary').innerHTML = summaryHtml;

    const metadataHtml = `
        <div class="search-card mb-4" style="box-shadow:none;border:1px solid var(--c-border);">
            <h6 class="mb-3">🧾 Metadane wczytanego pliku</h6>
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;font-size:0.9rem;">
                <div><strong>Nazwa pliku:</strong><br>${financialEscape(fileMeta.name || report.file_name || '—')}</div>
                <div><strong>Rozmiar:</strong><br>${financialEscape(fileMeta.size_human || '—')}</div>
                <div><strong>Wysłano:</strong><br>${financialEscape(fileMeta.uploaded_at || '—')}</div>
                <div><strong>Ostatnia modyfikacja pliku:</strong><br>${financialEscape(fileMeta.client_modified_at || 'brak danych z przeglądarki')}</div>
                <div><strong>Liczba arkuszy:</strong><br>${financialEscape(workbookMeta.sheet_count || 0)}</div>
                <div><strong>Ukryte arkusze:</strong><br>${financialEscape(workbookMeta.hidden_sheet_count || 0)}</div>
                <div><strong>Autor:</strong><br>${financialEscape(workbookMeta.creator || 'brak danych')}</div>
                <div><strong>Ostatnio modyfikował:</strong><br>${financialEscape(workbookMeta.last_modified_by || 'brak danych')}</div>
            </div>
            <div class="small text-muted mt-3">
                <strong>Arkusze:</strong> ${financialEscape((workbookMeta.sheet_names || []).join(', ') || 'brak danych')}<br>
                <strong>Typ MIME:</strong> ${financialEscape(fileMeta.mime_type || 'nieznany')}<br>
                <strong>Czas zapisu na serwerze:</strong> ${financialEscape(fileMeta.received_at || '—')}
            </div>
        </div>
    `;
    document.getElementById('financialMetadata').innerHTML = metadataHtml;

    const hubs = (logicOverview.hub_cells || []).map(cell => `
        <li>
            <strong>${financialEscape(cell.cell)}</strong> — zależy od ${financialEscape(cell.dependencies_count)} komórek i zasila ${financialEscape(cell.dependents_count)} kolejnych obliczeń.
        </li>
    `).join('');

    document.getElementById('financialLogic').innerHTML = `
        <div class="search-card mb-4" style="box-shadow:none;border:1px solid var(--c-border);">
            <h6 class="mb-3">🧠 Logika skoroszytu</h6>
            <p class="mb-2">${financialEscape(logicOverview.description || 'Brak opisu logiki arkusza.')}</p>
            <div class="small text-muted mb-2">
                Tryb: <strong>${financialEscape(financialAnalysisModeLabel(analysisMeta.requested_mode))}</strong> |
                Dogłębnie przeskanowane arkusze: <strong>${financialEscape(analysisMeta.deep_scan_sheets || 0)}</strong> |
                Tylko screening: <strong>${financialEscape(analysisMeta.screening_only_sheets || 0)}</strong><br>
                Maksymalna głębokość zależności: <strong>${financialEscape(logicOverview.max_dependency_depth || 0)}</strong> |
                Odwołania między arkuszami: <strong>${financialEscape(logicOverview.cross_sheet_references || 0)}</strong>
            </div>
            ${hubs ? `<div><strong>Najważniejsze węzły obliczeń:</strong><ul class="mb-0 mt-2">${hubs}</ul></div>` : '<div class="text-muted small">Nie wykryto centralnych węzłów logiki.</div>'}
        </div>
    `;

    renderFinancialFlowExplorer(report);

    // Anomalie
    if (summary.anomalies_count > 0) {
        let anomaliesHtml = '<h6 class="mb-3">⚠️ Wykryte nieprawidłowości:</h6>';
        anomaliesHtml += '<div style="max-height:300px;overflow-y:auto;">';

        report.summary.anomalies = report.summary.anomalies || [];
        for (const anomaly of report.summary.anomalies.slice(0, 20)) {
            const severityBg = anomaly.severity === 'HIGH' ? '#fee' : '#fef3c7';
            const severityColor = anomaly.severity === 'HIGH' ? '#dc2626' : '#d97706';
            anomaliesHtml += `
                <div style="background:${severityBg};padding:10px;margin-bottom:8px;border-left:4px solid ${severityColor};border-radius:4px;">
                    <strong style="color:${severityColor};">${financialEscape(anomaly.label || anomaly.type)}</strong>
                    <span class="badge" style="background:${severityColor};">${financialEscape(anomaly.severity_label || anomaly.severity)}</span><br>
                    <small><strong>Opis:</strong> ${financialEscape(anomaly.message)}</small><br>
                    <small><strong>Wpływ:</strong> ${financialEscape(anomaly.impact || 'Wymaga ręcznej weryfikacji.')}</small><br>
                    <small><strong>Rekomendacja:</strong> ${financialEscape(anomaly.recommendation || 'Sprawdź wskazaną komórkę i jej rolę w modelu.')}</small>
                    ${anomaly.cell ? `<br><small><strong>Komórka:</strong> ${financialEscape(anomaly.cell)}</small>` : ''}
                    ${anomaly.formula ? `<br><code style="font-size:0.75rem;background:#f1f5f9;padding:4px;border-radius:2px;display:inline-block;margin-top:4px;">${financialEscape(anomaly.formula)}</code>` : ''}
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
        const outputCells = (sheetData.logic_summary?.output_cells || []).slice(0, 5);
        const hubsText = (sheetData.logic_summary?.hub_cells || []).slice(0, 3).map(h => h.cell).join(', ');
        const focusRows = (sheetData.analysis?.focus_rows || []).slice(0, 8).join(', ');
        const focusColumns = (sheetData.analysis?.focus_columns || []).slice(0, 8).join(', ');
        const evidenceSummary = sheetData.evidence_summary || {};
        const evidenceItems = (evidenceSummary.items || []).slice(0, 4).map(item => `
            <li>${financialEscape(item)}</li>
        `).join('');
        const formulasPreview = (sheetData.formulas || []).slice(0, 6).map(item => `
            <tr>
                <td><code>${financialEscape(item.cell)}</code></td>
                <td><code>${financialEscape(item.formula)}</code></td>
                <td>${financialEscape((item.dependencies || []).join(', ') || '—')}</td>
                <td>${financialEscape(item.value ?? 'brak')}</td>
            </tr>
        `).join('');
        sheetsHtml += `
            <div style="background:var(--c-surface);padding:12px;border-radius:var(--r-md);margin-bottom:12px;">
                <strong>${financialEscape(sheetName)}</strong>
                <div class="small text-muted mt-1">
                    Formuł: ${financialEscape(sheetData.formula_count)} |
                    Komórek z logiką: ${financialEscape(sheetData.cell_count)} |
                    Stan arkusza: ${financialEscape(sheetData.sheet_state === 'visible' ? 'widoczny' : 'ukryty')} |
                    Priorytet weryfikacji: <span style="color:${riskColor[sheetData.risk_level]};font-weight:600;">${financialEscape(financialSheetPriorityLabel(sheetData.risk_level))}</span> |
                    Tryb: ${financialEscape(sheetData.analysis?.mode || 'screening_only')}
                </div>
                <div class="mt-2 small">
                    <strong>${financialEscape(evidenceSummary.headline || 'Opis logiki arkusza:')}</strong>
                    ${evidenceItems ? `<ul class="mb-1 mt-1">${evidenceItems}</ul>` : financialEscape(sheetData.logic_summary?.description_pl || 'Brak dodatkowego opisu logiki.')}
                </div>
                <div class="small text-muted mt-1">${financialEscape(sheetData.logic_summary?.description_pl || '')}</div>
                <div class="small text-muted mt-2">
                    Wynikowe komórki: ${financialEscape(outputCells.join(', ') || 'brak')}<br>
                    Centralne węzły: ${financialEscape(hubsText || 'brak')}<br>
                    Fokus tropienia: kolumny ${financialEscape(focusColumns || '—')} / wiersze ${financialEscape(focusRows || '—')}
                </div>
                ${formulasPreview ? `
                    <div class="table-responsive mt-3">
                        <table class="table table-sm">
                            <thead>
                                <tr><th>Komórka</th><th>Formuła</th><th>Zależności</th><th>Wartość</th></tr>
                            </thead>
                            <tbody>${formulasPreview}</tbody>
                        </table>
                    </div>
                ` : ''}
            </div>
        `;
    }
    document.getElementById('financialSheets').innerHTML = sheetsHtml;
}

function renderFinancialFlowExplorer(report) {
    const host = document.getElementById('financialFlowExplorer');
    if (!host) return;
    const sheetEntries = Object.entries(report.sheets || {}).filter(([, sheetData]) => (sheetData.flow_graph?.nodes || []).length > 0);
    if (!sheetEntries.length) {
        host.innerHTML = '';
        return;
    }

    const selectedSheet = sheetEntries.some(([name]) => name === host.dataset.selectedSheet)
        ? host.dataset.selectedSheet
        : sheetEntries[0][0];

    host.innerHTML = `
        <div class="search-card" style="box-shadow:none;border:1px solid var(--c-border);">
            <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-3">
                <div>
                    <h6 class="mb-1">🕸️ Relacja arkusza</h6>
                    <div class="small text-muted">Przepływ pokazuje, skąd wynik pobiera dane i które komórki są punktami kontroli lub ręcznej ingerencji.</div>
                </div>
                <div>
                    <label for="financialFlowSheetSelect" class="form-label small fw-semibold mb-1">Arkusz</label>
                    <select id="financialFlowSheetSelect" class="form-select form-select-sm">
                        ${sheetEntries.map(([name]) => `<option value="${financialEscape(name)}"${name === selectedSheet ? ' selected' : ''}>${financialEscape(name)}</option>`).join('')}
                    </select>
                </div>
            </div>
            <div id="financialFlowDetails"></div>
        </div>
    `;

    const select = document.getElementById('financialFlowSheetSelect');
    if (select) {
        select.onchange = () => {
            host.dataset.selectedSheet = select.value;
            renderFinancialSheetFlow(report, select.value);
        };
    }
    host.dataset.selectedSheet = selectedSheet;
    renderFinancialSheetFlow(report, selectedSheet);
}

function renderFinancialSheetFlow(report, sheetName) {
    const target = document.getElementById('financialFlowDetails');
    if (!target) return;
    const sheetData = (report.sheets || {})[sheetName];
    const flow = sheetData?.flow_graph || {};
    const nodes = flow.nodes || [];
    const edges = flow.edges || [];
    const narratives = flow.narratives || [];

    if (!nodes.length) {
        target.innerHTML = '<div class="text-muted small">Brak danych do wizualizacji przepływu dla tego arkusza.</div>';
        return;
    }

    const layers = [...new Set(nodes.map(node => Number(node.layer) || 0))].sort((a, b) => a - b);
    const nodesByLayer = new Map();
    for (const layer of layers) nodesByLayer.set(layer, []);
    for (const node of nodes) nodesByLayer.get(Number(node.layer) || 0).push(node);
    for (const items of nodesByLayer.values()) {
        items.sort((a, b) => String(a.kind).localeCompare(String(b.kind)) || String(a.label).localeCompare(String(b.label)));
    }

    const columnWidth = 220;
    const nodeWidth = 180;
    const nodeHeight = 56;
    const rowGap = 74;
    const margin = { top: 24, right: 24, bottom: 24, left: 24 };
    const tallestLayer = Math.max(...[...nodesByLayer.values()].map(items => items.length), 1);
    const svgWidth = Math.max(860, margin.left + margin.right + Math.max(layers.length, 1) * columnWidth);
    const innerHeight = Math.max(220, tallestLayer * rowGap + 24);
    const svgHeight = margin.top + innerHeight + margin.bottom;
    const positions = new Map();

    layers.forEach((layer, layerIndex) => {
        const items = nodesByLayer.get(layer) || [];
        const totalHeight = items.length > 1 ? (items.length - 1) * rowGap : 0;
        const startY = margin.top + Math.max(0, (innerHeight - totalHeight) / 2);
        items.forEach((node, nodeIndex) => {
            positions.set(node.id, {
                x: margin.left + layerIndex * columnWidth,
                y: startY + nodeIndex * rowGap
            });
        });
    });

    const nodeMap = new Map(nodes.map(node => [node.id, node]));
    const edgeSvg = edges.map(edge => {
        const sourcePos = positions.get(edge.source);
        const targetPos = positions.get(edge.target);
        if (!sourcePos || !targetPos) return '';
        const sourceNode = nodeMap.get(edge.source) || {};
        const targetNode = nodeMap.get(edge.target) || {};
        const sourceX = sourcePos.x + nodeWidth;
        const sourceY = sourcePos.y + nodeHeight / 2;
        const targetX = targetPos.x;
        const targetY = targetPos.y + nodeHeight / 2;
        const midX = sourceX + (targetX - sourceX) / 2;
        const stroke = financialFlowColor(targetNode.kind).stroke;
        const dash = sourceNode.kind === 'external' ? '6 4' : '';
        return `<path d="M ${sourceX} ${sourceY} C ${midX} ${sourceY}, ${midX} ${targetY}, ${targetX} ${targetY}" fill="none" stroke="${stroke}" stroke-width="2.2"${dash ? ` stroke-dasharray="${dash}"` : ''} opacity="0.8" marker-end="url(#financialFlowArrow)"></path>`;
    }).join('');

    const nodeSvg = nodes.map(node => {
        const pos = positions.get(node.id);
        if (!pos) return '';
        const colors = financialFlowColor(node.kind);
        const shortNote = financialEscape(node.note || '');
        const label = financialEscape(node.label || node.id);
        const formula = node.formula ? financialEscape(node.formula) : '';
        return `
            <g transform="translate(${pos.x}, ${pos.y})">
                <rect x="0" y="0" rx="12" ry="12" width="${nodeWidth}" height="${nodeHeight}" fill="${colors.fill}" stroke="${colors.stroke}" stroke-width="1.5"></rect>
                <text x="12" y="20" font-size="13" font-weight="700" fill="${colors.text}">${label}</text>
                <text x="12" y="37" font-size="11" fill="${colors.text}" opacity="0.85">${shortNote}</text>
                ${formula ? `<title>${label}\n${formula}</title>` : `<title>${label}</title>`}
            </g>
        `;
    }).join('');

    const legend = [
        ['input', 'wejście z danych'],
        ['external', 'wejście z innego arkusza'],
        ['formula', 'zwykły etap liczenia'],
        ['hub', 'węzeł centralny'],
        ['anomaly', 'miejsce do sprawdzenia'],
        ['output', 'wynik']
    ].map(([kind, label]) => {
        const colors = financialFlowColor(kind);
        return `<span style="display:inline-flex;align-items:center;gap:6px;margin-right:12px;margin-bottom:6px;">
            <span style="width:12px;height:12px;border-radius:999px;background:${colors.fill};border:2px solid ${colors.stroke};display:inline-block;"></span>
            <span>${financialEscape(label)}</span>
        </span>`;
    }).join('');

    const notesHtml = narratives.length
        ? `<ul class="mb-0">${narratives.map(item => `<li>${financialEscape(item)}</li>`).join('')}</ul>`
        : `<div class="text-muted small">${financialEscape(sheetData.logic_summary?.description_pl || 'Brak dodatkowego opisu przepływu.')}</div>`;

    target.innerHTML = `
        <div class="row g-3">
            <div class="col-lg-8">
                <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:12px;overflow:auto;">
                    <div class="small text-muted mb-2">Strzałki pokazują przepływ od wejść do komórek wynikowych.</div>
                    <svg width="${svgWidth}" height="${svgHeight}" viewBox="0 0 ${svgWidth} ${svgHeight}" role="img" aria-label="Relacja arkusza ${financialEscape(sheetName)}">
                        <defs>
                            <marker id="financialFlowArrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">
                                <path d="M0,0 L0,6 L9,3 z" fill="#64748b"></path>
                            </marker>
                        </defs>
                        ${edgeSvg}
                        ${nodeSvg}
                    </svg>
                </div>
            </div>
            <div class="col-lg-4">
                <div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:14px;height:100%;">
                    <div class="small fw-semibold mb-2">Jak czytać ten przepływ</div>
                    <div class="small text-muted mb-3">${legend}</div>
                    <div class="small fw-semibold mb-2">Opis przepływu</div>
                    <div class="small">${notesHtml}</div>
                    <div class="small text-muted mt-3">
                        Pokazano <strong>${financialEscape(nodes.length)}</strong> węzłów i <strong>${financialEscape(edges.length)}</strong> połączeń.
                        ${flow.hidden_formula_nodes ? ` Pozostałe formuły poza głównym przepływem: <strong>${financialEscape(flow.hidden_formula_nodes)}</strong>.` : ''}
                    </div>
                </div>
            </div>
        </div>
    `;
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

syncFinancialSelectionUi();
