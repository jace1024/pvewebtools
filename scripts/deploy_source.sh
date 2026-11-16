#!/usr/bin/env bash
# pvewebtools（开源版）源码模式部署脚本（在 PVE 主机上以 root 运行）
#
# 用法:
#   ./scripts/deploy_source.sh [端口]        # 默认端口 8899
#
# 效果:
#   - 注册 pvewebtools.service 并开机自启（ExecStart 用 python3 -m core.web.server）
#   - 进程崩溃或被 kill 后自动拉起（Restart=on-failure）
#   - 部署前自动清理手工运行的旧进程，避免端口占用
#
# 常用管理命令:
#   systemctl status  pvewebtools    # 查看状态
#   systemctl stop    pvewebtools    # 停止（用 kill 停不掉，会被自动拉起）
#   systemctl restart pvewebtools    # 重启
#   journalctl -u pvewebtools -f     # 跟踪日志
#
# 说明: 本脚本位于 <仓库根>/scripts/，服务的工作目录取仓库根——
# `python3 -m 包路径` 以工作目录为 sys.path[0]，包才能被导入。

set -euo pipefail

PORT="${1:-8899}"
UNIT="pvewebtools.service"
UNIT_FILE="/etc/systemd/system/$UNIT"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKDIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# 包入口（相对仓库根）
PKG_ENTRY="core/web/server.py"

# ---- 前置检查 ----
if [[ $EUID -ne 0 ]]; then
    echo "错误: 请用 root 运行（sudo ./scripts/deploy_source.sh）" >&2
    exit 1
fi

PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
    echo "错误: 未找到 python3" >&2
    exit 1
fi

if ! command -v pvesh >/dev/null 2>&1; then
    echo "错误: 未找到 pvesh 命令，本工具仅限 PVE 宿主机部署" >&2
    exit 1
fi

if [[ ! -f "$WORKDIR/$PKG_ENTRY" ]]; then
    echo "错误: 未找到 $PKG_ENTRY，请在 pvewebtools 源码仓库内执行本脚本" >&2
    exit 1
fi

if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
    echo "错误: 非法端口: $PORT" >&2
    exit 1
fi

# ---- 清理手工运行的旧进程（systemd 托管的交给 restart 处理） ----
if pgrep -f "core\.web\.server" >/dev/null 2>&1 || pgrep -f "python3 .*server\.py" >/dev/null 2>&1; then
    echo "发现正在运行的旧进程，先停止..."
    systemctl stop "$UNIT" 2>/dev/null || true
    pkill -f "core\.web\.server" 2>/dev/null || true
    pkill -f "python3 .*server\.py" 2>/dev/null || true
    sleep 1
fi

# ---- 清理旧版服务单元（历史改名升级的迁移处理） ----
if [[ -f /etc/systemd/system/pve-webtool.service ]]; then
    echo "检测到旧版 pve-webtool.service，移除并迁移到 $UNIT ..."
    systemctl disable --now pve-webtool.service 2>/dev/null || true
    rm -f /etc/systemd/system/pve-webtool.service
fi

# ---- 专用登录账号：pvewebtools@pve，初始密码 pvewebtools，首次登录强制改密 ----
ALLOW_LINE=""
if command -v pveum >/dev/null 2>&1; then
    if pveum user list 2>/dev/null | awk '{print $1}' | grep -qx 'pvewebtools@pve'; then
        echo "==> 登录账号 pvewebtools@pve 已存在（保留原密码）"
    else
        if pveum user add pvewebtools@pve --password pvewebtools \
             --comment 'PVE WebTools Web 登录（首次登录须改密）' >/dev/null 2>&1; then
            mkdir -p /etc/pvewebtools
            printf '%s\n' 'pvewebtools@pve' > /etc/pvewebtools/force_pw_change
            chmod 600 /etc/pvewebtools/force_pw_change 2>/dev/null || true
            echo "==> 已创建登录账号 pvewebtools@pve（初始密码 pvewebtools，首次登录必须修改）"
            echo "!!! 安全提示：初始密码为公开默认值，首次登录前请勿把端口暴露到不可信网络 !!!"
        else
            echo "警告：创建 pvewebtools@pve 失败（可稍后手动 pveum user add）" >&2
        fi
    fi
    if pveum user list 2>/dev/null | awk '{print $1}' | grep -qx 'pvewebtools@pve'; then
        ALLOW_LINE="Environment=PVE_WEBTOOLS_ALLOW_USERS=pvewebtools@pve
Environment=PVE_WEBTOOLS_DEFAULT_REALM=pve"
    fi
else
    echo "警告：未找到 pveum，跳过专用账号创建（将允许任意 PVE 账号登录）" >&2
fi

# ---- 证书探测：有证书默认 HTTPS（项目 tls/ 或 certs/ 或 PVE 自带），否则回退 HTTP ----
TLS_ARGS=""
for _d in "$WORKDIR/tls" "$WORKDIR/certs"; do
    if { [ -f "$_d/fullchain.pem" ] && [ -f "$_d/privkey.pem" ]; } \
       || { [ -f "$_d/cert.pem" ] && [ -f "$_d/key.pem" ]; }; then
        TLS_ARGS="--tls"; break
    fi
done
if [ -z "$TLS_ARGS" ] && [ -f /etc/pve/local/pve-ssl.pem ] && [ -f /etc/pve/local/pve-ssl.key ]; then
    TLS_ARGS="--tls"
fi
echo "==> HTTPS: $([ -n "$TLS_ARGS" ] && echo '启用（自动证书）' || echo '未启用（未找到证书，走 HTTP）')"

# ---- 写入 systemd 单元 ----
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=PVE WebTools (open source)
After=network-online.target pve-cluster.service

[Service]
WorkingDirectory=$WORKDIR
$ALLOW_LINE
ExecStart=$PYTHON -m core.web.server $PORT $TLS_ARGS
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

# ---- 启用并启动 ----
systemctl daemon-reload
systemctl enable --now "$UNIT"
systemctl restart "$UNIT"
sleep 2

# ---- 结果验证 ----
if systemctl is-active --quiet "$UNIT" && ss -ltn | grep -q ":$PORT "; then
    SCHEME="http"
    if [ -n "$TLS_ARGS" ]; then SCHEME="https"; fi
    echo "部署成功: $UNIT 已启动并监听 $PORT 端口（源码模式）"
    echo "访问地址: $SCHEME://$(hostname -I | awk '{print $1}'):$PORT"
else
    echo "部署异常，请查看日志: journalctl -u $UNIT -n 30 --no-pager" >&2
    exit 1
fi
