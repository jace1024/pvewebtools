"""纯逻辑：pve_api 中无副作用的工具函数（不调用 pvesh）。"""

import pytest

from core.pve import pve_api


@pytest.mark.parametrize(
    "addr, expected",
    [
        ("127.0.0.1", True),      # 回环
        ("10.1.2.3", True),       # 10/8 私有
        ("172.16.0.1", True),     # 172.16/12 私有
        ("169.254.1.1", True),    # 链路本地
        ("192.168.1.10", False),  # LAN 段刻意保留
        ("172.32.0.1", False),    # 172.16/12 之外
        ("8.8.8.8", False),
        ("not-an-ip", True),      # 非法输入按「内部」处理
    ],
)
def test_is_internal_ipv4(addr, expected):
    assert pve_api._is_internal_ipv4(addr) is expected


def test_parse_options_kv_basic():
    assert pve_api._parse_options_kv("foo=bar,baz=qux") == ["--foo", "bar", "--baz", "qux"]


def test_parse_options_kv_empty():
    assert pve_api._parse_options_kv("") == []
    assert pve_api._parse_options_kv(None) == []


def test_parse_options_kv_skips_invalid_segments():
    # 无 "=" 、注释行、非法键（含点）都被忽略
    assert pve_api._parse_options_kv("k=v,#c=x,bad,y.z=1") == ["--k", "v"]


def test_parse_options_kv_keeps_value_with_equals():
    assert pve_api._parse_options_kv("k=a=b") == ["--k", "a=b"]


def test_normalize_cluster_name_ok():
    assert pve_api.normalize_cluster_name("  pve-cluster  ") == "pve-cluster"
    assert pve_api.normalize_cluster_name("a.b-c_1") == "a.b-c_1"


@pytest.mark.parametrize("bad", ["", "1abc", "a b", "a" * 65, "集群"])
def test_normalize_cluster_name_rejects(bad):
    with pytest.raises(pve_api.PveError):
        pve_api.normalize_cluster_name(bad)


def test_list_guests_with_ips_cache_miss_no_nameerror(monkeypatch):
    """回归：缓存未命中分支读取 `_ip_cache_time` 不能 NameError（该模块级变量曾被漏定义）。

    通过 monkeypatch 把外部依赖（pvesh、各本地 conf map、IP 探测）全部替换，
    并强制 `_ip_cache_time = 0.0` 触发「缓存已过期」分支，确保 line 334 的读取不再抛 NameError。
    """
    from core.pve.api import vm

    guests = [
        {"vmid": "100", "node": "pve01", "type": "qemu", "status": "running", "name": "vm1"},
        {"vmid": "101", "node": "pve01", "type": "lxc", "status": "stopped", "name": "ct1"},
    ]
    monkeypatch.setattr(vm, "_run_pvesh", lambda *a, **k: guests)
    monkeypatch.setattr(vm, "_qemu_ips", lambda node, vmid: [])
    monkeypatch.setattr(vm, "_lxc_ips", lambda node, vmid: [])
    monkeypatch.setattr(vm, "_onboot_map", lambda: {})
    monkeypatch.setattr(vm, "_passthrough_map", lambda: {})
    monkeypatch.setattr(vm, "_passthrough_kind", lambda: {})
    monkeypatch.setattr(vm, "_description_map", lambda: {})
    monkeypatch.setattr(vm, "_ip_cache_time", 0.0)  # 强制缓存未命中，命中原 NameError 分支

    out = vm.list_guests_with_ips()
    assert isinstance(out, list) and len(out) == 2
    assert out[0]["onboot"] in (0, 1)
    assert "has_passthrough" in out[0]
    assert "ip" in out[0]
