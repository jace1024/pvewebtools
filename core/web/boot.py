"""进程启动入口：命令行参数解析、pvesh 门槛检查、后台线程拉起（节点缓存刷新）、HTTP 服务启动与优雅退出。

与 HTTP 请求路径无关，故独立成模块；由 core/web/server.py 的 __main__ 分支转发到这里。
"""

import os
import ssl
import sys
import threading

from core.pve import pve_api, storage_provision
from core.web.maintenance import _cleanup_orphan_job_tmps, _cleanup_orphan_parts, _nodes_refresh_loop, _resume_incomplete_uploads, _sweep_stale_chunks
from core.web import server as web_server
from core.web.vbios import init_vbios_local
from core.web.server import DEFAULT_PORT, ApiHandler, SilentHTTPServer

# PVE 自带证书（每台宿主机都有，由 PVE 集群 CA 签发）；--tls 直接用它。
_PVE_SSL_CERT = "/etc/pve/local/pve-ssl.pem"
_PVE_SSL_KEY = "/etc/pve/local/pve-ssl.key"


def _parse_cli(argv):
    """解析命令行，返回 (port, tls_cert, tls_key, tls_auto)。

    --tls                启用 HTTPS，证书自动查找（见 _resolve_tls_certs）
    --tls-cert PATH      指定证书链（PEM），与 --tls-key 成对
    --tls-key PATH       指定私钥（PEM）
    其它非 '-' 开头的参数按端口解析（忽略 --debug/--no-auth 等开关）。
    """
    port = None
    tls_cert = tls_key = None
    tls_auto = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--tls":
            tls_auto = True
        elif a == "--tls-cert" and i + 1 < len(argv):
            tls_cert = argv[i + 1]
            i += 1
        elif a == "--tls-key" and i + 1 < len(argv):
            tls_key = argv[i + 1]
            i += 1
        elif not a.startswith("-"):
            try:
                port = int(a)
            except ValueError:
                pass
        i += 1
    return port, tls_cert, tls_key, tls_auto


def _resolve_tls_certs():
    """--tls 的证书解析顺序：环境变量 > 项目目录 tls/ 或 certs/ > PVE 自带证书。

    项目目录指当前工作目录（systemd 的 WorkingDirectory 即部署目录）。
    支持文件名：fullchain.pem/privkey.pem 或 cert.pem/key.pem。
    """
    ec = os.environ.get("PVE_WEBTOOLS_TLS_CERT")
    ek = os.environ.get("PVE_WEBTOOLS_TLS_KEY")
    if ec and ek:
        return ec, ek
    cwd = os.getcwd()
    candidates = (
        ("tls", "fullchain.pem", "privkey.pem"),
        ("tls", "cert.pem", "key.pem"),
        ("certs", "fullchain.pem", "privkey.pem"),
        ("certs", "cert.pem", "key.pem"),
    )
    for sub, cp, kp in candidates:
        c = os.path.join(cwd, sub, cp)
        k = os.path.join(cwd, sub, kp)
        if os.path.isfile(c) and os.path.isfile(k):
            return c, k
    return _PVE_SSL_CERT, _PVE_SSL_KEY


def main():
    # --no-auth：关闭登录门禁的破窗通道（极端恢复用，如 PVE 认证不可用时仍可进面板）。
    # 通过环境变量传给运行期（auth_session.disabled 读取），避免跨模块传参。
    if "--no-auth" in sys.argv:
        os.environ["PVE_WEBTOOLS_NO_AUTH"] = "1"
    # 单机版门槛：本工具只能部署在 PVE 宿主机上，没有 pvesh 直接拒启
    if not pve_api.pvesh_available():
        print("错误：未检测到 pvesh，本工具需部署在 PVE 宿主机上运行。")
        print("请将本目录复制到 PVE 主机后执行：./pvewebtools [端口]")
        sys.exit(1)
    # 统一初始化入口：NFS server + pvewebtools 存储注册 + 四子目录 + 旧目录布局迁移
    storage_provision.ensure_storage()
    # vBIOS 本机初始化：迁移旧本机 vBIOS + 重建 /usr/share/kvm 软链（幂等，失败仅告警）
    try:
        init_vbios_local()
    except Exception as e:  # noqa: BLE001
        print(f"[vbios] 初始化异常: {e}")
    port, tls_cert, tls_key, tls_auto = _parse_cli(sys.argv[1:])
    port = port or DEFAULT_PORT
    if tls_auto and not (tls_cert and tls_key):
        tls_cert, tls_key = _resolve_tls_certs()
    if bool(tls_cert) != bool(tls_key):
        print("错误：--tls-cert 与 --tls-key 必须同时提供（或用 --tls 自动查找：项目 tls/ 或 PVE 证书）。")
        sys.exit(1)
    web_server.RUN_PORT = port   # 写在 server 模块上：服务实际监听端口
    try:
        server = SilentHTTPServer(("0.0.0.0", port), ApiHandler)
    except OSError as e:
        if getattr(e, "errno", None) in (98, 48, 10048):  # EADDRINUSE: Linux/macOS/Win
            print(f"错误：端口 {port} 已被占用，可能已有实例在运行。")
            print(f"       排查：ss -tlnp | grep :{port}   或   systemctl status pvewebtools")
        else:
            print(f"错误：无法在端口 {port} 启动服务：{e}")
        sys.exit(1)
    # 调大连接队列（默认仅 5）：前端刷新会并行请求 HTML+JS+CSS+多个 API，
    # 队列太小会被内核拒绝连接导致浏览器报 Failed to fetch
    server.request_queue_size = 128
    # 原生 TLS：直接把监听 socket 包成 TLS（不依赖 Nginx/Caddy）。证书加载失败即拒绝启动。
    if tls_cert and tls_key:
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(tls_cert, tls_key)
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
        except (OSError, ssl.SSLError) as e:
            print(f"错误：加载 TLS 证书失败（cert={tls_cert} key={tls_key}）：{e}")
            sys.exit(1)
        # HTTPS 下会话 Cookie 必须带 Secure
        os.environ["PVE_WEBTOOLS_SECURE_COOKIE"] = "1"
        print(f"已启用 HTTPS（证书：{tls_cert}）")
    # 启动后台预热循环：周期采集节点概览更新缓存，前台请求只读缓存、秒回不阻塞
    threading.Thread(target=_nodes_refresh_loop, daemon=True).start()
    # 先清理旧下载/解压半成品，再恢复上传，避免删除后台合并刚创建的 .part。
    try:
        _cleanup_orphan_parts()
    except Exception:  # noqa: BLE001
        pass
    # 通过持久化的上传元数据恢复分片合并，同时恢复供前端查询的合并状态。
    try:
        _resume_incomplete_uploads()
    except Exception:  # noqa: BLE001
        pass
    # 启动分片残留清理：定时删除上传中断遗留的超时分片目录
    threading.Thread(target=_sweep_stale_chunks, daemon=True).start()
    # 启动兜底：清理服务整体崩溃时来不及回收的任务临时目录 /var/tmp/pvewebtools-job-*
    try:
        _cleanup_orphan_job_tmps()
    except Exception:  # noqa: BLE001
        pass
    scheme = "https" if (tls_cert and tls_key) else "http"
    print(f"PVE Web 管理工具已启动: {scheme}://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


