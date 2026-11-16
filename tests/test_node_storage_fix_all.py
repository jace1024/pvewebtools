"""节点管理「一键修复共享存储」：导出端优先、跳过离线/占位、逐台汇总不中断。"""

from types import SimpleNamespace

from core.web import handlers
from core.web.handlers import HandlerMixin


def _fake_handler(monkeypatch, nodes, roles, fixes=None, errors=None):
    """只替换采集/角色/日志这几个外部依赖，修复动作记录调用顺序而不真的 SSH。"""
    fixes, errors = fixes or {}, errors or {}
    monkeypatch.setattr(handlers, "collect_nodes_cached", lambda: nodes)
    monkeypatch.setattr(handlers, "_merge_cluster_nodes", lambda ns: ns)
    monkeypatch.setattr(handlers.storage_provision, "_load_config", lambda: {})
    monkeypatch.setattr(handlers, "_storage_role", lambda name, ip, cfg: roles.get(name, "client"))
    monkeypatch.setattr(handlers.jobs, "record_event", lambda *args, **kwargs: None)
    calls = []

    def fix(name):
        calls.append(name)
        if name in errors:
            raise RuntimeError(errors[name])
        return fixes.get(name, (True, "OK"))

    return SimpleNamespace(_json=lambda data: data, _fix_node_storage=fix), calls


def test_storage_fix_all_server_first_and_skips_unknown(monkeypatch):
    nodes = [
        {"node": "pve2", "status": "online", "ip": "10.0.0.2"},
        {"node": "pve1", "status": "online", "ip": "10.0.0.1"},
        {"node": "pve3", "status": "offline", "ip": "10.0.0.3"},
        {"node": "pve4", "status": "online", "ip": "10.0.0.4", "placeholder": True},
        {"node": "pve5", "status": "online", "ip": "10.0.0.5"},
    ]
    roles = {"pve1": "server", "pve2": "client", "pve3": "client",
             "pve4": "client", "pve5": "client"}
    h, calls = _fake_handler(monkeypatch, nodes, roles,
                             fixes={"pve2": (False, "挂载失败")})
    data = HandlerMixin._handle_node_storage_fix_all(h)
    # 导出端必须先就绪客户端才挂得上；离线/占位节点都不该拖住整批
    assert calls == ["pve1", "pve2", "pve5"]
    assert [r["node"] for r in data["results"]] == ["pve1", "pve2", "pve5"]
    assert data["skipped"] == ["pve3", "pve4"]
    assert (data["ok"], data["done"], data["total"]) == (False, 2, 3)
    assert "失败" in data["msg"]
    assert data["results"][1]["msg"] == "挂载失败"


def test_storage_fix_all_dir_mode_has_no_targets(monkeypatch):
    nodes = [{"node": "pve1", "status": "online", "ip": "10.0.0.1"}]
    h, calls = _fake_handler(monkeypatch, nodes, {"pve1": "dir"})
    data = HandlerMixin._handle_node_storage_fix_all(h)
    assert calls == []
    assert data["total"] == 0 and data["ok"] is True
    assert "没有需要修复的节点" in data["msg"]


def test_storage_fix_all_one_failure_does_not_stop_others(monkeypatch):
    nodes = [{"node": n, "status": "online", "ip": "10.0.0.1"} for n in ("pve1", "pve2")]
    h, calls = _fake_handler(monkeypatch, nodes, {"pve1": "server", "pve2": "client"},
                             errors={"pve1": "SSH 断开"})
    data = HandlerMixin._handle_node_storage_fix_all(h)
    assert calls == ["pve1", "pve2"]
    assert (data["ok"], data["done"], data["total"]) == (False, 1, 2)
    assert "SSH 断开" in data["results"][0]["msg"]
