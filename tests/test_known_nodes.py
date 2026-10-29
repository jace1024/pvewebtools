"""节点名校验改读本地成员表：不再为每个带 ?node= 的下拉跑一次 pvesh get /nodes。"""

import pytest

from core.pve.api import node as node_mod


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    # 测试机即便恰好是 PVE 也不读真实节点目录，避免环境污染
    monkeypatch.setattr(node_mod, "_local_node_dirs", lambda: set())
    node_mod.invalidate_known_nodes_cache()
    yield
    node_mod.invalidate_known_nodes_cache()


def test_known_node_names_uses_local_members(monkeypatch):
    """集群成员读 corosync nodelist（本地文件）即可，不跑 pvesh。"""
    monkeypatch.setattr(node_mod, "cluster_members", lambda: ["pve01", "pve02"])
    monkeypatch.setattr(node_mod.socket, "gethostname", lambda: "pve01.local")

    def _no_pvesh(*a, **kw):  # pragma: no cover - 走到这里说明回退逻辑错了
        raise AssertionError("不应回退 pvesh list_nodes")

    monkeypatch.setattr(node_mod, "list_nodes", _no_pvesh)
    assert node_mod.known_node_names() == {"pve01", "pve02"}


def test_known_node_names_falls_back_to_pvesh_on_single_node(monkeypatch):
    """单机（无集群成员信息）时才回退 pvesh，保证非集群部署仍能列出本机。"""
    monkeypatch.setattr(node_mod, "cluster_members", lambda: [])
    monkeypatch.setattr(node_mod.socket, "gethostname", lambda: "solo")
    monkeypatch.setattr(node_mod, "list_nodes",
                        lambda: [{"node": "solo"}, {"node": "extra"}])
    assert node_mod.known_node_names() == {"solo", "extra"}


def test_known_node_names_cached(monkeypatch):
    calls = []

    def _members():
        calls.append(1)
        return ["pve01", "pve02"]

    monkeypatch.setattr(node_mod, "cluster_members", _members)
    monkeypatch.setattr(node_mod.socket, "gethostname", lambda: "pve01")
    node_mod.known_node_names()
    node_mod.known_node_names()
    assert len(calls) == 1          # 10s 内复用，避免每次请求都 readdir pmxcfs
