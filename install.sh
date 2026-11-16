#!/usr/bin/env bash
# pvewebtools（开源版）一键安装脚本
#
# 用法（在发行目录或仓库根，以 root 运行）:
#   ./install.sh            # 装到 /etc/pvewebtools，默认端口 8899
#   ./install.sh 9000       # 指定端口
#   ./install.sh 9000 /opt  # 指定端口与安装目录
#
# 做的事：把程序目录同步到安装目录 → 注册 systemd 服务（开机自启 + 崩溃自动拉起）
#        → 创建专用登录账号 pvewebtools@pve → 自动探测证书（有证书则启用 HTTPS）。
#
# 升级：直接再跑一次本脚本（`./install.sh`），程序文件被覆盖，端口/账号/证书沿用。
# 卸载：systemctl disable --now pvewebtools && rm -rf /etc/pvewebtools /etc/systemd/system/pvewebtools.service

set -euo pipefail

PORT="${1:-8899}"
DEST="${2:-/etc/pvewebtools}"

SRC="$(cd "$(dirname "$0")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "错误: 请用 root 运行（sudo ./install.sh）" >&2
    exit 1
fi

if [[ ! -f "$SRC/core/web/server.py" ]]; then
    echo "错误: 当前目录不像 pvewebtools 发行包（缺少 core/web/server.py）" >&2
    exit 1
fi

echo "==> 安装目录: $DEST（端口 $PORT）"
mkdir -p "$DEST"

if [[ "$(cd "$SRC" && pwd)" != "$(cd "$DEST" && pwd)" ]]; then
    # 同步程序文件（保留安装目录里不属于程序本体的运行期数据：logs/、config/ 覆盖配置、backup/）
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete \
            --exclude '.git/' --exclude '__pycache__/' --exclude '*.pyc' \
            --exclude 'logs/' --exclude 'backup/' \
            "$SRC/" "$DEST/"
    else
        # 没有 rsync：逐项复制（不删除旧文件，升级时可能残留已下线的模块，但不会导致启动失败）
        (cd "$SRC" && tar -cf - \
            --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
            --exclude='logs' --exclude='backup' .) | (cd "$DEST" && tar -xf -)
    fi
fi

exec "$DEST/scripts/deploy_source.sh" "$PORT"
