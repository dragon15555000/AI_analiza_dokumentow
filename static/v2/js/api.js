document.addEventListener("DOMContentLoaded", function() {
    const btnBrowse = document.getElementById('btnBrowse');
    const btnImport = document.getElementById('btnImport');
    const folderInput = document.getElementById('folderInput');
    const dropZone = document.getElementById('drop-zone');
    
    const progressContainer = document.getElementById('progress-container');
    const progressBar = document.getElementById('progress-bar');
    const progressStatus = document.getElementById('progress-status');
    const resultContainer = document.getElementById('result-container');
    const resultMsg = document.getElementById('result-msg');

    // Drag & Drop events (text dropping)
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('border-primary', 'bg-light');
    });
    dropZone.addEventListener('dragleave', (e) => {
        e.preventDefault();
        dropZone.classList.remove('border-primary', 'bg-light');
    });
    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('border-primary', 'bg-light');
        const text = e.dataTransfer.getData('text');
        if (text) {
            folderInput.value = text;
        } else if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
            // Web browsers mask the absolute path of dropped files for security.
            // We hint the user.
            alert("Przeglądarka blokuje pełną ścieżkę pliku. Użyj przycisku 'Wybierz w Windows' lub wklej ścieżkę ręcznie.");
        }
    });
    
    // Browse native Windows folder via existing endpoint
    btnBrowse.addEventListener('click', async () => {
        try {
            const res = await fetch('/browse/pick', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ initial: folderInput.value })
            });
            const data = await res.json();
            if (data.success === false && data.error) {
                if (data.error !== 'Anulowano lub nie wybrano folderu') {
                    alert('Błąd: ' + data.error);
                }
                return;
            }
            if (data.path) {
                folderInput.value = data.path;
            }
        } catch (e) {
            alert('Błąd komunikacji z backendem.');
        }
    });

    // Start Import
    btnImport.addEventListener('click', async () => {
        const folder = folderInput.value.trim();
        if (!folder) {
            alert('Proszę podać ścieżkę do folderu!');
            return;
        }

        // UI Reset
        resultContainer.classList.add('d-none');
        progressContainer.classList.remove('d-none');
        progressBar.style.width = '0%';
        progressBar.innerText = '0%';
        progressBar.classList.remove('bg-danger', 'bg-success');
        progressStatus.innerText = 'Kolejkowanie zadania...';
        btnImport.disabled = true;

        try {
            const params = new URLSearchParams({ folder: folder, async: '1' });
            const res = await fetch('/import/stream?' + params.toString());
            const data = await res.json();
            
            if (!data.success && data.error) {
                throw new Error(data.error);
            }
            if (!data.task_id) {
                throw new Error(data.message || 'Brak kompatybilnych plików w folderze.');
            }

            // Start SSE polling
            startEventSource(data.task_id);

        } catch (e) {
            showError(e.message);
        }
    });

    function startEventSource(taskId) {
        const source = new EventSource('/tasks/' + taskId + '/stream');

        source.addEventListener('start', (e) => {
            const d = JSON.parse(e.data);
            progressStatus.innerText = `Rozpoczęto analizę ${d.total} plików...`;
        });

        source.addEventListener('progress', (e) => {
            const d = JSON.parse(e.data);
            const pct = d.progress_pct || 0;
            progressBar.style.width = pct + '%';
            progressBar.innerText = Math.round(pct) + '%';
            progressStatus.innerText = `Analiza pliku: ${d.current_item || '...'}`;
        });

        source.addEventListener('done', (e) => {
            const d = JSON.parse(e.data);
            if (d.status === 'failed' || d.error) {
                source.close();
                showError(d.error || 'Zadanie przerwane (błąd limitów lub serwera).');
                return;
            }
            source.close();
            progressBar.style.width = '100%';
            progressBar.innerText = '100%';
            progressBar.classList.add('bg-success');
            progressStatus.innerText = 'Ukończono pomyślnie.';
            btnImport.disabled = false;
            
            // Show results
            resultContainer.classList.remove('d-none');
            resultMsg.innerText = d.msg || 'Import zakończony.';
            document.getElementById('res-count').innerText = d.count || 0;
            document.getElementById('res-chunks').innerText = d.new_chunks || 0;
        });

        source.addEventListener('error', (e) => {
            const d = JSON.parse(e.data);
            source.close();
            showError(d.msg || d.error || 'Błąd podczas streamingu');
        });
        
        source.onerror = (e) => {
            source.close();
            showError('Połączenie przerwane.');
        };
    }

    function showError(msg) {
        progressBar.classList.add('bg-danger');
        progressStatus.innerText = 'BŁĄD: ' + msg;
        btnImport.disabled = false;
    }
});
