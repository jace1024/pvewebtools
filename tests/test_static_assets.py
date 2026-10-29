"""静态资源装配检查（M3/T3.5）。

前端没有构建步骤、也没有 JS 测试（开发机没有 node），所以「拆分后还能不能跑」只能靠装配
层面的自动检查兜底。这里钉四件事：

1. index.html 引用的每个本地 js/css 都真实存在——防改名 / 搬家漏改（那是「页面白屏」级故障）；
2. 这些引用都被首页注入 ``?v=``——防漏注入后浏览器一直吃旧缓存（「改了代码不生效」）；
3. 每个 js 的**顶层声明名互不重复**——防搬迁时把同一段代码复制进两个文件，后者静默覆盖前者；
4. index.html 内联事件里调用的函数都能在某个 js 里找到定义——内联 ``onclick`` 依赖全局函数名，
   漏搬一个就是「按钮点了没反应」，且只在用户点击时才暴露。

第 3、4 条是 M3 拆分期间最有价值的网：JS 没有编译期，重复声明不报错、缺函数也不报错。
"""

import re
from pathlib import Path

from core.web.static_srv import STATIC_DIR, local_asset_refs

# 顶层声明：只匹配行首（嵌套函数 / 块内声明都有缩进，天然排除）
_JS_DECL_RES = (
    re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.M),
    re.compile(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)", re.M),
)

# 内联事件处理器：onclick / onchange / oninput / …（属性值里可能有多条语句）
_INLINE_HANDLER = re.compile(r'\son(?:click|change|input|submit|keydown|keyup|focus|blur)="([^"]+)"')

# index.html 内联里出现的语言内建 / 关键字，不是我们定义的函数
_INLINE_BUILTINS = {"if", "return", "this", "event", "String", "Number", "parseInt", "parseFloat"}


def _js_files():
    return sorted(STATIC_DIR.rglob("*.js"))


def _top_level_decls(src):
    out = set()
    for rx in _JS_DECL_RES:
        out.update(rx.findall(src))
    return out


def test_index_references_exist():
    """index.html 引用的每个本地 js/css 都必须存在。"""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    refs = local_asset_refs(html)
    assert refs, "index.html 里没扫到任何本地 js/css 引用，检查 _ASSET_REF 是否失效"
    missing = [r for r in refs if not (STATIC_DIR / r).is_file()]
    assert not missing, "index.html 引用了不存在的资源：%s" % missing


def test_index_assets_are_versioned(http):
    """首页返回的 HTML 里，每个本地 js/css 引用都要带 ?v=（多文件拆分后尤其重要）。"""
    status, _headers, body = http("/")
    assert status == 200
    html = body.decode("utf-8")
    refs = local_asset_refs(html)
    assert refs, "首页 HTML 里没扫到静态资源引用"
    unversioned = [r for r in refs if ('"%s?v=' % r) not in html]
    assert not unversioned, "这些资源没被注入版本号：%s" % unversioned


def test_local_asset_refs_ignores_non_local():
    """外链与 data URI 不能被当成需要注入版本的本地资源。"""
    html = ('<script src="https://cdn.example.com/x.js"></script>'
            '<script src="//cdn.example.com/y.js"></script>'
            '<link href="data:text/css,body%7B%7D">'
            '<script src="js/api.js"></script>')
    assert local_asset_refs(html) == ["js/api.js"]


def test_js_top_level_declarations_are_unique():
    """同一顶层名字不得在两个 js 文件里各声明一次（后者会静默覆盖前者）。"""
    owner, dupes = {}, []
    for path in _js_files():
        for name in _top_level_decls(path.read_text(encoding="utf-8")):
            if name in owner and owner[name] != path.name:
                dupes.append("%s：%s 与 %s" % (name, owner[name], path.name))
            owner.setdefault(name, path.name)
    assert not dupes, "顶层声明重名：%s" % dupes


def test_inline_handlers_reference_defined_functions():
    """index.html 内联事件调用的函数必须在某个 js 里定义过。"""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    defined = set()
    for path in _js_files():
        defined |= _top_level_decls(path.read_text(encoding="utf-8"))

    called = set()
    for handler in _INLINE_HANDLER.findall(html):
        called.update(re.findall(r"([A-Za-z_$][\w$]*)\s*\(", handler))

    unknown = sorted(called - defined - _INLINE_BUILTINS)
    assert not unknown, "内联事件调用了未定义的函数：%s" % unknown


def test_all_referenced_assets_are_served(http):
    """首页引用的每个本地资源都要真能取到（含 js/ 子目录）。

    拆分后脚本从单文件变成 `js/xxx.js` 子路径——静态服务若没覆盖子目录，页面会白屏而
    首页本身仍返回 200，只看首页状态码发现不了。
    """
    status, _headers, body = http("/")
    assert status == 200
    refs = local_asset_refs(body.decode("utf-8"))
    assert refs, "首页 HTML 里没扫到静态资源引用"
    for ref in refs:
        st, _h, payload = http("/" + ref)
        assert st == 200, "%s 取不到（HTTP %s）" % (ref, st)
        assert payload, "%s 是空文件" % ref


def test_all_js_files_are_wired_into_index():
    """反向检查：static 目录下的 js 必须都被 index.html 引用。

    拆分时最容易犯的错是「文件建好了、script 标签忘了加」——那一整个模块的函数都不存在，
    但页面上只表现为个别按钮失效，很难定位。
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    refs = set(local_asset_refs(html))
    orphan = sorted(p.relative_to(STATIC_DIR).as_posix() for p in _js_files()
                    if p.relative_to(STATIC_DIR).as_posix() not in refs)
    assert not orphan, "这些 js 没被 index.html 引用：%s" % orphan
