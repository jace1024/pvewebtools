"""节点关机：确认词强校验 + 本机/远端排程（无远程开机能力，动作必须收窄）。"""

from types import SimpleNamespace

import pytest

from core.tools import toolbox
from core.web import ops


def _handler(confirm="关机节点"):
    return SimpleNamespace(_read_body=lambda: {"confirm": confirm},
                           _json=lambda data: data)


def _patch_common(monkeypatch, local, known):
    monkeypatch.setattr(ops.backend, "known_node_names", lambda: known)
    monkeypatch.setattr(ops.socket, "gethostname", lambda: local)
    monkeypatch.setattr(ops.jobs, "record_event", lambda *args, **kwargs: None)


def test_shutdown_requires_confirm_word(monkeypatch):
    monkeypatch.setattr(ops.subprocess, "Popen", lambda *a, **k: pytest.fail("确认词不对不应排程"))
    with pytest.raises(toolbox.ToolError, match="关机节点"):
        ops.OpsMixin._shutdown_node(_handler("重启节点"), "pve1")


def test_shutdown_local_schedules_poweroff(monkeypatch):
    calls = []
    _patch_common(monkeypatch, "pve1", {"pve1"})
    monkeypatch.setattr(ops.subprocess, "Popen", lambda args, **kw: calls.append(args))
    assert ops.OpsMixin._shutdown_node(_handler(), "pve1") == {"scheduled": "pve1"}
    assert "shutdown -h now" in calls[0][2]      # ["sh", "-c", cmd]
    assert "shutdown -r now" not in calls[0][2]  # 不能误写成重启


def test_shutdown_remote_goes_through_ssh(monkeypatch):
    calls = []
    _patch_common(monkeypatch, "pve1", {"pve1", "pve2"})
    monkeypatch.setattr(ops.backend, "ssh_host_opts", lambda node: [])
    monkeypatch.setattr(ops.backend, "ssh_host", lambda node: "10.0.0.2")
    monkeypatch.setattr(ops.subprocess, "Popen", lambda *a, **k: pytest.fail("远端不应本地执行"))

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ops.subprocess, "run", fake_run)
    assert ops.OpsMixin._shutdown_node(_handler(), "pve2") == {"scheduled": "pve2"}
    assert any("shutdown -h now" in str(a) for a in calls[0])
