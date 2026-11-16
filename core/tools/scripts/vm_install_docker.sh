
VMIDS_RAW="${PT_VMID:-}"
VMIDS=$(echo "$VMIDS_RAW" | tr ',' ' ' | tr -s '[:space:]' '\n' | grep -E '^[0-9]+$' | sort -n -u | tr '\n' ' ')
[ -z "$VMIDS" ] && { echo "[错误] 未选择任何有效虚机（VMID）"; exit 1; }
N=$(echo "$VMIDS" | wc -w)
echo "===== 处理 $N 台虚机：$VMIDS ====="
IDX=0
for VMID in $VMIDS; do
  IDX=$((IDX+1))
  PT_VMID="$VMID"
  echo
  echo "===== [$IDX/$N] 虚机 $VMID ====="
  (
  echo "===== 虚机内安装 Docker ====="
  VMID="$PT_VMID"
MIRROR="${PT_MIRROR:-aliyun}"
REGISTRY="${PT_REGISTRY:-auto}"
REGISTRY_CUSTOM="${PT_REGISTRY_CUSTOM:-}"
DOCKER_VERSION="${PT_DOCKER_VERSION:-latest}"
echo "VMID=$VMID  MIRROR=$MIRROR  REGISTRY=$REGISTRY  DOCKER_VERSION=$DOCKER_VERSION"

# 1) 虚机状态
st=$(qm status "$VMID" 2>/dev/null | awk '{print $2}')
if [ "$st" != "running" ]; then
  echo "[错误] 虚机 $VMID 当前状态为「${st:-未知}」，必须处于 running 才能安装。"
  echo "[提示] 请先在 PVE 中开机该虚机，再执行本工具。"
  exit 1
fi
echo "[1/5] 虚机状态: running"

# ---- 宿主机侧虚机电源/磁盘辅助（两处自愈复用） ----
# vm_shutdown: 优雅关机，超时强杀；vm_start: 开机
vm_shutdown() {
  qm shutdown "$VMID" >/dev/null 2>&1
  local k st
  for k in $(seq 1 20); do
    st=$(qm status "$VMID" 2>/dev/null | awk '{print $2}')
    [ "$st" != "running" ] && break
    sleep 2
  done
  st=$(qm status "$VMID" 2>/dev/null | awk '{print $2}')
  [ "$st" = "running" ] && qm stop "$VMID" >/dev/null 2>&1
  sleep 2
}
vm_start() { qm start "$VMID" >/dev/null 2>&1; }

# vm_root_mount: 把虚机系统盘根分区挂到 VM_ROOT_MNT（须先关机）。
# 成功设置全局 VM_ROOT_MNT/VM_ROOT_NBD 并返回 0；失败返回 1（已断开已建立的 nbd）。
vm_root_mount() {
  VM_ROOT_MNT=""; VM_ROOT_NBD=""; VM_ROOT_KPARTX=""
  command -v qemu-nbd >/dev/null 2>&1 || { echo "[自愈] 宿主机缺少 qemu-nbd"; return 1; }
  local disk_line vol dev
  disk_line=$(qm config "$VMID" 2>/dev/null | grep -E '^(scsi|virtio|sata|ide)[0-9]+:' | grep -viE 'cloudinit|efidisk|media=cdrom' | head -1)
  [ -n "$disk_line" ] || { echo "[自愈] 未找到可挂载的系统盘"; return 1; }
  vol="${disk_line#*: }"; vol="${vol%%,*}"
  if echo "$vol" | grep -q '^/dev/'; then dev="$vol"; else dev=$(pvesm path "$vol" 2>/dev/null); fi
  { [ -n "$dev" ] && { [ -b "$dev" ] || [ -f "$dev" ]; }; } || { echo "[自愈] 无法解析磁盘路径: $vol"; return 1; }

  local nbd="" n b
  modprobe nbd max_part=16 >/dev/null 2>&1
  for n in /dev/nbd0 /dev/nbd1 /dev/nbd2 /dev/nbd3 /dev/nbd4 /dev/nbd5; do
    [ -b "$n" ] || continue
    b=$(basename "$n")
    [ -s "/sys/block/$b/pid" ] && continue
    nbd="$n"; break
  done
  [ -n "$nbd" ] || { echo "[自愈] 无空闲 /dev/nbdX 设备"; return 1; }
  qemu-nbd --connect="$nbd" "$dev" >/dev/null 2>&1 || { echo "[自愈] qemu-nbd 连接磁盘失败"; return 1; }
  VM_ROOT_NBD="$nbd"
  sleep 1
  partprobe "$nbd" >/dev/null 2>&1
  partx -a "$nbd" >/dev/null 2>&1
  sleep 1

  local mnt part fstype rootpart="" pt parts=""
  mnt=$(mktemp -d)
  # 收集候选分区：nbd 直显分区 → 整盘即文件系统 → kpartx 映射（nbd 预载 max_part=0 时）
  for part in "${nbd}"p*; do [ -b "$part" ] && parts="$parts $part"; done
  if [ -z "$parts" ]; then
    pt=$(blkid -o value -s PTTYPE "$nbd" 2>/dev/null)
    fstype=$(blkid -o value -s TYPE "$nbd" 2>/dev/null)
    if [ -z "$pt" ] && echo "$fstype" | grep -qE '^(ext2|ext3|ext4|xfs)$'; then
      parts="$nbd"
    elif [ -n "$pt" ] && command -v kpartx >/dev/null 2>&1; then
      kpartx -a "$nbd" >/dev/null 2>&1; sleep 1; VM_ROOT_KPARTX=1
      for part in /dev/mapper/"$(basename "$nbd")"p*; do [ -b "$part" ] && parts="$parts $part"; done
    fi
  fi
  # 逐分区找根文件系统（以含 /etc/os-release 判定）
  for part in $parts; do
    fstype=$(blkid -o value -s TYPE "$part" 2>/dev/null)
    case "$fstype" in ext2|ext3|ext4|xfs) ;; *) continue ;; esac
    if mount -t "$fstype" "$part" "$mnt" 2>/dev/null; then
      if [ -f "$mnt/etc/os-release" ]; then rootpart="$part"; break; fi
      umount "$mnt" 2>/dev/null
    fi
  done
  if [ -z "$rootpart" ]; then
    echo "[自愈] 未能在系统盘定位根文件系统（可能 LVM/多盘/非常规分区）"
    rmdir "$mnt" 2>/dev/null
    vm_root_umount
    return 1
  fi
  VM_ROOT_MNT="$mnt"
  return 0
}

vm_root_umount() {
  [ -n "$VM_ROOT_MNT" ] && { sync; umount "$VM_ROOT_MNT" 2>/dev/null; rmdir "$VM_ROOT_MNT" 2>/dev/null; }
  [ "$VM_ROOT_KPARTX" = "1" ] && [ -n "$VM_ROOT_NBD" ] && kpartx -d "$VM_ROOT_NBD" >/dev/null 2>&1
  [ -n "$VM_ROOT_NBD" ] && qemu-nbd --disconnect "$VM_ROOT_NBD" >/dev/null 2>&1
  VM_ROOT_MNT=""; VM_ROOT_NBD=""; VM_ROOT_KPARTX=""
}

# ---- guest-exec 就绪探测 ----
# ga_probe [iterations]: 轮询 qm guest exec，输出 ready / disabled / timeout
ga_probe() {
  local iters="${1:-40}" i err
  for i in $(seq 1 "$iters"); do
    if err=$(timeout 5 qm guest exec "$VMID" -- true 2>&1 >/dev/null); then echo ready; return 0; fi
    if echo "$err" | grep -qi "guest-exec has been disabled"; then echo disabled; return 0; fi
    sleep 3
  done
  echo timeout
}

# guest_pm: 经 guest-exec 探测虚机内可用包管理器，输出 dnf / yum / apt / none
guest_pm() {
  local raw
  raw=$(qm guest exec "$VMID" -- /bin/bash -c 'for p in dnf yum apt-get microdnf; do command -v "$p" >/dev/null 2>&1 && "$p" --version >/dev/null 2>&1 && { echo -n "$p"; exit 0; }; done; echo -n none' 2>/dev/null)
  echo "$raw" | python3 -c 'import sys,json
raw=sys.stdin.read()
try:
    d=json.loads(raw) or {}
    od=(d.get("out-data") or "").strip()
except Exception:
    od=""
print(od or "none")'
}

# ---- 自愈 1：guest-exec 被禁用（CentOS/RHEL 7 默认黑名单含 guest-exec）----
# 关机 → 挂盘移除 guest-exec*（保留 guest-file-*）→ 开机。成功返回 0。
ga_autofix() {
  echo "[自愈] guest-exec 被禁用：挂盘移除黑名单（会重启虚机一次）..."
  vm_shutdown
  if ! vm_root_mount; then vm_start; return 1; fi
  local fixed=0 f
  for f in "$VM_ROOT_MNT/etc/sysconfig/qemu-ga" "$VM_ROOT_MNT/etc/default/qemu-guest-agent" "$VM_ROOT_MNT/etc/qemu-ga/qemu-ga.conf"; do
    [ -f "$f" ] && sed -i 's/guest-exec[-a-z]*,//g; s/,guest-exec[-a-z]*//g; s/guest-exec[-a-z]*//g' "$f" && fixed=1
  done
  [ "$fixed" = "1" ] || echo "[自愈] 黑名单配置里未发现 guest-exec（可能不走配置文件）"
  vm_root_umount
  echo "[自愈] 重新启动虚机..."
  vm_start
  [ "$fixed" = "1" ]
}

# ---- 自愈 2：SELinux enforcing 把 virt_qemu_ga_t 域里的包管理器拦死（CentOS/RHEL 7 常见）----
# 关机 → 挂盘把 /etc/selinux/config 改成 disabled（彻底关）→ 开机。成功返回 0。
# 改动写在虚机磁盘上，跨重启持久；日后要开回 enforcing 需 touch /.autorelabel 后重启做整盘重打标签。
ga_selinux_fix() {
  echo "[自愈] 虚机内包管理器不可用，疑似 SELinux(virt_qemu_ga_t) 拦截：挂盘置 disabled（会重启虚机一次）..."
  vm_shutdown
  if ! vm_root_mount; then vm_start; return 1; fi
  local cfg="$VM_ROOT_MNT/etc/selinux/config" fixed=0
  if [ -f "$cfg" ]; then
    if grep -qE '^SELINUX=enforcing' "$cfg"; then
      sed -i 's/^SELINUX=enforcing/SELINUX=disabled/' "$cfg" && fixed=1
      echo "[自愈] 已把虚机 SELinux 配置改为 disabled"
    else
      echo "[自愈] 虚机 SELinux 当前非 enforcing（未改动）；问题可能另有原因"
    fi
  else
    echo "[自愈] 未找到 /etc/selinux/config（虚机可能未启用 SELinux）"
  fi
  vm_root_umount
  echo "[自愈] 重新启动虚机..."
  vm_start
  [ "$fixed" = "1" ]
}

# 2) 等待 QEMU Guest Agent 就绪（最多 120s）
echo "[2/5] 等待 QEMU Guest Agent 就绪（最多 120s）..."
ga_state=$(ga_probe 40)

# guest-exec 被禁用 → 先尝试宿主机侧自动修复，再复检
if [ "$ga_state" = "disabled" ]; then
  echo "[提示] QEMU Guest Agent 已连接，但 guest-exec 被该实例禁用（CentOS/RHEL 7 等老版本常见）。"
  if ga_autofix; then
    echo "[自动修复] 已放行 guest-exec，等待虚机重启后代理恢复..."
    ga_state=$(ga_probe 60)
  fi
fi

if [ "$ga_state" = "disabled" ]; then
  echo "[错误] 虚机 QEMU Guest Agent 已连接，但「guest-exec」命令被该实例禁用，且自动修复未成功。"
  echo "[原因] 虚机内的 qemu-guest-agent 以黑名单/未放行 guest-exec 的方式启动（CentOS/RHEL 7 等较老版本常见）。"
  echo
  echo "[修复] 先以 SSH 或 PVE 控制台(noVNC) 进入虚机，按系统执行下面命令放行 guest-exec 并重启服务："
  echo
  echo "---- CentOS / RHEL 7（在虚机内执行）----"
  echo "grep '^BLACKLIST_RPC' /etc/sysconfig/qemu-ga"
  echo "sed -i 's/guest-exec[-a-z]*,//g; s/,guest-exec[-a-z]*//g; s/guest-exec[-a-z]*//g' /etc/sysconfig/qemu-ga"
  echo "systemctl restart qemu-guest-agent"
  echo
  echo "---- Debian / Ubuntu（在虚机内执行）----"
  echo "grep -Rni blacklist /etc/qemu-ga/ /etc/default/qemu-guest-agent 2>/dev/null"
  echo "sed -i 's/guest-exec[-a-z]*,//g; s/,guest-exec[-a-z]*//g; s/guest-exec[-a-z]*//g' /etc/qemu-ga/qemu-ga.conf /etc/default/qemu-guest-agent 2>/dev/null"
  echo "systemctl restart qemu-guest-agent"
  echo
  echo "[验证] 回到 PVE 宿主机执行（把 100 换成你的 VMID），期望 exit=0："
  echo 'qm guest exec 100 -- true; echo "exit=$?"'
  echo
  echo "[提示] 放行并重启后，重新执行本工具即可。"
  exit 1
fi
if [ "$ga_state" != "ready" ]; then
  echo "[错误] QEMU Guest Agent 未在预期时间内就绪，无法进入虚机执行安装。"
  echo "[排查] 请确认："
  echo "  1) 虚机「选项」中已勾选 QEMU Guest Agent（agent=1）；"
  echo "  2) 虚机系统内已安装并运行 qemu-guest-agent："
  echo "     Debian/Ubuntu: apt install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent"
  echo "     CentOS/RHEL:   dnf/yum install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent"
  echo "  3) 虚机已完全进入系统（而非卡在引导界面）。"
  exit 1
fi
echo "  QEMU Guest Agent 就绪"

# 3) 探测虚机内 OS（只取 ASCII 字段，规避 QEMU 代理 latin-1 中文乱码）
OSINFO=$(qm guest exec "$VMID" -- cat /etc/os-release 2>/dev/null | python3 -c 'import sys,json,re
raw=sys.stdin.read()
try:
    d=json.loads(raw) or {}
except Exception:
    d={}
content = d.get("out-data") or ""
def gv(t,k):
    m=re.search(r"(?m)^"+re.escape(k)+r"=\"?([^\"\n]*)", t)
    return m.group(1) if m else ""
print(gv(content,"ID")+"|"+gv(content,"ID_LIKE"))')
ID="${OSINFO%%|*}"
echo "[3/5] 虚机系统 ID=$ID"
if [ "$ID" = "windows" ]; then
  echo "[错误] 检测到 Windows 虚机。Windows 请使用 Docker Desktop + WSL 安装，本工具仅支持 Linux 虚机。"
  exit 1
fi

# 3.5) 预检：虚机内是否有可用包管理器
# CentOS/RHEL 7 开 SELinux(enforcing) 时，qm guest exec 跑在受限域 virt_qemu_ga_t，
# 连 /usr/bin/yum 都读不到 → 会被误判成"无包管理器"。这里提前发现并自动放宽 SELinux 再继续。
PM=$(guest_pm)
echo "[预检] 虚机包管理器: $PM"
if [ "$PM" = "none" ]; then
  if ga_selinux_fix; then
    echo "[自愈] SELinux 已置 disabled，等待虚机重启后代理恢复..."
    ga_state=$(ga_probe 60)
    if [ "$ga_state" != "ready" ]; then
      echo "[错误] 修复后 QEMU Guest Agent 未恢复，无法继续，请检查虚机状态后重试。"
      exit 1
    fi
    PM=$(guest_pm)
    echo "[预检] 修复后包管理器: $PM"
  fi
fi
if [ "$PM" = "none" ]; then
  echo "[错误] 虚机内未找到可用的包管理器（yum/dnf/apt-get）。"
  echo "[提示] 若为 CentOS/RHEL 7，多为 SELinux enforcing 限制 qemu-ga 域（virt_qemu_ga_t）所致："
  echo "       进虚机控制台执行 setenforce 0（临时），或把 /etc/selinux/config 改为 disabled（持久）后重试。"
  exit 1
fi

# 4) 组装虚机内安装脚本（base64 传入；虚机内回显统一用英文，避免 QEMU 代理乱码）
GUEST_SCRIPT=$(cat <<'GUEST_EOF'
#!/usr/bin/env bash
set -e
# qm guest exec 继承的是 qemu-ga 进程的最小 PATH，常不含 /usr/bin，
# 会导致 command -v 找不到 apt-get/dnf/yum 而误走 get.docker.com 便捷脚本；这里显式补全。
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
MIRROR='__MIRROR__'
REGISTRY='__REGISTRY__'
DOCKER_VERSION='__DOCKER_VERSION__'
# 选定大版本时锁定该线最后一个稳定版（latest 不锁）；装前解析仓库内该线最高版本号
if [ "$DOCKER_VERSION" != "latest" ] && [ -n "$DOCKER_VERSION" ]; then
  MAJOR="$DOCKER_VERSION"
  echo "[*] pin docker major line: $MAJOR"
else
  MAJOR=""
fi
# registry 镜像 key -> 真实 URL（none/auto 不在此处决定，auto 见下方探测）
case "$REGISTRY" in
  1ms)     REGISTRY_MIRROR="https://docker.1ms.run" ;;
  daocloud) REGISTRY_MIRROR="https://docker.m.daocloud.io" ;;
  163)     REGISTRY_MIRROR="https://hub-mirror.c.163.com" ;;
  tencent) REGISTRY_MIRROR="https://mirror.ccs.tencentyun.com" ;;
  baidu)   REGISTRY_MIRROR="https://mirror.baidubce.com" ;;
  ustc)    REGISTRY_MIRROR="https://docker.mirrors.ustc.edu.cn" ;;
  custom)  REGISTRY_MIRROR='__REGISTRY_CUSTOM__' ;;
  auto|none|"") REGISTRY_MIRROR="" ;;
  *)       REGISTRY_MIRROR="$REGISTRY" ;;
esac
echo "[*] install docker (mirror=$MIRROR, registry=${REGISTRY_MIRROR:-none})"
if command -v docker >/dev/null 2>&1; then
  echo "[*] docker already installed: $(docker --version)"
else
  PKG=""
  if [ -x /usr/bin/apt-get ] || [ -x /bin/apt-get ]; then PKG=apt
  elif [ -x /usr/bin/dnf ] || [ -x /bin/dnf ]; then PKG=dnf
  elif [ -x /usr/bin/yum ] || [ -x /bin/yum ]; then PKG=yum
  elif [ -x /usr/bin/microdnf ]; then PKG=dnf
  elif command -v apt-get >/dev/null 2>&1; then PKG=apt
  elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then PKG=yum
  fi
  echo "[*] detected package manager: ${PKG:-none}"
  if [ "$PKG" = apt ]; then
    echo "[*] apt-based distro"
    . /etc/os-release
    ARCH=$(dpkg --print-architecture)
    case "$MIRROR" in
      aliyun) REPO="https://mirrors.aliyun.com/docker-ce/linux/$ID" ;;
      ustc)   REPO="https://mirrors.ustc.edu.cn/docker-ce/linux/$ID" ;;
      *)      REPO="https://download.docker.com/linux/$ID" ;;
    esac
    echo "[*] repo: $REPO"
    apt-get update -y
    apt-get install -y ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "$REPO/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$ARCH signed-by=/etc/apt/keyrings/docker.asc] $REPO $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    if [ -n "$MAJOR" ]; then
      EXACT=$(apt-cache madison docker-ce 2>/dev/null | awk '{print $3}' | grep -E "^[0-9]+:${MAJOR}\." | sort -V | tail -1)
      if [ -z "$EXACT" ]; then
        echo "[ERROR] docker-ce ${MAJOR}.x not found in repo for this distro/codename (likely too old / not published here)."
        LATEST_MAJOR=$(apt-cache madison docker-ce 2>/dev/null | awk '{print $3}' | grep -oE '^[0-9]+:[0-9]+' | sed 's/^[0-9]*://' | sort -n | tail -1)
        if [ -n "$LATEST_MAJOR" ]; then
          echo "[INFO] recommended: latest available major is ${LATEST_MAJOR}.x; falling back to it."
          MAJOR="$LATEST_MAJOR"
          EXACT=$(apt-cache madison docker-ce 2>/dev/null | awk '{print $3}' | grep -E "^[0-9]+:${MAJOR}\." | sort -V | tail -1)
        else
          echo "[ERROR] no docker-ce version resolvable from repo; cannot install."
          exit 1
        fi
      fi
      echo "[*] resolved docker-ce version: $EXACT"
      apt-get install -y docker-ce="$EXACT" docker-ce-cli="$EXACT" docker-buildx-plugin docker-compose-plugin containerd.io
    else
      apt-get install -y docker-ce docker-ce-cli docker-buildx-plugin docker-compose-plugin containerd.io
    fi
  elif [ "$PKG" = dnf ] || [ "$PKG" = yum ]; then
    echo "[*] rpm-based distro"
    . /etc/os-release 2>/dev/null
    _WEAK=""
    # 先删掉上次失败遗留的 docker-ce 源：其中 $releasever 由 dnf 解析（openEuler 得到 24.03LTS_SP4 之类），
    # 指向不存在的目录，会让后面任何一条 dnf/yum 命令在元数据阶段就硬失败。装好插件后再按需重新生成。
    _REPOFILE=/etc/yum.repos.d/docker-ce.repo
    rm -f "$_REPOFILE"
    # CentOS 7 / RHEL 7 已 EOL，官方仓库(mirror.centos.org)失效，安装任何包前先把基础仓库切到 vault 归档源，
    # 否则 yum 直接报 baseurl 不可用；docker-ce 依赖的 container-selinux 也来自 vault 的 extras 仓库。
    if { [ "$ID" = "centos" ] && [ "${VERSION_ID%%.*}" = "7" ]; } || { [ "$ID" = "rhel" ] && [ "${VERSION_ID%%.*}" = "7" ]; }; then
      echo "[*] $ID $VERSION_ID is EOL, official repo is gone; switching base repos to vault archive"
      case "$MIRROR" in
        ustc)   _VAULT="https://mirrors.ustc.edu.cn/centos-vault/7.9.2009" ;;
        aliyun) _VAULT="https://mirrors.aliyun.com/centos-vault/7.9.2009" ;;
        *)      _VAULT="http://vault.centos.org/centos/7" ;;
      esac
      # EOL 的 EPEL 7 已清空，新版 docker-ce 会把弱依赖 docker-ce-rootless-extras 拉进来，
      # 后者又依赖 fuse-overlayfs/slirp4netns（仅 EPEL 有）→ 装失败；关掉弱依赖即可跳过 rootless。
      _WEAK="--setopt=install_weak_deps=False"
      for f in /etc/yum.repos.d/CentOS-*.repo /etc/yum.repos.d/RHEL*.repo; do
        [ -e "$f" ] || continue
        sed -i -e 's|^mirrorlist=|#mirrorlist=|g' -e "s|^#\?baseurl=http://mirror.centos.org/centos/\$releasever/|baseurl=$_VAULT/|g" "$f"
      done
      if ! yum makecache fast >/dev/null 2>&1; then
        echo "[*] mirror vault unreachable, falling back to vault.centos.org"
        _VAULT="http://vault.centos.org/centos/7"
        for f in /etc/yum.repos.d/CentOS-*.repo /etc/yum.repos.d/RHEL*.repo; do
          [ -e "$f" ] || continue
          sed -i -e "s|^#\?baseurl=https://mirrors.ustc.edu.cn/centos-vault/7.9.2009/|baseurl=$_VAULT/|g" -e "s|^#\?baseurl=https://mirrors.aliyun.com/centos-vault/7.9.2009/|baseurl=$_VAULT/|g" "$f"
        done
        yum makecache fast >/dev/null 2>&1 || true
      fi
    fi
    # 卸载旧版 docker / docker-engine / containerd（标准做法，避免与 docker-ce 冲突）
    echo "[*] removing old docker packages (if any)"
    "$PKG" remove -y 'docker*' 'docker-engine*' 'containerd.io' 'runc' >/dev/null 2>&1 || true
    case "$MIRROR" in
      aliyun) REPO_BASE="https://mirrors.aliyun.com/docker-ce/linux" ;;
      ustc)   REPO_BASE="https://mirrors.ustc.edu.cn/docker-ce/linux" ;;
      *)      REPO_BASE="https://download.docker.com/linux" ;;
    esac
    echo "[*] repo base: $REPO_BASE"
    # docker-ce 官方源只发布 centos / fedora 两类目录；openEuler、Anolis、Kylin 等 EL 衍生发行版走 centos 源。
    case "$ID" in fedora) _DIST=fedora ;; *) _DIST=centos ;; esac
    _ARCH=$(uname -m)
    # 关键：openEuler 的 $releasever 形如 24.03，docker-ce 源里根本没有该目录，
    # 元数据请求直接 404（日志里只有一行 "Docker CE Stable ... 414 B" 后退出 1）。
    # 因此加源后必须把 $releasever 钉到源上真实存在的版本号。
    _pick_rel() {
      local v c
      # centos/rhel/rocky/alma/fedora：源目录就是本发行版主版本号，保持不变
      case "$ID" in
        centos|rhel|rocky|almalinux) v="${VERSION_ID%%.*}"; echo "${v:-9}"; return 0 ;;
        fedora) echo "${VERSION_ID:-}"; return 0 ;;
      esac
      # 其余发行版（openEuler/Anolis/Kylin 等）的 releasever 不能直接用，探测源上真实存在的 EL 版本
      if command -v curl >/dev/null 2>&1; then
        for c in 9 10 8; do
          if curl -fsS --max-time 8 "$REPO_BASE/$_DIST/$c/$_ARCH/stable/repodata/repomd.xml" >/dev/null 2>&1; then
            echo "$c"; return 0
          fi
        done
      fi
      echo 9
    }
    _REL=$(_pick_rel)
    echo "[*] docker-ce repo: distro dir=$_DIST releasever=$_REL arch=$_ARCH"
    # 非 EL 发行版（openEuler 等）关掉弱依赖，避免拉进只在 EPEL 里的 rootless 依赖导致装不上
    case "$ID" in
      centos|rhel|rocky|almalinux|fedora) ;;
      *) [ -n "$_WEAK" ] || _WEAK="--setopt=install_weak_deps=False" ;;
    esac

    if [ "$PKG" = dnf ]; then
      dnf install -y dnf-plugins-core $_WEAK --disablerepo='docker-ce*'
      _LISTCMD="dnf list docker-ce --showduplicates"
      _INSTALL="dnf install -y"
    else
      yum install -y yum-utils device-mapper-persistent-data lvm2 $_WEAK --disablerepo='docker-ce*'
      _LISTCMD="yum --showduplicates list docker-ce"
      _INSTALL="yum install -y"
    fi
    # 直接下载 repo 文件（config-manager --add-repo 会立刻刷新一次元数据，而 $releasever 未修正时注定失败）
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL --max-time 20 "$REPO_BASE/$_DIST/docker-ce.repo" -o "$_REPOFILE" >/dev/null 2>&1 || true
    fi
    if [ ! -s "$_REPOFILE" ]; then
      if [ "$PKG" = dnf ]; then
        dnf config-manager --add-repo "$REPO_BASE/$_DIST/docker-ce.repo" || true
      else
        yum-config-manager --add-repo "$REPO_BASE/$_DIST/docker-ce.repo" || true
      fi
    fi
    if [ ! -f "$_REPOFILE" ]; then
      echo "[ERROR] failed to create $_REPOFILE (mirror: $REPO_BASE/$_DIST/docker-ce.repo)"
      exit 1
    fi
    # 修正 repo：① USTC 等镜像的 docker-ce.repo 里 baseurl/gpgkey 仍指向 download.docker.com，
    # 国内基本拉不动（表现为 98 B/s、几秒后元数据失败），需改回镜像地址；
    # ② 把 $releasever 替换为实际存在的版本目录。
    if [ -f "$_REPOFILE" ]; then
      if [ "$REPO_BASE" != "https://download.docker.com/linux" ]; then
        sed -i "s|https://download.docker.com/linux|$REPO_BASE|g" "$_REPOFILE"
      fi
      sed -i "s/\$releasever/$_REL/g" "$_REPOFILE"
      grep -E '^(baseurl|gpgkey)=' "$_REPOFILE" | head -3
    fi

    if [ -n "$MAJOR" ]; then
      EXACT=$($_LISTCMD 2>/dev/null | awk '/^docker-ce\./{print $2}' | sed 's/^[0-9]*://' | grep -E "^${MAJOR}\." | sort -V | tail -1)
      if [ -z "$EXACT" ]; then
        echo "[ERROR] docker-ce ${MAJOR}.x not found in repo for this distro (likely too old / not published here)."
        LATEST_MAJOR=$($_LISTCMD 2>/dev/null | awk '/^docker-ce\./{print $2}' | sed 's/^[0-9]*://' | grep -oE '^[0-9]+' | sort -n | tail -1)
        if [ -n "$LATEST_MAJOR" ]; then
          echo "[INFO] recommended: latest available major is ${LATEST_MAJOR}.x; falling back to it."
          MAJOR="$LATEST_MAJOR"
          EXACT=$($_LISTCMD 2>/dev/null | awk '/^docker-ce\./{print $2}' | sed 's/^[0-9]*://' | grep -E "^${MAJOR}\." | sort -V | tail -1)
        fi
        if [ -z "$EXACT" ]; then
          echo "[ERROR] no docker-ce version resolvable from repo; cannot install."
          echo "[DEBUG] repo url: $REPO_BASE/$_DIST/$_REL/$_ARCH/stable"
          sed -n '1,30p' "$_REPOFILE" 2>/dev/null
          exit 1
        fi
      fi
      echo "[*] resolved docker-ce version: $EXACT"
      PKGS="docker-ce-$EXACT docker-ce-cli-$EXACT docker-compose-plugin containerd.io"
    else
      PKGS="docker-ce docker-ce-cli docker-compose-plugin containerd.io"
    fi
    echo "[*] installing: $PKGS ${_WEAK}"
    _ILOG=/tmp/docker-ce-install.log
    if ! $_INSTALL $PKGS $_WEAK >"$_ILOG" 2>&1; then
      cat "$_ILOG"
      # openEuler 等非 RHEL 系可能缺 docker-ce 的硬依赖 container-selinux，先用本发行版仓库补上再重试一次
      if grep -qi 'container-selinux' "$_ILOG"; then
        echo "[*] dependency container-selinux missing: install it from distro repo, then retry"
        "$PKG" install -y container-selinux >/dev/null 2>&1 || true
        if ! $_INSTALL $PKGS $_WEAK; then
          echo "[ERROR] docker-ce install failed (distro=$ID, repo dist dir=$_DIST, releasever=$_REL)"
          exit 1
        fi
      else
        echo "[ERROR] docker-ce install failed (distro=$ID, repo dist dir=$_DIST, releasever=$_REL)"
        echo "[DEBUG] repo file:"
        sed -n '1,30p' "$_REPOFILE" 2>/dev/null
        exit 1
      fi
    fi
  else
    echo "[*] unknown package manager (PATH=$PATH)"
    echo "[*] debug: $(ls -l /usr/bin/apt-get /usr/bin/dnf /usr/bin/yum /bin/yum /usr/bin/microdnf 2>&1)"
    curl -fsSL https://get.docker.com | sh
  fi
fi
# ===== Registry 加速器 =====
# 国内公共加速器（网易/百度/中科大等）2024 年起陆续停服，写死任何一个都可能 pull 卡住：
# - auto：逐个探测候选地址的 /v2/（200/401 都算在线），选第一个可用的；全失败则直连 docker.io
# - none：显式直连
if [ "$REGISTRY" = "auto" ] && [ -z "$REGISTRY_MIRROR" ]; then
  if command -v curl >/dev/null 2>&1; then
    echo "[*] auto-detect registry mirror ..."
    for u in https://docker.1ms.run https://docker.m.daocloud.io \
             https://docker.nju.edu.cn https://docker.mirrors.sjtug.sjtu.edu.cn \
             https://mirror.ccs.tencentyun.com https://hub-mirror.c.163.com \
             https://mirror.baidubce.com https://docker.mirrors.ustc.edu.cn; do
      code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$u/v2/" 2>/dev/null || true)
      case "$code" in
        200|401) REGISTRY_MIRROR="$u"; echo "[*] reachable: $u (HTTP $code)"; break ;;
        *) echo "[*] skip unreachable: $u (HTTP ${code:-timeout})" ;;
      esac
    done
  else
    echo "[!] curl not found, skip auto-detect"
  fi
  [ -n "$REGISTRY_MIRROR" ] || echo "[!] no reachable public registry mirror; docker will pull from docker.io directly"
fi
if [ -n "$REGISTRY_MIRROR" ]; then
  echo "[*] configure registry mirror: $REGISTRY_MIRROR"
  mkdir -p /etc/docker
  # 已有 daemon.json 且不是本工具写的（不含 registry-mirrors）：先备份再覆盖，避免静默丢掉
  # data-root / log-driver 等自定义配置；合并需人工确认，这里只保证有备份可回退。
  if [ -f /etc/docker/daemon.json ] && ! grep -q 'registry-mirrors' /etc/docker/daemon.json; then
    cp -a /etc/docker/daemon.json "/etc/docker/daemon.json.bak.$(date +%s)" 2>/dev/null || true
    echo "[*] existing daemon.json backed up before overwrite"
  fi
  cat > /etc/docker/daemon.json <<JSON
{"registry-mirrors": ["$REGISTRY_MIRROR"]}
JSON
else
  # 未配置/探测失败：清掉本工具此前写下的「只有 registry-mirrors」的 daemon.json，
  # 否则失效地址会一直留在里面、每次 pull 都要先撞一次超时。
  if [ -f /etc/docker/daemon.json ] && grep -q 'registry-mirrors' /etc/docker/daemon.json; then
    if grep -qE '^[[:space:]]*\{[[:space:]]*"registry-mirrors"[[:space:]]*:' /etc/docker/daemon.json; then
      rm -f /etc/docker/daemon.json
      echo "[*] removed stale daemon.json (registry-mirrors only)"
    else
      echo "[!] /etc/docker/daemon.json still has registry-mirrors; please review it manually"
    fi
  fi
fi
systemctl enable docker
systemctl restart docker || systemctl start docker
sleep 2
echo "[*] verify: $(docker --version)"
docker info >/dev/null 2>&1 && echo "[*] docker daemon running" || echo "[!] docker daemon not ready"
# 配了加速器就实际拉一次 hello-world：能 pull 才算真的可用（daemon 起来不代表镜像源通）
if [ -n "$REGISTRY_MIRROR" ]; then
  _pull_ok=0
  echo "[*] verify pull through mirror (hello-world) ..."
  if command -v timeout >/dev/null 2>&1; then
    if timeout 60 docker pull hello-world >/dev/null 2>&1; then _pull_ok=1; fi
  else
    if docker pull hello-world >/dev/null 2>&1; then _pull_ok=1; fi
  fi
  if [ "$_pull_ok" = "1" ]; then
    echo "[*] registry mirror works (hello-world pulled)"
  else
    echo "[!] pull through mirror failed: mirror may be down or rate-limited; use a private aliyun accelerator instead"
  fi
fi
echo "[done] docker install finished"
GUEST_EOF
)
GUEST_SCRIPT=${GUEST_SCRIPT//__MIRROR__/$MIRROR}
GUEST_SCRIPT=${GUEST_SCRIPT//__REGISTRY__/$REGISTRY}
GUEST_SCRIPT=${GUEST_SCRIPT//__REGISTRY_CUSTOM__/$REGISTRY_CUSTOM}
GUEST_SCRIPT=${GUEST_SCRIPT//__DOCKER_VERSION__/$DOCKER_VERSION}

B64=$(printf '%s' "$GUEST_SCRIPT" | base64 -w0)
echo "[4/5] 进入虚机执行安装（以下为虚机内实时日志）..."
# 为什么不用 `qm guest exec --timeout 1800`（一次性同步调用）：
#   1) 同步模式下 qm 自己在宿主机侧每秒查一次 guest-exec-status，而 QGA 每次查询后会清空
#      输出缓冲 → 长任务中途的输出被丢弃，只剩最后一小段；
#   2) 整段日志要等命令完全跑完才一次性返回，网页上表现为长时间「零输出」，像卡死。
# 这里改为 synchronous=0 先拿 pid，再由 python3 轮询 guest-exec-status 增量回传
# stdout/stderr，实现边装边看；每 15s 打一行心跳，避免误判卡死；总超时 1800s。
PT_GA_VMID="$VMID" PT_GA_B64="$B64" PT_GA_DEADLINE=1800 python3 - <<'PYEOF'
import base64
import binascii
import json
import os
import re
import subprocess
import sys
import time

vmid = os.environ.get("PT_GA_VMID", "")
b64 = os.environ.get("PT_GA_B64", "")
deadline = int(os.environ.get("PT_GA_DEADLINE", "1800") or 1800)


def emit(s):
    if s:
        sys.stdout.write(s)
        sys.stdout.flush()


def qm(args, timeout=60):
    return subprocess.run(["qm"] + args, capture_output=True, text=True,
                          errors="replace", timeout=timeout)


_B64_ONLY = re.compile(r"\A[A-Za-z0-9+/]+={0,2}\Z")


def as_text(raw):
    """不同 PVE 版本的 guest-exec-status 可能返回 base64 原文或已解码文本，按内容自适应。"""
    if not raw:
        return raw
    t = "".join(raw.split())
    if len(t) >= 8 and len(t) % 4 == 0 and _B64_ONLY.match(t):
        try:
            dec = base64.b64decode(t, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return raw
        if dec and all(c in "\r\n\t" or 32 <= ord(c) < 127 for c in dec):
            return demojibake(dec)
    return demojibake(raw)


def demojibake(s):
    """修复 QEMU 代理的 latin-1 乱码：UTF-8 字节被按 latin-1 解成「å¸è½½」这类字符。

    虚机内脚本已约定只用英文回显，这里只是兜底（自定义脚本、apt/dnf 自带中文横幅等）。
    仅当整串能无损按 latin-1 还原、且还原后是合法 UTF-8 时才替换，纯 ASCII 与真正的
    Latin-1 文本（café 之类还原不出合法 UTF-8）都原样返回。
    """
    try:
        raw_bytes = s.encode("latin-1")
    except (UnicodeEncodeError, ValueError):
        return s
    try:
        fixed = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return s
    return fixed if fixed != s and any(ord(c) > 127 for c in fixed) else s


# ---- 1) 异步启动虚机内安装，拿 guest 侧 pid ----
try:
    p = qm(["guest", "exec", vmid, "--synchronous", "0", "--",
            "bash", "-c", "echo %s | base64 -d | bash" % b64])
except Exception as e:
    emit("[错误] 启动虚机内安装进程失败: %s\n" % e)
    sys.exit(1)
if p.returncode != 0:
    emit("[错误] 启动虚机内安装进程失败（qm guest exec 返回 %d）\n" % p.returncode)
    emit(((p.stderr or "").strip() + "\n") if (p.stderr or "").strip() else "")
    sys.exit(1)
try:
    pid = (json.loads(p.stdout or "{}") or {}).get("pid")
except Exception:
    pid = None
if not pid:
    emit("[错误] 虚机内安装进程未返回 pid（QEMU Guest Agent 可能未启用 guest-exec）\n")
    emit(((p.stdout or "").strip() + "\n") if (p.stdout or "").strip() else "")
    sys.exit(1)

emit("[*] 安装已在虚机内启动（pid=%s），开始实时回传日志 ...\n" % pid)

start = time.time()
last_beat = start
prev = ""     # 上一轮 out-data，用于兼容「累积式」返回
fails = 0     # 连续查询失败次数（agent 重启/进程丢失会一直失败，不能无限重试）
exitcode = None

# ---- 2) 轮询 exec-status，增量回传输出 ----
while True:
    st = None
    try:
        r = qm(["guest", "exec-status", vmid, str(pid)], timeout=20)
        if r.returncode == 0:
            st = json.loads(r.stdout or "{}") or {}
    except Exception:
        st = None

    if st is None:
        fails += 1
        if fails >= 6:
            emit("\n[错误] 连续多次无法查询虚机内进程状态（QEMU Guest Agent 可能重启或进程状态丢失），停止回传日志。\n")
            emit("[提示] 安装可能仍在虚机内继续，可进虚机执行 ps aux | grep -E 'dnf|apt|docker' 确认。\n")
            sys.exit(1)
    else:
        fails = 0
        cur = as_text(st.get("out-data") or "")
        err = as_text(st.get("err-data") or "")
        # QGA 每次查询后清空缓冲 → cur 即增量；若某实现返回累积值，则按前缀取增量。
        if cur:
            if prev and cur.startswith(prev) and len(cur) >= len(prev):
                delta = cur[len(prev):]
            else:
                delta = cur
            emit(delta)
            prev = cur
        emit(err)
        if st.get("exited"):
            exitcode = st.get("exitcode")
            break

    now = time.time()
    if now - start > deadline:
        emit("\n[错误] 虚机内安装超时（%ds），尝试终止虚机内进程 ...\n" % deadline)
        try:
            qm(["guest", "exec", vmid, "--timeout", "10", "--",
                "bash", "-c", "kill -9 %s 2>/dev/null; true" % pid], timeout=20)
        except Exception:
            pass
        sys.exit(1)
    if now - last_beat >= 15:
        emit("[进度] 安装进行中，已等待 %ds ...\n" % int(now - start))
        last_beat = now
    time.sleep(1)

if exitcode not in (0, None):
    emit("\n[guest] install exited with code %s\n" % exitcode)
    sys.exit(1)
sys.exit(0)
PYEOF
_ga_rc=$?
[ "$_ga_rc" -eq 0 ] || exit 1
echo "[5/5] 完成。可进入虚机执行: docker run --rm hello-world 验证。"
true
  )
done
echo "===== 全部 $N 台虚机处理完成 ====="
