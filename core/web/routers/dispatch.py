"""路由分发协议：路径段 → 处理入口的查表分发。

各路由域模块（``core/web/routers/<domain>.py``）只声明数据::

    HANDLERS = {
        "get":  {"guests/<vmid>/snapshots": "_handle_guest_snapshots"},
        "post": {"license/token": "_add_license_token"},
    }

键是**路径模板**：``/api/`` 之后的路径，段以 ``/`` 分隔。``<name>`` 形式的段匹配
任意非空段并捕获为参数，其余段必须逐字相等；模板段数必须与请求段数相同。
值的**目标**有两种形态：

- ``str``：``ApiHandler`` 上的方法名，**无参**调用（``"options" -> "_handle_options"``）。
  用字符串而不是直接引用，是为了让域模块不必 import ``server``（否则循环导入）；
  代价是「方法名不存在」要等到调用时才暴露，由 ``tests/test_routers.py`` 的
  「目标必须在 ApiHandler 上存在」用例兜底。
- ``callable``：``fn(handler, params)``，``params`` 是模板捕获到的参数字典
  （无参模板为空 dict）。

匹配语义与旧 if 链保持一致：模板已含段数约束，不同段数天然互斥；同段数内精确
模板走 O(1) 字典，带参模板按**声明顺序**在其段数桶内依次尝试——所以「更严格的
模板必须声明在更宽松的模板之前」（旧 if 链的隐式顺序依赖，见 POST ``guests`` 的
``<action>`` 通配项）。重复登记（同方法同模板）直接抛错，避免静默覆盖。

本模块参与 Cython 编译；``routers/__init__.py`` 是明文薄聚合层，两者分工固定。
"""

__all__ = ["Table", "dispatch", "match", "merge"]


def _segments(template):
    """把模板切成 ``[(is_param, text)]``；模板非法时抛 ``ValueError``。

    ``<name>`` 必须整段占满（``a<b`` / ``<a`` / ``<>`` 都非法），避免出现
    「半字面半通配」这种没法用一段字符串表达的匹配语义。
    """
    out = []
    for seg in template.split("/"):
        if not seg:
            raise ValueError("路由模板含空段（多余的 /）：%r" % (template,))
        if "<" in seg or ">" in seg:
            name = seg[1:-1]
            if not (seg.startswith("<") and seg.endswith(">")) or not name or "<" in name or ">" in name:
                raise ValueError("路由模板的 <> 必须整段占满且非空：%r" % (template,))
            out.append((True, name))
        else:
            out.append((False, seg))
    if not out:
        raise ValueError("路由模板为空：%r" % (template,))
    return out


def _bind(segs, parts):
    """把已切好的模板段与请求段绑定；任一字面段不符返回 ``None``。"""
    params = {}
    for (is_param, text), got in zip(segs, parts):
        if is_param:
            params[text] = got
        elif text != got:
            return None
    return params


def match(template, parts):
    """模板匹配：命中返回参数字典（无参时为空 dict），未命中返回 ``None``。

    注意空的参数字典是 falsy，调用方必须用 ``is not None`` 判断命中。
    """
    segs = _segments(template)
    if len(segs) != len(parts):
        return None
    return _bind(segs, parts)


def _check_target(template, target):
    """校验目标形态（早失败：登记期就炸，而不是等某个请求打过来）。"""
    if isinstance(target, str):
        if not target:
            raise ValueError("路由目标方法名不能为空：%r" % (template,))
        return target
    if callable(target):
        return target
    raise TypeError("路由目标必须是方法名或可调用对象：%r -> %r" % (template, target))


class Table:
    """一个 HTTP 方法的路由表：精确路径 O(1)，带参模板按段数分桶顺序扫描。"""

    __slots__ = ("_exact", "_patterns")

    def __init__(self):
        self._exact = {}      # tuple(段) -> 目标
        self._patterns = {}   # 段数 -> [(已切段, 目标)]，桶内保声明顺序

    def add(self, template, target):
        segs = _segments(template)
        if all(not is_param for is_param, _ in segs):
            key = tuple(text for _, text in segs)
            if key in self._exact:
                raise ValueError("路由重复登记：/%s" % "/".join(key))
            self._exact[key] = _check_target(template, target)
            return
        bucket = self._patterns.setdefault(len(segs), [])
        if any(segs == old for old, _ in bucket):
            raise ValueError("路由重复登记：%s" % template)
        bucket.append((segs, _check_target(template, target)))

    def find(self, parts):
        """返回 ``(目标, 参数字典)``；未命中返回 ``(None, None)``。"""
        target = self._exact.get(tuple(parts))
        if target is not None:
            return target, {}
        for segs, tgt in self._patterns.get(len(parts), ()):
            params = _bind(segs, parts)
            if params is not None:
                return tgt, params
        return None, None

    def routes(self):
        """返回 ``[(模板, 目标)]``，供诊断与测试（顺序不保证）。"""
        out = [("/".join(key), target) for key, target in self._exact.items()]
        out += [("/".join(("<%s>" % text) if is_param else text for is_param, text in segs), target)
                for bucket in self._patterns.values() for segs, target in bucket]
        return out


def merge(*modules):
    """把多个域模块的 ``HANDLERS`` 合并成 ``{方法: Table}``。

    重复登记（两个域模块声明同方法同模板）直接抛错——这通常是迁移时「旧分支没删」
    或「两个域都想接管同一路径」，静默覆盖会变成很难查的路由丢失。
    """
    tables = {}
    for mod in modules:
        handlers = getattr(mod, "HANDLERS", None) or {}
        for method, table in handlers.items():
            dst = tables.get(method)
            if dst is None:
                dst = tables[method] = Table()
            for template, target in table.items():
                dst.add(template, target)
    return tables


def dispatch(handler, method, parts, tables):
    """查表分发：命中即调用目标并返回 ``True``（响应已由目标回写），未命中返回 ``False``。

    ``parts`` 为 ``None`` 或空（``/api/`` 本身）时直接判未命中——``do_DELETE`` 会带着
    ``None`` 进来（见其无 ``_api_parts`` 结果检查），不能在这里抛异常。
    """
    if not parts:
        return False
    table = tables.get(method)
    if table is None:
        return False
    target, params = table.find(parts)
    if target is None:
        return False
    if isinstance(target, str):
        getattr(handler, target)()
    else:
        target(handler, params)
    return True
