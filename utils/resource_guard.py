import os
import shutil
import subprocess


def safe_thread_count(limit_pct: int = 80) -> int:
    cpu_count = max(1, os.cpu_count() or 1)
    clamped = max(1, min(100, int(limit_pct or 80)))
    return max(1, int(cpu_count * clamped / 100))


def _cpu_usage_pct() -> float | None:
    try:
        load1 = os.getloadavg()[0]
        cpu_count = max(1, os.cpu_count() or 1)
        return max(0.0, min(100.0, (load1 / cpu_count) * 100.0))
    except Exception:
        return None


def _ram_usage_pct() -> float | None:
    try:
        mem_total = None
        mem_available = None
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    mem_available = int(line.split()[1]) * 1024
                if mem_total is not None and mem_available is not None:
                    break
        if not mem_total or mem_available is None:
            return None
        used = mem_total - mem_available
        return max(0.0, min(100.0, (used / mem_total) * 100.0))
    except Exception:
        return None


def _disk_usage_pct(path: str = "/") -> float | None:
    try:
        total, used, _free = shutil.disk_usage(path)
        if total <= 0:
            return None
        return max(0.0, min(100.0, (used / total) * 100.0))
    except Exception:
        return None


def _gpu_memory_usage_pct(gpu_index: int | None = None) -> float | None:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        rows = []
        for line in result.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 2:
                continue
            used = float(parts[0])
            total = float(parts[1])
            if total > 0:
                rows.append((used / total) * 100.0)
        if not rows:
            return None
        if gpu_index is not None and 0 <= gpu_index < len(rows):
            return max(0.0, min(100.0, rows[gpu_index]))
        return max(rows)
    except Exception:
        return None


def get_resource_snapshot(disk_path: str = "/", gpu_index: int | None = None) -> dict:
    return {
        "cpu_pct": _cpu_usage_pct(),
        "ram_pct": _ram_usage_pct(),
        "disk_pct": _disk_usage_pct(disk_path),
        "gpu_pct": _gpu_memory_usage_pct(gpu_index),
    }


def ensure_resources_available(
    *,
    cpu_limit_pct: int = 80,
    ram_limit_pct: int = 80,
    disk_limit_pct: int = 80,
    gpu_limit_pct: int = 80,
    disk_path: str = "/",
    gpu_index: int | None = None,
) -> tuple[bool, str | None, dict]:
    snapshot = get_resource_snapshot(disk_path=disk_path, gpu_index=gpu_index)
    limits = {
        "cpu_pct": cpu_limit_pct,
        "ram_pct": ram_limit_pct,
        "disk_pct": disk_limit_pct,
        "gpu_pct": gpu_limit_pct,
    }
    labels = {
        "cpu_pct": "CPU",
        "ram_pct": "RAM",
        "disk_pct": "dysk",
        "gpu_pct": "GPU VRAM",
    }
    for key, limit in limits.items():
        value = snapshot.get(key)
        if value is None:
            continue
        if value >= float(limit):
            return (
                False,
                f"{labels[key]} zajęte w {value:.1f}% (limit ochronny {limit}%)",
                snapshot,
            )
    return True, None, snapshot
