#!/usr/bin/env bash
# pvewebtools（开源版）一键安装脚本
#
# 用法（在发行目录或仓库根，以 root 运行）:
#   ./install.sh            # 装到 /etc/pvewebtools，默认端口 8899
#   ./install.sh 9000       # 指定端口
#   ./install.sh 9000 /opt  # 指定端口与安装目录
#
# 也可以不下载仓库直接跑（脚本会自己拉源码；国内可换 ghproxy.net 镜像）:
#   curl -fsSL https://raw.githubusercontent.com/jace1024/pvewebtools/main/install.sh | sudo bash
#
# 做的事：把程序目录同步到安装目录 → 注册 systemd 服务（开机自启 + 崩溃自动拉起）
#        → 创建专用登录账号 pvewebtools@pve → 自动探测证书（有证书则启用 HTTPS）。
#
# 升级：直接再跑一次本脚本（`./install.sh`），程序文件被覆盖，端口/账号/证书沿用。
# 卸载：systemctl disable --now pvewebtools && rm -rf /etc/pvewebtools /etc/systemd/system/pvewebtools.service

set -euo pipefail

PORT="${1:-8899}"
DEST="${2:-/etc/pvewebtools}"

REPO_URL="https://github.com/jace1024/pvewebtools.git"

if [[ $EUID -ne 0 ]]; then
    echo "错误: 请用 root 运行（sudo ./install.sh）" >&2
    exit 1
fi

# ---- 定位源码目录：脚本在完整仓库里就直接用；否则（如 curl | bash）自动拉一份 ----
_self="${BASH_SOURCE[0]:-}"
SRC=""
if [[ -n "$_self" && -f "$_self" ]]; then
    SRC="$(cd "$(dirname "$_self")" && pwd)"
fi

if [[ -z "$SRC" || ! -f "$SRC/core/web/server.py" ]]; then
    if ! command -v git >/dev/null 2>&1; then
        echo "错误: 未找到 git，无法自动获取源码（apt install -y git 后重试，或先把仓库复制到本机）" >&2
        exit 1
    fi
    TMP_DIR="$(mktemp -d /tmp/pvewebtools-install.XXXXXX)"
    echo "==> 未检测到完整源码，正在拉取 $REPO_URL"
    if ! git clone --depth 1 "$REPO_URL" "$TMP_DIR/pvewebtools" >/dev/null 2>&1; then
        echo "错误: 拉取源码失败（检查网络，或改用「复制仓库后 ./install.sh」的方式）" >&2
        exit 1
    fi
    echo "==> 源码已就绪: $TMP_DIR/pvewebtools"
    exec bash "$TMP_DIR/pvewebtools/install.sh" "$@"
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
