from utils import resource_guard


def test_safe_thread_count_caps_to_percent(monkeypatch):
    monkeypatch.setattr(resource_guard.os, "cpu_count", lambda: 10)
    assert resource_guard.safe_thread_count(80) == 8


def test_ensure_resources_available_rejects_over_limit(monkeypatch):
    monkeypatch.setattr(
        resource_guard,
        "get_resource_snapshot",
        lambda disk_path="/", gpu_index=None: {
            "cpu_pct": 81.0,
            "ram_pct": 40.0,
            "disk_pct": 50.0,
            "gpu_pct": 20.0,
        },
    )
    ok, reason, snapshot = resource_guard.ensure_resources_available(cpu_limit_pct=80)
    assert ok is False
    assert "CPU" in reason
    assert snapshot["cpu_pct"] == 81.0
