"""节点分发回归：显式选择本机时复用实际归属模块的实现。"""

import json
import subprocess
from importlib import import_module
from unittest.mock import Mock

import pytest

from core.web import handlers, probe, template_cache


@pytest.fixture(autouse=True)
def local_hostname(monkeypatch):
    monkeypatch.setattr(probe.socket, "gethostname", lambda: "pve-local.example.test")


@pytest.mark.parametrize(
    "name,module,attr",
    [
        ("list_pci_gpus", probe, "list_pci_gpus"),
        ("list_byid_disks", handlers, "list_byid_disks"),
        ("list_phys_disks", handlers, "list_phys_disks"),
        ("_node_backup_files", handlers, "_node_backup_files"),
        ("list_disk_storages", handlers, "list_disk_storages"),
        ("list_image_storages", handlers, "list_image_storages"),
        ("list_backup_storages", handlers, "list_backup_storages"),
        ("list_rootdir_storages", handlers, "_rootdir_storages_local"),
        ("list_storage_options", probe.backend, "list_storage_list"),
    ],
)
def test_explicit_local_node_resolves_function_owner(monkeypatch, name, module, attr):
    collector = Mock(return_value=[{"source": attr}])
    monkeypatch.setattr(module, attr, collector)
    monkeypatch.setattr(probe, "_collect_source", lambda: pytest.fail("local call loaded remote code"))

    assert probe._run_func_on_node("pve-local", name) == [{"source": attr}]
    collector.assert_called_once_with()


@pytest.mark.parametrize("node", [None, "", "pve-local"])
def test_disk_entrypoint_preserves_local_classification(monkeypatch, node):
    def lsblk(command, **kwargs):
        assert command == ["lsblk", "-dn", "-o", "NAME,TYPE,SIZE,MODEL"]
        return subprocess.CompletedProcess(command, 0, "sda disk 100G system\nsdb disk 500G data\n")

    monkeypatch.setattr(handlers.subprocess, "run", lsblk)
    monkeypatch.setattr(handlers, "_pve_system_disk_names", lambda: {"sda"})
    monkeypatch.setattr(handlers, "_pve_storage_disk_names", lambda: {"sdb"})
    monkeypatch.setattr(handlers, "_other_os_disk_names", lambda: set())

    disks = handlers.list_phys_disks(node)

    assert [(disk["name"], disk["pve_system"], disk["pve_data"]) for disk in disks] == [
        ("sda", True, False), ("sdb", False, True),
    ]
    assert all(disk["host_used"] for disk in disks)


@pytest.mark.parametrize("node", [None, "", "pve-local"])
def test_lxc_storage_alias_preserves_filter(monkeypatch, node):
    monkeypatch.setattr(handlers, "_rootdir_storages_local", lambda: [
        {"storage": "local-lvm"}, {"storage": "pvewebtools"},
    ])

    assert handlers.list_lxc_storages(node) == [{"storage": "local-lvm"}]


@pytest.mark.parametrize("node", [None, "", "pve-local"])
@pytest.mark.parametrize("name,args", [
    ("_node_template_cache", ()),
    ("_node_template_cache_delete", ("Ubuntu 24.raw",)),
    ("_node_template_cache_clear", ()),
    ("_node_template_cache_expire", ()),
])
def test_cache_dispatch_resolves_local_module(monkeypatch, node, name, args):
    collector = Mock(return_value={"ok": True})
    monkeypatch.setattr(template_cache, name, collector)
    monkeypatch.setattr(probe, "_collect_source", lambda: pytest.fail("local call loaded remote code"))

    assert template_cache.template_cache_dispatch(node, name, args) == {"ok": True}
    collector.assert_called_once_with(*args)


@pytest.mark.parametrize("node", [None, "", "pve-local"])
def test_local_storage_content_passes_arguments(monkeypatch, node):
    collector = Mock(return_value=[{"volid": "local:iso/test.iso"}])
    monkeypatch.setattr(probe.backend, "list_storage_content", collector)

    assert probe._run_func_on_node_args(node, "list_storage_content", ("local",)) == [
        {"volid": "local:iso/test.iso"},
    ]
    collector.assert_called_once_with("local")


@pytest.mark.parametrize("with_args", [False, True])
def test_remote_dispatch_keeps_ssh_collector(monkeypatch, with_args):
    monkeypatch.setattr(probe, "_collect_source", lambda: "# remote collector")
    monkeypatch.setattr(probe, "_local_collector", lambda name: pytest.fail("remote call used local function"))
    monkeypatch.setattr(probe.backend, "ssh_host_opts", lambda node: ["-p", "2222"])
    monkeypatch.setattr(probe.backend, "ssh_host", lambda node: "192.0.2.10")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"node": "pve-remote"})))
    monkeypatch.setattr(probe.subprocess, "run", run)

    if with_args:
        result = probe._run_func_on_node_args("pve-remote", "list_storage_content", ("local",), timeout=42)
    else:
        result = probe._run_func_on_node("pve-remote", "list_phys_disks", timeout=42)

    assert result == {"node": "pve-remote"}
    command = run.call_args.args[0]
    assert command[0] == "ssh"
    assert command[-5:] == ["-p", "2222", "root@192.0.2.10", "python3", "-"]
    assert run.call_args.kwargs["timeout"] == 42
    source = run.call_args.kwargs["input"]
    assert source.startswith("# remote collector\n")
    assert "list_storage_content(*json.loads(" in source if with_args else "list_phys_disks()" in source


def test_local_collector_registry_targets_exist():
    for module_name, attr in probe._LOCAL_COLLECTORS.values():
        assert callable(getattr(import_module(module_name), attr))
