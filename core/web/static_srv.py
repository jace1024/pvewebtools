"""首页与静态资源服务（结构重构 M2/T2.3 从 server.py 抽出）。

首页读取 + 静态资源 ``?v=<mtime-size>`` 版本注入 + ETag 条件缓存。以 mixin 形式供
``server.ApiHandler`` 多继承；本模块自行从 ``core.paths`` 解析 ``STATIC_DIR``，
**不 import server**（避免循环导入）。``send_head`` / ``_send_static`` 等通用发送逻辑在
``http_util.HttpUtilMixin``。

版本注入（M3/T3.5）：不再硬编码 ``app.js`` / ``style.css`` 两个名字，而是扫描 index.html
引用的**所有**本地 js/css —— 前端拆成多文件后，漏注入的那个会一直被浏览器缓存，表现为
「改了代码页面却不生效」，这类问题既难发现也难排查。外链（路径含 ``:``，如
``https://cdn/x.js``）天然不匹配，不会被误改。
"""

import re

from core import paths

STATIC_DIR = paths.RESOURCE_DIR / "static"

# 本地静态资源引用：<script src="x.js"> / <link href="x.css"> 里的相对路径。
# 只认「不含 : 与 / 开头的相对路径」，因此 http(s):// 与 //cdn 形式的外链不会被注入。
_ASSET_REF = re.compile(r'"([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:js|css))(?:\?[^"]*)?"')


def local_asset_refs(html):
    """index.html 里引用的本地 js/css 相对路径（去重、保持出现顺序）。"""
    return list(dict.fromkeys(m.group(1) for m in _ASSET_REF.finditer(html)))


class StaticMixin:
    """首页与静态资源。"""

    def _serve_index(self):
        """返回 index.html，并给它引用的每个本地 js/css 注入 ``?v=<mtime>-<size>``：
        文件一更新 URL 即变，浏览器必然重新拉取。首页本身用 ETag 条件缓存 + gzip。"""
        idx = STATIC_DIR / "index.html"
        try:
            html = idx.read_text(encoding="utf-8")
            idx_m = int(idx.stat().st_mtime)
        except OSError:
            return self._error("index.html 缺失", 500)
        vers = []

        def _stamp(m):
            name = m.group(1)
            try:
                st = (STATIC_DIR / name).stat()
                ver = "%d-%d" % (int(st.st_mtime), st.st_size)   # mtime+size：内容变版本必变
            except OSError:
                ver = "0"
            vers.append(ver)
            return '"%s?v=%s"' % (name, ver)

        html = _ASSET_REF.sub(_stamp, html)
        body = html.encode("utf-8")
        etag = '"idx-%d-%s"' % (idx_m, "-".join(vers))
        self._send_static(body, "text/html; charset=utf-8", etag, "no-cache")
