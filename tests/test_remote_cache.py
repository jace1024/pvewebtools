"""远端节点采集缓存：同一节点同一采集函数 30s 内不再重复 SSH。"""

from types import SimpleNamespace

import pytest

from core.web import probe


def _stub_run(monkeypatch, payload='[{"slot": "0000:01:00.0"}]'):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    monkeypatch.setattr(probe.backend, "ssh_host", lambda node: "10.0.0.9")
    monkeypatch.setattr(probe.backend, "ssh_host_opts", lambda node: [])
    return calls


def test_remote_collect_cached(monkeypatch):
    calls = _stub_run(monkeypatch)
    probe.invalidate_remote_cache()
    assert probe._run_func_on_node("other-node", "list_pci_nics") == [{"slot": "0000:01:00.0"}]
    assert probe._run_func_on_node("other-node", "list_pci_nics") == [{"slot": "0000:01:00.0"}]
    assert len(calls) == 1          # 第二次命中缓存，没有再 SSH
    # 不同函数各自一条缓存
    probe._run_func_on_node("other-node", "list_usb_devices")
    assert len(calls) == 2
    probe.invalidate_remote_cache()


def test_remote_collect_cache_invalidate(monkeypatch):
    calls = _stub_run(monkeypatch)
    probe.invalidate_remote_cache()
    probe._run_func_on_node("other-node", "list_pci_gpus")
    probe.invalidate_remote_cache("other-node")
    probe._run_func_on_node("other-node", "list_pci_gpus")
    assert len(calls) == 2          # 失效后重新采集
    probe.invalidate_remote_cache()


def test_remote_collect_cache_expires(monkeypatch):
    calls = _stub_run(monkeypatch)
    probe.invalidate_remote_cache()
    clock = {"t": 1000.0}
    monkeypatch.setattr(probe.time, "time", lambda: clock["t"])
    probe._run_func_on_node("other-node", "list_pci_storage")
    probe._run_func_on_node("other-node", "list_pci_storage")
    assert len(calls) == 1
    clock["t"] += probe._REMOTE_CACHE_TTL + 1
    probe._run_func_on_node("other-node", "list_pci_storage")
    assert len(calls) == 2
    probe.invalidate_remote_cache()


def test_remote_skips_when_node_marked_unreachable(monkeypatch):
    """节点已进「不可达负缓存」时直接失败，不再付一次 SSH 建连超时。"""
    calls = _stub_run(monkeypatch)
    probe.invalidate_remote_cache()
    probe.backend._unreachable_until.pop("dead-node", None)
    probe.backend.mark_node_unreachable("dead-node")
    try:
        with pytest.raises(RuntimeError):
            probe._run_func_on_node("dead-node", "list_pci_nics")
        assert calls == []
    finally:
        probe.backend._unreachable_until.pop("dead-node", None)


def test_remote_ssh_failure_marks_unreachable(monkeypatch):
    """SSH 自身失败（returncode 255）→ 记负缓存；下一次请求被直接挡掉。"""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=255, stdout="",
                               stderr="ssh: connect to host 10.0.0.9 port 22: Connection timed out")

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    monkeypatch.setattr(probe.backend, "ssh_host", lambda node: "10.0.0.9")
    monkeypatch.setattr(probe.backend, "ssh_host_opts", lambda node: [])
    probe.invalidate_remote_cache()
    probe.backend._unreachable_until.pop("other-node", None)
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError):
                probe._run_func_on_node("other-node", "list_pci_nics")
        assert len(calls) == 1
        assert probe.backend.node_unreachable("other-node")
    finally:
        probe.backend._unreachable_until.pop("other-node", None)


def test_remote_success_marks_reachable(monkeypatch):
    """采集成功要解除负缓存，避免节点恢复后仍被当成不可达。"""
    _stub_run(monkeypatch)
    marks = []
    monkeypatch.setattr(probe.backend, "mark_node_reachable", lambda n: marks.append(n))
    probe.invalidate_remote_cache()
    probe.backend._unreachable_until.pop("other-node", None)
    probe._run_func_on_node("other-node", "list_pci_nics")
    assert marks == ["other-node"]


def test_remote_failure_not_cached(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="宿主机不可达")

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    monkeypatch.setattr(probe.backend, "ssh_host", lambda node: "10.0.0.9")
    monkeypatch.setattr(probe.backend, "ssh_host_opts", lambda node: [])
    probe.invalidate_remote_cache()
    for _ in range(2):
        try:
            probe._run_func_on_node("other-node", "list_pci_nics")
        except RuntimeError:
            pass
        else:  # pragma: no cover
            raise AssertionError("远端失败应抛 RuntimeError")
    assert len(calls) == 2          # 失败不写入缓存，下次仍会重试
