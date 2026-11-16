"""路由分发协议单测。

覆盖协议本身（模板匹配 / 表合并 / 查表分发 / 通配保序），以及两条「防漏」纪律：
① 路由表里的方法名必须在 ``ApiHandler`` 上真实存在——写错或被删只有请求打过来才 500；
② 已迁入路由表的路径不得再留在 ``server.py`` 的旧 if 链里——迁移没删旧分支会变成
   两处定义、静默漂移（改了一处忘了另一处）。
"""

import re
from pathlib import Path

import pytest

from core.web.routers import TABLES, dispatch
from core.web.routers import node as node_routes
from core.web.routers import vm as vm_routes
from core.web.routers.dispatch import Table, match

ROOT = Path(__file__).resolve().parents[1]


class FakeHandler:
    """只记录目标被调用的情况，不发真响应。"""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))

        return _record


# ---- 模板匹配语义 ----

def test_match_exact():
    assert match("guests", ["guests"]) == {}
    assert match("upload/status", ["upload", "status"]) == {}
    assert match("guests", ["nodes"]) is None


def test_match_captures_params():
    assert match("guests/<vmid>/snapshots", ["guests", "101", "snapshots"]) == {"vmid": "101"}
    assert match("nodes/<node>/reboot", ["nodes", "pve1", "reboot"]) == {"node": "pve1"}


def test_match_requires_same_segment_count():
    assert match("jobs/<id>", ["jobs"]) is None
    assert match("jobs/<id>", ["jobs", "abc", "extra"]) is None


def test_match_is_case_sensitive():
    assert match("demo/status", ["Demo", "status"]) is None


@pytest.mark.parametrize("bad", ["", "a//b", "/a", "a/", "<a", "a>", "<>", "a<b", "<a>b>"])
def test_bad_template_rejected(bad):
    with pytest.raises(ValueError):
        match(bad, ["x"])


# ---- 表合并 ----

def test_table_rejects_duplicate_route():
    exact = Table()
    exact.add("demo/status", "_a")
    with pytest.raises(ValueError):
        exact.add("demo/status", "_b")

    pattern = Table()
    pattern.add("jobs/<id>/kill", "_a")
    with pytest.raises(ValueError):
        pattern.add("jobs/<id>/kill", "_b")


def test_merge_rejects_duplicate_across_modules():
    class ModA:
        HANDLERS = {"get": {"demo/status": "_x"}}

    class ModB:
        HANDLERS = {"get": {"demo/status": "_y"}}

    with pytest.raises(ValueError):
        dispatch.merge(ModA, ModB)


def test_table_rejects_bad_target():
    with pytest.raises(TypeError):
        Table().add("demo/status", 123)
    with pytest.raises(ValueError):
        Table().add("demo/status", "")


# ---- 查表分发 ----

def _demo_tables():
    table = Table()
    table.add("demo/status", "_status")
    table.add("jobs/<id>/kill", "_kill")
    return {"post": table}


def test_dispatch_hit_str_target():
    handler = FakeHandler()
    assert dispatch.dispatch(handler, "post", ["demo", "status"], _demo_tables()) is True
    assert handler.calls == [("_status", (), {})]


def test_dispatch_hit_callable_target_captures_params():
    seen = []

    def _target(handler, params):
        seen.append(params)

    table = Table()
    table.add("guests/<vmid>/snapshots", _target)
    handler = FakeHandler()
    assert dispatch.dispatch(handler, "get", ["guests", "101", "snapshots"], {"get": table}) is True
    assert seen == [{"vmid": "101"}]


def test_dispatch_miss_returns_false():
    handler = FakeHandler()
    assert dispatch.dispatch(handler, "post", ["upgrade", "upload"], _demo_tables()) is False
    assert dispatch.dispatch(handler, "get", ["demo", "status"], _demo_tables()) is False   # 无该方法表
    assert handler.calls == []


def test_dispatch_tolerates_missing_parts():
    """do_DELETE 会把 ``_api_parts()`` 的 None 直接传进来，必须判未命中而不是抛异常。"""
    handler = FakeHandler()
    assert dispatch.dispatch(handler, "post", None, _demo_tables()) is False
    assert dispatch.dispatch(handler, "post", [], _demo_tables()) is False


def test_pattern_bucket_keeps_declaration_order():
    """旧 if 链的顺序依赖（通配项必须排在严格项之后）改由桶内声明顺序承载。"""
    order = []

    def _specific(handler, params):
        order.append("specific")

    def _wildcard(handler, params):
        order.append("wildcard")

    table = Table()
    table.add("guests/<node>/<type>/<vmid>/rename", _specific)
    table.add("guests/<node>/<type>/<vmid>/<action>", _wildcard)
    handler = FakeHandler()
    assert dispatch.dispatch(handler, "post", ["guests", "n", "qemu", "100", "rename"], {"post": table}) is True
    assert order == ["specific"]


# ---- 实际路由表 ----

def test_system_domain_registered():
    target, params = TABLES["get"].find(["options"])
    assert target == "_handle_options"
    assert params == {}


def test_node_domain_registered():
    target, params = TABLES["get"].find(["nodes"])
    assert target == "_handle_nodes"
    assert params == {}

    assert TABLES["post"].find(["node", "join-init"])[0] == "_handle_node_join_init"
    # 整页全局「一键修复」是两段静态路径，不能被 nodes/<node>/... 三段模板抢先匹配
    assert TABLES["post"].find(["nodes", "storage-fix-all"]) == (
        node_routes._storage_fix_all, {})
    for action in ("reboot", "shutdown", "storage-fix", "nfs-server-fix"):
        target, params = TABLES["post"].find(["nodes", "pve1", action])
        assert callable(target), action
        assert params == {"node": "pve1"}, action


def test_vm_domain_registered():
    """vm 域已自包含（实现迁进 routers/vm.py），十个目标都是域内函数。"""
    assert TABLES["get"].find(["guests"])[0] is vm_routes._guests
    assert TABLES["get"].find(["deploy", "template-config"])[0] is vm_routes._deploy_template_config
    assert TABLES["get"].find(["deploy", "cloud-defaults"])[0] is vm_routes._deploy_cloud_defaults

    target, params = TABLES["get"].find(["guests", "101", "snapshots"])
    assert target is vm_routes._snapshots
    assert params == {"vmid": "101"}

    for sub, expected in (("config", vm_routes._config), ("status", vm_routes._status)):
        target, params = TABLES["get"].find(["guests", "pve1", "qemu", "100", sub])
        assert target is expected, sub
        assert params == {"node": "pve1", "type": "qemu", "vmid": "100"}


def test_vm_guest_action_wildcard_does_not_shadow_specific_actions():
    """通配项 guests/<node>/<type>/<vmid>/<action> 不得吞掉 onboot/description/rename。

    旧 if 链靠书写顺序保证（通配项必须排在三者之后）；新表靠「精确表先于带参模板命中」
    （见 routers/dispatch.py 的 Table.find），所以这里对同段数直接断言各自命中。
    """
    node, gtype, vmid = "pve1", "qemu", "100"
    for action, expected in (("onboot", vm_routes._onboot),
                             ("description", vm_routes._description),
                             ("rename", vm_routes._rename)):
        target, params = TABLES["post"].find(["guests", node, gtype, vmid, action])
        assert target is expected, action
        assert params == {"node": node, "type": gtype, "vmid": vmid}, action

    target, params = TABLES["post"].find(["guests", node, gtype, vmid, "reboot"])
    assert target is vm_routes._action
    assert params == {"node": node, "type": gtype, "vmid": vmid, "action": "reboot"}


def test_storage_domain_registered():
    """storage 域：全字面项落精确表，storage-cfg/<s> 与 storage/<s>/content 落带参模板。"""
    exact = (("get", ["storages"]), ("get", ["storages", "images"]), ("get", ["storages", "rootdir"]),
             ("get", ["storage", "move-targets"]), ("get", ["storage", "download"]),
             ("get", ["storage-cfg"]), ("get", ["disks"]), ("get", ["disks", "byid"]),
             ("get", ["disks", "storages"]), ("get", ["disks", "vm"]), ("get", ["disks", "imported"]),
             ("get", ["disks", "exported"]), ("get", ["disks", "download"]),
             ("post", ["storage", "move-content"]), ("post", ["storage", "move"]),
             ("post", ["storage-cfg"]), ("post", ["disks", "upload"]), ("post", ["disks", "delete"]),
             ("delete", ["storage"]))
    for method, path in exact:
        target, params = TABLES[method].find(path)
        assert isinstance(target, str), "%s %s" % (method, path)
        assert params == {}, "%s %s" % (method, path)

    for method, path in (("get", ["storage", "local", "content"]),
                         ("get", ["storage-cfg", "local"]),
                         ("put", ["storage-cfg", "local"]),
                         ("delete", ["storage-cfg", "local"])):
        target, params = TABLES[method].find(path)
        assert callable(target), "%s %s" % (method, path)
        assert params == {"storage": "local"}, "%s %s" % (method, path)

    # 3 段带参模板只匹配 content 尾段，未知尾段不得被吞
    assert TABLES["get"].find(["storage", "local", "bogus"])[0] is None


def test_network_domain_registered():
    """network 域 13 条路由都是无路径参数的精确项，全部走精确表并以方法名登记。"""
    paths = (["network", "bridges"], ["network", "vlans"], ["network", "interfaces"],
             ["network", "backups"], ["pci"], ["pci", "nics"], ["pci", "gpus"],
             ["pci", "generic"], ["pci", "vfio"], ["usb", "devices"], ["kernels"],
             ["kernels", "available"], ["grub", "backups"])
    for path in paths:
        target, params = TABLES["get"].find(path)
        assert isinstance(target, str), path
        assert params == {}, path


def test_transfer_domain_registered():
    """transfer 域 17 条路由均为无路径参数的精确项。"""
    get_paths = (["upload", "status"], ["upload", "verify"], ["backup", "storages"],
                 ["backup", "files"], ["backup", "download"], ["template-cache", "nodes"],
                 ["template-cache"], ["vbios", "roms"], ["docker-versions"])
    post_paths = (["backup", "upload"], ["vbios", "upload"], ["vbios", "sync"],
                  ["upload", "register"], ["template-cache", "warmup"], ["template-cache", "expire"])
    delete_paths = (["template-cache"], ["backup"], ["vbios"])

    for method, paths in (("get", get_paths), ("post", post_paths), ("delete", delete_paths)):
        for path in paths:
            target, params = TABLES[method].find(path)
            assert isinstance(target, str), "%s %s" % (method, path)
            assert params == {}, "%s %s" % (method, path)


def test_download_domain_registered():
    """download 域：cloud-image 与 lxc-template 共 20 条无参精确路由。

    其中 cloud-image/presets 在 GET 与 POST 下各有一条，处理入口不同。
    """
    get_paths = (["cloud-image", "status"], ["cloud-image", "list"], ["cloud-image", "presets"],
                 ["cloud-image", "mirrors"], ["cloud-image", "storages"],
                 ["lxc-template", "available"], ["lxc-template", "downloaded"],
                 ["lxc-template", "storages"], ["lxc-template", "cluster-sources"],
                 ["lxc-template", "status"], ["lxc-template", "list"])
    post_paths = (["cloud-image", "download"], ["cloud-image", "presets"],
                  ["cloud-image", "presets", "check"], ["cloud-image", "presets", "reset"],
                  ["cloud-image", "cancel"], ["lxc-template", "upload"],
                  ["lxc-template", "download"], ["lxc-template", "batch-source-swap"],
                  ["lxc-template", "cancel"])

    for path in get_paths:
        target, params = TABLES["get"].find(path)
        assert isinstance(target, str), path
        assert params == {}, path
    for path in post_paths:
        target, params = TABLES["post"].find(path)
        assert isinstance(target, str), path
        assert params == {}, path

    # 同路径不同方法：GET 读预设源、POST 覆盖写入，入口必须不同
    assert TABLES["get"].find(["cloud-image", "presets"])[0] == "_handle_cloud_image_presets_get"
    assert TABLES["post"].find(["cloud-image", "presets"])[0] == "_handle_cloud_image_presets_set"


def test_toolbox_domain_registered():
    """toolbox 域：工具清单/运行 + 任务队列；含 jobs/<job_id> 与 /stream 的段数区分。"""
    assert TABLES["get"].find(["tools"])[0] == "_handle_tools"
    assert TABLES["get"].find(["jobs"])[0] == "_handle_jobs"
    assert TABLES["post"].find(["jobs", "clear"])[0] == "_handle_jobs_clear"

    cases = (("get", ["jobs", "abc"], "job_id"),
             ("get", ["jobs", "abc", "stream"], "job_id"),
             ("post", ["jobs", "abc", "kill"], "job_id"),
             ("delete", ["jobs", "abc"], "job_id"),
             ("post", ["tools", "clean-tmp", "run"], "tool_id"))
    for method, path, key in cases:
        target, params = TABLES[method].find(path)
        assert callable(target), "%s %s" % (method, path)
        assert params == {key: path[1]}, "%s %s" % (method, path)


def test_cluster_domain_registered():
    """cluster 域以方法名登记。"""
    for path in (["cluster", "info"], ["cluster", "quorum"], ["edition"], ["schedules"]):
        target, params = TABLES["get"].find(path)
        assert isinstance(target, str), path
        assert params == {}, path

    for path in (["cluster", "create"], ["cluster", "join"],
                 ["cluster", "quorum", "degrade"], ["cluster", "quorum", "restore"]):
        target, params = TABLES["post"].find(path)
        assert isinstance(target, str), path
        assert params == {}, path

    # cluster/quorum（2 段）与 cluster/quorum/{degrade,restore}（3 段）互不干扰
    assert TABLES["get"].find(["cluster", "quorum", "degrade"])[0] is None


def test_dispatch_methods_have_no_business_branches():
    """T2.6 验收：四个 ``_handle_*`` 只做「查表 + 兜底 404」，不再有业务分支。

    判据是 AST 层面的：方法体只允许「if 查表分支」与「表达式语句」（注释/dostring/
    ``self._error(...)``）。一旦出现赋值、``return self._xxx()`` 等语句，就说明还有域
    没有迁到路由表——这是 M2 剩余的活，不该被悄悄合进来。

    四个方法住在 ``core/web/routing.py`` 的 ``RoutingMixin`` 里。
    """
    import ast

    tree = ast.parse((ROOT / "core/web/routing.py").read_text(encoding="utf-8"))
    targets = {"_handle_get", "_handle_post", "_handle_put", "_handle_delete"}
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "RoutingMixin":
            for fn in node.body:
                if isinstance(fn, ast.FunctionDef) and fn.name in targets:
                    found[fn.name] = fn
    assert set(found) == targets, "未找到全部分发方法：%s" % sorted(targets - set(found))

    offenders = []
    for name, fn in found.items():
        for stmt in fn.body:
            if isinstance(stmt, (ast.If, ast.Expr)):
                continue
            offenders.append("%s:%d %s" % (name, stmt.lineno, type(stmt).__name__))
    assert not offenders, "分发方法里还有未迁走的业务语句：%s" % offenders


def test_api_handler_has_no_duplicate_method_definitions():
    """ApiHandler 类体内同名方法只能定义一次。

    Python 允许重复定义但后者会静默覆盖前者——迁入「处理入口」方法时最容易踩到
    （例如给一个已有实现的方法再补一个同名的壳），出错时没有任何报错。
    """
    import ast

    tree = ast.parse((ROOT / "core/web/server.py").read_text(encoding="utf-8"))
    methods = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ApiHandler":
            methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert methods, "未在 server.py 里找到 ApiHandler 类体"
    dupes = sorted({name for name in methods if methods.count(name) > 1})
    assert not dupes, "ApiHandler 内重复定义的方法（后者会覆盖前者）：%s" % dupes


def test_registered_method_targets_exist_on_api_handler():
    from core.web.server import ApiHandler

    missing = []
    for method, table in TABLES.items():
        for template, target in table.routes():
            if isinstance(target, str) and not hasattr(ApiHandler, target):
                missing.append("%s %s -> %s" % (method.upper(), template, target))
    assert not missing, "路由表登记了 ApiHandler 上不存在的方法：%s" % missing


def test_route_targets_only_touch_existing_handler_attributes():
    """路由目标只允许访问 ``ApiHandler`` 上真实存在的属性。

    字符串目标由 ``test_registered_method_targets_exist_on_api_handler`` 用反射校验；域模块里
    **函数**（转发函数、以及像 auth 那样把实现直接放进域模块的函数）中的 ``handler._xxx``
    则是另一层间接，拼错只会在请求打过来时 500。这里用 AST 静态提取，不真的调用它们
    ——实现函数会读 body、碰真实资源，调用会带来副作用。
    """
    import ast
    import inspect

    from core.web import routers as routers_pkg
    from core.web.server import ApiHandler

    # BaseHTTPRequestHandler 在 __init__ 里挂的实例属性不在类上，dir(ApiHandler) 取不到
    instance_attrs = {"path", "headers", "rfile", "wfile", "request", "client_address",
                      "server", "close_connection", "command"}
    known = set(dir(ApiHandler)) | instance_attrs
    missing = []

    for mod in routers_pkg._ROUTERS:
        tree = ast.parse(inspect.getsource(mod))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "handler"
                    and node.attr not in known):
                missing.append("%s -> handler.%s" % (mod.__name__, node.attr))

    assert not missing, "路由目标访问了 ApiHandler 上不存在的属性：%s" % sorted(set(missing))


def test_migrated_routes_removed_from_old_chain():
    """已迁路径不得在 server.py 旧链里重复定义。

    按「方法无关」的近似口径比对精确路径：同一路径在不同 HTTP 方法下共存、且只迁了
    其中一个方法时，需要为它加白名单（当前无此情形）。
    """
    src = (ROOT / "core/web/server.py").read_text(encoding="utf-8")
    old_chain = set()
    for m in re.finditer(r"parts == \[([^\]]*)\]", src):
        old_chain.add(tuple(re.findall(r'"([^"]*)"', m.group(1))))

    migrated = {tuple(template.split("/")) for table in TABLES.values()
                for template, _target in table.routes() if "<" not in template}
    overlap = migrated & old_chain
    assert not overlap, "已迁入路由表但仍留在 server.py 旧链里：%s" % sorted(overlap)
