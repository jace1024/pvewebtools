"""存储定义解析：/etc/pve/storage.cfg → 目录映射 / storage_path（不跑 pvesh）。"""

import pytest

from core.pve.api import storage as storage_api

CFG = """\
dir: local
\tpath /var/lib/vz
\tcontent iso,vztmpl,backup

nfs: pvewebtools
\texport /mnt/data/pvewebtools
\tserver 192.168.0.108
\tcontent backup,vztmpl

dir: vmimg
\tpath /data/vmimg

nfs: slowstore
\tserver 192.168.0.9
\texport /srv/slowstore

dir: retired
\tpath /data/retired
\tdisable

lvmthin: data
\tvgname pve
\tthinpool data
"""


@pytest.fixture()
def cfg_file(tmp_path, monkeypatch):
    """把 storage.cfg 指到临时文件，并清空 storage_path 缓存，避免用例互相干扰。"""
    p = tmp_path / "storage.cfg"
    p.write_text(CFG, encoding="utf-8")
    monkeypatch.setattr(storage_api, "_STORAGE_CFG_PATH", p)
    storage_api.invalidate_storage_path_cache()
    yield p
    storage_api.invalidate_storage_path_cache()


def test_parse_storage_cfg_sections(cfg_file):
    entries = storage_api._parse_storage_cfg()
    by_sid = {e["storage"]: e for e in entries}
    assert set(by_sid) == {"local", "pvewebtools", "vmimg", "slowstore", "retired", "data"}
    assert by_sid["local"]["type"] == "dir"
    assert by_sid["local"]["path"] == "/var/lib/vz"
    assert by_sid["local"]["content"] == "iso,vztmpl,backup"
    assert by_sid["pvewebtools"]["export"] == "/mnt/data/pvewebtools"
    assert by_sid["retired"]["disable"] == "1"


def test_parse_storage_cfg_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_api, "_STORAGE_CFG_PATH", tmp_path / "nope.cfg")
    assert storage_api._parse_storage_cfg() is None


def test_storage_cfg_dirs_filters_and_defaults(cfg_file):
    dirs = storage_api._storage_cfg_dirs(("images", "import"))
    # images：vmimg 未声明 content → 按 PVE 默认 images,rootdir 计入；
    # local/pvewebtools 显式声明了不含 images 的 content → 不计入；lvmthin 类型与 disable 项被排除
    assert dirs["images"] == {"vmimg": storage_api.Path("/data/vmimg"),
                              "slowstore": storage_api.Path("/mnt/pve/slowstore")}
    # import 不在任何存储的 content 里
    assert dirs["import"] == {}


def test_storage_cfg_dirs_vztmpl(cfg_file):
    dirs = storage_api._storage_cfg_dirs(("vztmpl",))
    assert dirs["vztmpl"] == {
        "local": storage_api.Path("/var/lib/vz"),
        "pvewebtools": storage_api.Path("/mnt/data/pvewebtools"),
    }


def test_storage_dirs_batch_prefers_cfg(cfg_file, monkeypatch):
    """cfg 可读时不得再调用 pvesh（这条链路是「存储下拉很慢」的元凶）。"""

    def boom(*a, **kw):  # pragma: no cover - 一旦被调用说明回退逻辑走错了
        raise AssertionError("不应回退 pvesh")

    monkeypatch.setattr(storage_api, "_run_pvesh", boom)
    dirs = storage_api._storage_dirs_batch(("images", "import"))
    assert "vmimg" in dirs["images"]


def test_storage_path_from_cfg(cfg_file):
    # pvewebtools 的 nfs 存储优先取 export（绕开服务器侧 /mnt/pve 回环挂载）
    assert storage_api.storage_path("pvewebtools") == storage_api.Path("/mnt/data/pvewebtools")
    assert storage_api.storage_path("vmimg") == storage_api.Path("/data/vmimg")
    assert storage_api.storage_path("slowstore") == storage_api.Path("/mnt/pve/slowstore")
    # lvmthin 没有本地路径 / 不存在 → None
    assert storage_api.storage_path("data") is None
    assert storage_api.storage_path("absent") is None


def test_storage_path_cache_and_invalidate(cfg_file):
    assert storage_api.storage_path("vmimg") == storage_api.Path("/data/vmimg")
    # 改配置后：缓存期内仍返回旧值（避免每次上传都重新解析）
    cfg_file.write_text("dir: vmimg\n\tpath /data/vmimg2\n", encoding="utf-8")
    assert storage_api.storage_path("vmimg") == storage_api.Path("/data/vmimg")
    # 存储增删改后调用 invalidate → 立刻读到新路径
    storage_api.invalidate_storage_path_cache("vmimg")
    assert storage_api.storage_path("vmimg") == storage_api.Path("/data/vmimg2")


def test_list_storage_list_from_cfg(cfg_file):
    """「存储内容」下拉：存储定义读 cfg，仍跳过 disable、content 缺省按 PVE 默认补齐。"""
    rows = {r["storage"]: r for r in storage_api.list_storage_list()}
    assert set(rows) == {"local", "pvewebtools", "vmimg", "slowstore", "data"}   # retired 已禁用
    assert rows["local"]["content"] == "iso,vztmpl,backup"
    assert rows["vmimg"]["content"] == "images,rootdir"   # 未声明 → 默认 images,rootdir
    assert rows["data"]["type"] == "lvmthin"


def test_node_available_storages_cached(monkeypatch):
    """active 仍需 pvesh，但同一节点 30s 内复用，避免每个存储下拉都让 PVE 逐存储算 status。"""
    calls = []

    def fake(*a, **kw):
        calls.append(a)
        return [
            {"storage": "local", "active": 1},
            {"storage": "vmimg", "active": 0},      # 本节点无法激活 → 过滤
            {"storage": "off", "disabled": 1},      # 全局禁用 → 过滤
        ]

    monkeypatch.setattr(storage_api, "_run_pvesh", fake)
    storage_api.invalidate_node_available_cache()
    assert storage_api.node_available_storages() == {"local"}
    assert storage_api.node_available_storages() == {"local"}
    assert len(calls) == 1
    storage_api.invalidate_node_available_cache()
    storage_api.node_available_storages()
    assert len(calls) == 2


def test_node_available_storages_failure_not_cached(monkeypatch):
    """探测失败不缓存（保持「宁可多列，也不要把下拉整个清空」的原有语义）。"""
    calls = []

    def boom(*a, **kw):
        calls.append(1)
        raise storage_api.PveError("node down")

    monkeypatch.setattr(storage_api, "_run_pvesh", boom)
    storage_api.invalidate_node_available_cache()
    assert storage_api.node_available_storages() is None
    assert storage_api.node_available_storages() is None
    assert len(calls) == 2


def test_list_storage_cfg_from_cfg(cfg_file):
    """存储管理表格：字段口径与 pvesh 版一致（content/shared/path 的插件默认值要补齐）。"""
    rows = {r["storage"]: r for r in storage_api.list_storage_cfg()}
    assert set(rows) == {"local", "pvewebtools", "vmimg", "slowstore", "retired", "data"}
    assert rows["retired"]["disable"] is True
    assert rows["vmimg"]["content"] == "images,rootdir"
    assert rows["vmimg"]["shared"] is False          # dir 默认不共享
    assert rows["slowstore"]["shared"] is True       # nfs 插件默认共享
    assert rows["slowstore"]["path"] == "/mnt/pve/slowstore"
    assert rows["pvewebtools"]["export"] == "/mnt/data/pvewebtools"
    assert rows["data"]["type"] == "lvmthin" and rows["data"]["path"] == ""


def test_get_storage_cfg_from_cfg(cfg_file):
    """编辑表单回填：返回该存储的全部字段，且数值键/默认值与 pvesh 口径一致。"""
    d = storage_api.get_storage_cfg("pvewebtools")
    assert d["storage"] == "pvewebtools" and d["type"] == "nfs"
    assert d["export"] == "/mnt/data/pvewebtools"
    assert d["server"] == "192.168.0.108"          # 段落里其它键原样保留
    assert d["path"] == "/mnt/pve/pvewebtools"     # 网络存储缺省挂载点
    assert d["shared"] == 1                        # nfs 插件默认共享
    assert d.get("disable", 0) == 0                # 未显式 disable 时与 pvesh 一样不带该键

    d2 = storage_api.get_storage_cfg("retired")
    assert d2["disable"] == 1

    with pytest.raises(storage_api.PveError):
        storage_api.get_storage_cfg("absent")


def test_get_storage_cfg_hides_password(tmp_path, monkeypatch):
    """读 cfg 不得把 CIFS/PBS 的明文口令吐给前端（pvesh 的存储配置接口同样不返回 password）。"""
    p = tmp_path / "storage.cfg"
    p.write_text("cifs: nas\n\tserver 10.0.0.2\n\tshare bak\n\tusername admin\n\tpassword s3cret\n",
                 encoding="utf-8")
    monkeypatch.setattr(storage_api, "_STORAGE_CFG_PATH", p)
    d = storage_api.get_storage_cfg("nas")
    assert "password" not in d
    assert d["username"] == "admin"
    assert d["content"] == "images,rootdir"


def test_list_storage_cfg_falls_back_to_pvesh(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_api, "_STORAGE_CFG_PATH", tmp_path / "nope.cfg")
    monkeypatch.setattr(storage_api, "_run_pvesh",
                        lambda *a, **kw: [{"storage": "local", "type": "dir", "content": "iso",
                                           "path": "/var/lib/vz", "disable": 1, "shared": 1}])
    rows = storage_api.list_storage_cfg()
    assert rows == [{"storage": "local", "type": "dir", "content": "iso", "path": "/var/lib/vz",
                     "server": "", "export": "", "vgname": "", "thinpool": "", "pool": "",
                     "portal": "", "target": "", "datastore": "", "username": "", "fs-name": "",
                     "monhost": "", "disable": True, "shared": True}]


def test_list_guests_single_flight(monkeypatch):
    """缓存过期瞬间多个请求并发：只允许一次 pvesh，后来者复用同一次采集（不再排队等 30s）。"""
    import threading as _threading
    import time as _time

    from core.pve.api import vm as vm_api

    calls = []
    gate = _threading.Event()

    def slow(*_a, **_kw):
        calls.append(1)
        gate.wait(3)
        return [{"vmid": 100, "node": "pve", "type": "qemu", "status": "running"}]

    monkeypatch.setattr(vm_api, "_run_pvesh", slow)
    vm_api._invalidate_guests_cache()
    out = []

    def worker():
        out.append(vm_api.list_guests())

    try:
        t1 = _threading.Thread(target=worker)
        t1.start()
        _time.sleep(0.2)          # 让第一个请求真正进入采集
        t2 = _threading.Thread(target=worker)
        t2.start()
        _time.sleep(0.2)
        gate.set()
        t1.join(6)
        t2.join(6)
        assert len(calls) == 1
        assert len(out) == 2
        assert all(o and o[0]["vmid"] == 100 for o in out)
    finally:
        gate.set()
        vm_api._invalidate_guests_cache()


def test_free_space_times_out_instead_of_blocking(monkeypatch):
    """网络存储的 statvfs 卡住时，_free_space 必须限时放弃（返回 None），不能拖住下拉请求。"""
    import time as _time

    from core.web import handlers

    def _slow(_p):        # 模拟 NFS 卡死：statvfs 一直不返回
        _time.sleep(5)

    monkeypatch.setattr(handlers.shutil, "disk_usage", _slow)
    t0 = _time.time()
    assert handlers._free_space("/nonexistent/a/b", layers=1, timeout=0.2) is None
    assert _time.time() - t0 < 2.0

