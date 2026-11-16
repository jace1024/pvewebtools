
echo "===== 一键部署k8s ====="
VMIDS_RAW="${PT_VMID:-}"
ARCH="${PT_ARCH:-allinone}"
CNI="${PT_CNI:-flannel}"
CNI_MIRROR="${PT_CNI_MIRROR:-auto}"
CNI_MIRROR_CUSTOM="${PT_CNI_MIRROR_CUSTOM:-}"
SERVER_URL="${PT_SERVER_URL:-}"
JOIN_TOKEN="${PT_TOKEN:-}"
CERT_KEY="${PT_CERT_KEY:-}"
K8S_VERSION="${PT_K8S_VERSION:-1.37}"
K8S_MIRROR="${PT_K8S_MIRROR:-aliyun}"
K8S_MIRROR_CUSTOM="${PT_K8S_MIRROR_CUSTOM:-}"
K8S_REPO="${PT_K8S_REPO:-aliyun}"
OPEN_FW="${PT_OPEN_FW:-1}"
CGROUP_V2="${PT_CGROUP_V2:-1}"
COMPLETION="${PT_SHELL_COMPLETION:-1}"
BACKUP_MANIFEST="${PT_BACKUP_MANIFEST:-1}"
CNI_VERSION_OVERRIDE="${PT_CNI_VERSION:-}"
CALICO_POD_CIDR="${PT_CALICO_POD_CIDR:-10.244.0.0/16}"
CALICO_ENCAP="${PT_CALICO_ENCAP:-vxlan}"
CALICO_AUTODETECT="${PT_CALICO_AUTODETECT:-kubernetes-internal-ip}"
FLANNEL_POD_CIDR="${PT_FLANNEL_POD_CIDR:-10.244.0.0/16}"
FLANNEL_BACKEND="${PT_FLANNEL_BACKEND:-vxlan}"
FLANNEL_IFACE="${PT_FLANNEL_IFACE:-}"
CILIUM_POD_CIDR="${PT_CILIUM_POD_CIDR:-10.244.0.0/16}"
CILIUM_ROUTING="${PT_CILIUM_ROUTING:-tunnel-vxlan}"
CILIUM_DEVICES="${PT_CILIUM_DEVICES:-}"

# 解析虚机列表（支持多选：逗号/空格分隔）；去重并按 VMID 数值排序
VMIDS_CLEAN=$(echo "$VMIDS_RAW" | tr ',' ' ' | tr -s '[:space:]' '\n' | grep -E '^[0-9]+$' | sort -n -u | tr '\n' ' ')
set -- $VMIDS_CLEAN
N=$#
if [ "$N" -eq 0 ]; then echo "[错误] 未选择任何有效虚机（VMID）"; exit 1; fi
echo "目标虚机：$N 台 -> $* ｜ 架构形态=$ARCH"
echo "K8S=$K8S_VERSION  MIRROR=$K8S_MIRROR  REPO=$K8S_REPO  FW=$OPEN_FW  CGV2=$CGROUP_V2  COMPLETION=$COMPLETION"

# ===== 单台虚机的部署流程（下方编排入口按 VMID 多次调用）=====
deploy_vm() {

# -1) 部署前重置（默认开启）：实际重置动作在虚机内完成（见下方 GUEST 脚本的 reset 段）。
#     本脚本运行在 PVE 宿主机侧，若在此直接 kubeadm reset / rm 数据目录只会作用于宿主机、
#     对虚机里的旧集群完全无效；因此重置必须经由 qm guest exec 注入到虚机内执行。这里仅准备好参数。
RESET="${PT_RESET:-1}"

# 0) 防止本工具多个实例同时运行：上一个没退出的实例会占用 guest-exec 通道，
#    导致新的 guest-exec 被 QEMU guest-agent 串行排队、长时间零输出卡死。
MYSELF=$$
for op in $(pgrep -f 'vm_install_kubeadm\.sh' 2>/dev/null || true); do
  [ "$op" = "$MYSELF" ] && continue
  echo "[*] 终止上一个未退出的部署实例(pid=$op)，释放 guest-exec 通道"
  kill "$op" 2>/dev/null; sleep 1; kill -9 "$op" 2>/dev/null || true
done
unset MYSELF

# 1) 虚机状态
st=$(qm status "$VMID" 2>/dev/null | awk '{print $2}')
if [ "$st" != "running" ]; then
  echo "[错误] 虚机 $VMID 当前状态为「${st:-未知}」，必须处于 running 才能安装。"
  echo "[提示] 请先在 PVE 中开机该虚机，再执行本工具。"
  exit 1
fi
echo "[1/6] 虚机状态: running"

# ---- 宿主机侧虚机电源/磁盘辅助（自愈复用） ----
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
  for part in $parts; do
    fstype=$(blkid -o value -s TYPE "$part" 2>/dev/null)
    case "$fstype" in ext2|ext3|ext4|xfs) ;; *) continue ;; esac
    if mount -t "$fstype" "$part" "$mnt" 2>/dev/null; then
      if [ -f "$mnt/etc/os-release" ]; then rootpart="$part"; break; fi
      umount "$mnt" 2>/dev/null
    fi
  done
  if [ -z "$rootpart" ]; then
    echo "[自愈] 未能在系统盘定位根文件系统"
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
ga_probe() {
  local iters="${1:-40}" i err
  for i in $(seq 1 "$iters"); do
    if err=$(timeout 5 qm guest exec "$VMID" -- true 2>&1 >/dev/null); then echo ready; return 0; fi
    if echo "$err" | grep -qi "guest-exec has been disabled"; then echo disabled; return 0; fi
    sleep 3
  done
  echo timeout
}

# 按「总时长」等待（比固定次数更适合重启后慢启动的场景）：每 3s 探一次，直到 ready/disabled 或超时
ga_probe_secs() {
  local limit="${1:-300}" start now err
  start=$(date +%s)
  while :; do
    if err=$(timeout 5 qm guest exec "$VMID" -- true 2>&1 >/dev/null); then echo ready; return 0; fi
    if echo "$err" | grep -qi "guest-exec has been disabled"; then echo disabled; return 0; fi
    now=$(date +%s)
    [ $((now - start)) -ge "$limit" ] && { echo timeout; return 0; }
    sleep 3
  done
}

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

# ---- 自愈 1：guest-exec 被禁用 ----
ga_autofix() {
  echo "[自愈] guest-exec 被禁用：挂盘移除黑名单（会重启虚机一次）..."
  vm_shutdown
  if ! vm_root_mount; then vm_start; return 1; fi
  local fixed=0 f
  for f in "$VM_ROOT_MNT/etc/sysconfig/qemu-ga" "$VM_ROOT_MNT/etc/default/qemu-guest-agent" "$VM_ROOT_MNT/etc/qemu-ga/qemu-ga.conf"; do
    [ -f "$f" ] && sed -i 's/guest-exec[-a-z]*,//g; s/,guest-exec[-a-z]*//g; s/guest-exec[-a-z]*//g' "$f" && fixed=1
  done
  [ "$fixed" = "1" ] || echo "[自愈] 黑名单配置里未发现 guest-exec"
  vm_root_umount
  echo "[自愈] 重新启动虚机..."
  vm_start
  [ "$fixed" = "1" ]
}

# ---- 自愈 2：SELinux enforcing 拦截 qemu-ga 域 ----
ga_selinux_fix() {
  echo "[自愈] 虚机内包管理器不可用，疑似 SELinux 拦截：挂盘置 disabled（会重启虚机一次）..."
  vm_shutdown
  if ! vm_root_mount; then vm_start; return 1; fi
  local cfg="$VM_ROOT_MNT/etc/selinux/config" fixed=0
  if [ -f "$cfg" ]; then
    if grep -qE '^SELINUX=enforcing' "$cfg"; then
      sed -i 's/^SELINUX=enforcing/SELINUX=disabled/' "$cfg" && fixed=1
      echo "[自愈] 已把虚机 SELinux 配置改为 disabled"
    else
      echo "[自愈] 虚机 SELinux 当前非 enforcing（未改动）"
    fi
  else
    echo "[自愈] 未找到 /etc/selinux/config"
  fi
  vm_root_umount
  echo "[自愈] 重新启动虚机..."
  vm_start
  [ "$fixed" = "1" ]
}

# 2) 等待 QEMU Guest Agent 就绪
echo "[2/6] 等待 QEMU Guest Agent 就绪（最多 300s）..."
ga_state=$(ga_probe_secs 300)
if [ "$ga_state" = "disabled" ]; then
  echo "[提示] guest-exec 被禁用，尝试自动修复..."
  if ga_autofix; then
    ga_state=$(ga_probe_secs 180)
  fi
fi
if [ "$ga_state" = "disabled" ]; then
  echo "[错误] guest-exec 被禁用且自动修复未成功。"
  echo "[修复] 进虚机执行（按系统）："
  echo "  Debian/Ubuntu: sed -i 's/guest-exec[-a-z]*,//g; s/,guest-exec[-a-z]*//g; s/guest-exec[-a-z]*//g' /etc/qemu-ga/qemu-ga.conf /etc/default/qemu-guest-agent; systemctl restart qemu-guest-agent"
  echo "  CentOS/RHEL:  sed -i 's/guest-exec[-a-z]*,//g; s/,guest-exec[-a-z]*//g; s/guest-exec[-a-z]*//g' /etc/sysconfig/qemu-ga; systemctl restart qemu-guest-agent"
  exit 1
fi
if [ "$ga_state" != "ready" ]; then
  echo "[错误] QEMU Guest Agent 未在预期时间内就绪。"
  echo "[诊断] 当前虚机状态 / agent 探测："
  qm status "$VMID" 2>&1 | sed 's/^/  [status] /' || true
  qm guest cmd "$VMID" ping 2>&1 | sed 's/^/  [agent] /' || true
  echo "[排查] 虚机可能仍在启动，或 guest 内 qemu-guest-agent 未运行："
  echo "  1) qm terminal $VMID 进控制台查看（openEuler/RHEL 的 qemu-guest-agent 是 static 单元，靠 virtio-serial 端口经 udev 触发，enable 无效）"
  echo "  2) 若未运行：systemctl start qemu-guest-agent"
  echo "  3) 确认 agent: enabled=1（qm config $VMID | grep agent）且已安装 qemu-guest-agent"
  exit 1
fi
echo "  QEMU Guest Agent 就绪"

# 3) 探测虚机内 OS
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
echo "[3/6] 虚机系统 ID=$ID"
if [ "$ID" = "windows" ]; then
  echo "[错误] 检测到 Windows 虚机。kubeadm 仅支持 Linux，本工具不适用。"
  exit 1
fi

# 4) 组装虚机内安装脚本（base64 传入；虚机内回显统一用英文，避免 QEMU 代理乱码）
GUEST_SCRIPT=$(cat <<'GUEST_EOF'
#!/usr/bin/env bash
set -e
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
ROLE='__ROLE__'
SERVER_URL='__SERVER_URL__'
JOIN_TOKEN='__TOKEN__'
CERT_KEY='__CERT_KEY__'
K8S_VERSION='__K8S_VERSION__'
K8S_MIRROR='__K8S_MIRROR__'
K8S_MIRROR_CUSTOM='__K8S_MIRROR_CUSTOM__'
OPEN_FW='__OPEN_FW__'
CGROUP_V2='__CGROUP_V2__'
SHELL_COMPLETION='__SHELL_COMPLETION__'
BACKUP_MANIFEST='__BACKUP_MANIFEST__'
CNI_VERSION_OVERRIDE='__CNI_VERSION_OVERRIDE__'
CALICO_POD_CIDR='__CALICO_POD_CIDR__'
CALICO_ENCAP='__CALICO_ENCAP__'
CALICO_AUTODETECT='__CALICO_AUTODETECT__'
FLANNEL_POD_CIDR='__FLANNEL_POD_CIDR__'
FLANNEL_BACKEND='__FLANNEL_BACKEND__'
FLANNEL_IFACE='__FLANNEL_IFACE__'
CILIUM_POD_CIDR='__CILIUM_POD_CIDR__'
CILIUM_ROUTING='__CILIUM_ROUTING__'
CILIUM_DEVICES='__CILIUM_DEVICES__'
RESET='__RESET__'
CNI='__CNI__'
CNI_MIRROR='__CNI_MIRROR__'
CNI_MIRROR_CUSTOM='__CNI_MIRROR_CUSTOM__'
K8S_REPO='__K8S_REPO__'
INIT_ENDPOINT='__INIT_ENDPOINT__'

echo "[*] ensure curl present"
if ! command -v curl >/dev/null 2>&1; then
  if command -v dnf >/dev/null 2>&1; then dnf install -y curl >/dev/null 2>&1
  elif command -v yum >/dev/null 2>&1; then yum install -y curl >/dev/null 2>&1
  elif command -v apt-get >/dev/null 2>&1; then apt-get update -y >/dev/null 2>&1; apt-get install -y curl >/dev/null 2>&1
  fi
fi

echo "[*] detect package manager"
if command -v dnf >/dev/null 2>&1; then PM=dnf
elif command -v yum >/dev/null 2>&1; then PM=yum
elif command -v apt-get >/dev/null 2>&1; then PM=apt
else echo "[ERROR] no supported package manager (dnf/yum/apt) found"; exit 1; fi
echo "[*] package manager: $PM"

# ---- cgroup v2 前置检查（kubelet Kubernetes>=1.25 拒绝 cgroup v1）----
CGV2=0
[ "$(stat -fc %T /sys/fs/cgroup/ 2>/dev/null)" = "cgroup2fs" ] && CGV2=1
if [ "$CGROUP_V2" = "1" ] && [ "$CGV2" != "1" ]; then
  echo "[*] 检测到 cgroup v1，kubelet 需要 cgroup v2；将内核命令行加入 systemd.unified_cgroup_hierarchy=1 并重启虚机"
  APPLIED=0
  APPLY_LOG=""
  # 0) 改前先备份 grub 配置与 BLS 启动项，万一写坏可按 .bak-webtools 恢复
  for f in /boot/grub2/grub.cfg /boot/grub/grub.cfg; do
    [ -f "$f" ] && cp -f "$f" "$f.bak-webtools" 2>/dev/null || true
  done
  [ -d /boot/loader/entries ] && cp -a /boot/loader/entries /boot/loader/entries.bak-webtools 2>/dev/null || true
  # 1) 修复 /etc/default/grub：确保参数落在引号内（早期 sed 曾把参数写到引号外导致失效），幂等自愈
  if [ -f /etc/default/grub ]; then
    python3 - <<'PY' 2>/dev/null || true
p='/etc/default/grub'
try:
    lines=open(p,encoding='utf-8').read().splitlines()
except Exception:
    pass
else:
    out=[]
    for line in lines:
        if line.startswith('GRUB_CMDLINE_LINUX='):
            rest=line[len('GRUB_CMDLINE_LINUX='):]
            toks=rest.replace('"',' ').split()
            if 'systemd.unified_cgroup_hierarchy=1' not in toks:
                toks.append('systemd.unified_cgroup_hierarchy=1')
            line='GRUB_CMDLINE_LINUX="%s"'%' '.join(toks)
        out.append(line)
    open(p,'w',encoding='utf-8').write("\n".join(out)+"\n")
PY
  fi
  # 2) 按启动机制选择「真正生效」的写法，且校验一律以「实际启动配置文件里 grep 到参数」为准：
  #    - BLS 系统（/boot/loader/entries/*.conf 非空）：grubby 改 BLS 启动项，校验 BLS 文件；
  #    - 非 BLS（BIOS + grub.cfg，如 openEuler 24.03）：必须 grub2-mkconfig 重新生成 grub.cfg。
  #    注意：grubby --info 在非 BLS 系统上可能「谎报」参数已写入（实际 grub.cfg 未变），
  #          之前正是因此跳过了 grub2-mkconfig，导致重启后 cmdline 仍无参数、反复重启。
  if ls /boot/loader/entries/*.conf >/dev/null 2>&1; then
    if command -v grubby >/dev/null 2>&1; then
      grubby --update-kernel=ALL --remove-args="systemd.unified_cgroup_hierarchy" >/dev/null 2>&1 || true
      grubby --update-kernel=ALL --args="systemd.unified_cgroup_hierarchy=1" >/dev/null 2>&1 || true
      if grep -qs "systemd.unified_cgroup_hierarchy=1" /boot/loader/entries/*.conf; then
        APPLIED=1; APPLY_LOG="BLS(/boot/loader/entries)"
      fi
    fi
  else
    GRUB_CFG=""
    if [ -f /boot/grub2/grub.cfg ]; then
      GRUB_CFG="/boot/grub2/grub.cfg"
    elif [ -f /boot/grub/grub.cfg ]; then
      GRUB_CFG="/boot/grub/grub.cfg"
    else
      for mp in $(awk '$3=="vfat"{print $2}' /proc/mounts) /boot /boot/efi; do
        for f in "$mp"/EFI/*/grub.cfg; do [ -f "$f" ] && { GRUB_CFG="$f"; break 2; }; done
      done
    fi
    [ -z "$GRUB_CFG" ] && GRUB_CFG="/boot/grub2/grub.cfg"
    if command -v grub2-mkconfig >/dev/null 2>&1; then
      grub2-mkconfig -o "$GRUB_CFG" >/dev/null 2>&1 || true
    elif command -v update-grub >/dev/null 2>&1; then
      update-grub >/dev/null 2>&1 || true
      if [ -f /boot/grub/grub.cfg ]; then GRUB_CFG="/boot/grub/grub.cfg"; fi
    fi
    if [ -f "$GRUB_CFG" ] && grep -qs "systemd.unified_cgroup_hierarchy=1" "$GRUB_CFG"; then
      APPLIED=1; APPLY_LOG="$GRUB_CFG"
    fi
  fi
  if [ "$APPLIED" = "1" ]; then
    echo "[*] 已写入内核命令行（grub 配置：$APPLY_LOG）"
    # 防死循环：参数已在当前启动命令行里却不生效（cgroup 仍 v1）→ 说明内核/systemd 已忽略该参数，
    # 再重启也没用，直接停止并给诊断，避免用户被反复重启。
    if tr ' ' '\n' < /proc/cmdline 2>/dev/null | grep -qx "systemd.unified_cgroup_hierarchy=1"; then
      echo "[ERROR] /proc/cmdline 已含 systemd.unified_cgroup_hierarchy=1，但 cgroup 仍为 v1，参数未生效"
      echo "[ERROR] 已中止，不再重启（避免重启死循环）。诊断如下："
      echo "[诊断] systemd 版本：$(systemctl --version 2>/dev/null | head -1)"
      echo "[诊断] /sys/fs/cgroup 类型：$(stat -fc %T /sys/fs/cgroup/ 2>/dev/null)"
      echo "[诊断] cgroup 挂载点："
      mount 2>/dev/null | grep -i cgroup | sed 's/^/  /' || true
      echo "[诊断] 内核是否支持 cgroup2：$(grep -qw cgroup2 /proc/filesystems 2>/dev/null && echo 支持 || echo 不支持)"
      echo "[建议] 该参数在部分新版 systemd 已废弃/移除；可尝试："
      echo "       1) 确认虚机镜像/系统本身支持 cgroup v2（内核 4.15+、systemd 247+）"
      echo "       2) Debian/Ubuntu 可在 /etc/default/grub 的 GRUB_CMDLINE_LINUX_DEFAULT 追加后 update-grub"
      echo "       3) 若系统仍强制 hybrid，需在 initramfs 层禁用 cgroup v1（cgroup_no_v1=all）后重建 initramfs"
      exit 1
    fi
    echo "[警告] 即将重启虚机以启用 cgroup v2；工具会自动等待并继续安装，请勿关闭本窗口"
    # 用 systemd-run 定时触发重启（由 systemd 自身管理，不受 guest exec 会话结束影响）；不可用则回退 setsid/nohup
    if command -v systemd-run >/dev/null 2>&1; then
      systemd-run --no-block --on-active=5s systemctl reboot -f >/dev/null 2>&1 || systemctl reboot -f
    elif command -v setsid >/dev/null 2>&1; then
      setsid sh -c 'sleep 5; systemctl reboot -f' >/dev/null 2>&1 &
    else
      nohup sh -c 'sleep 5; systemctl reboot -f' >/dev/null 2>&1 &
    fi
    sleep 1
    exit 75
  else
    echo "[ERROR] 无法将 systemd.unified_cgroup_hierarchy=1 写入任何 grub 配置（grubby / grub2-mkconfig 均未能生效）"
    echo "[ERROR] 已中止，未重启虚机。可手动处理："
    echo "        grubby --update-kernel=ALL --args=systemd.unified_cgroup_hierarchy=1"
    echo "  或  编辑 /etc/default/grub 的 GRUB_CMDLINE_LINUX 追加该参数后 grub2-mkconfig -o /boot/grub2/grub.cfg"
    echo "  若曾被写坏，可用备份恢复：/boot/grub2/grub.cfg.bak-webtools、/boot/loader/entries.bak-webtools"
    echo "  当前 /proc/cmdline："
    cat /proc/cmdline 2>/dev/null | tr ' ' '\n' | grep -i cgroup || echo "  (无 cgroup 相关内核参数)"
    echo "[ERROR] 候选 grub 配置存在情况："
    ls -l /boot/grub2/grub.cfg /boot/grub/grub.cfg /boot/efi/EFI/*/grub.cfg 2>/dev/null || true
    exit 1
  fi
fi
if [ "$CGV2" != "1" ]; then
  echo "[警告] cgroup v2 未启用（仍为 v1），kubelet 将拒绝启动；如需自动修正请勾选 cgroup v2 前置修正后重跑"
fi

# ---- 部署前重置（默认开启）：在虚机内清掉旧集群残留，避免 admin 证书异常的旧集群被静默复用 ----
# 必须放在虚机内：本工具宿主机侧直接执行 kubeadm reset 对虚机无效。放在 cgroup 检测之后，
# 这样 cgroup v1 触发重启(退出)的那一轮不会重置、重启后下一轮才真正重置并安装，流程更干净。
if [ "$RESET" = "1" ]; then
  echo "[*] 部署前重置虚机内已有 k8s 残留（kubeadm reset -f + 清理数据目录）"
  if command -v kubeadm >/dev/null 2>&1; then
    kubeadm reset -f >/dev/null 2>&1 || true
  fi
  rm -rf /etc/kubernetes /var/lib/etcd /var/lib/kubelet 2>/dev/null || true
  echo "[*] 重置完成"
fi

# ---- 关闭 swap（永久注释 fstab 里的 swap，避免重启后 swap 重新挂载导致 kubelet 拒绝启动，
#      这是 K8s 重启起不来的最常见坑；kubelet/containerd 均已 systemctl enable，重启自动恢复）----
swapoff -a 2>/dev/null || true
sed -i '/\bswap\b/s/^\([^#].*\)$/#\1/' /etc/fstab 2>/dev/null || true

# ---- 内核模块 + sysctl ----
mkdir -p /etc/modules-load.d
cat > /etc/modules-load.d/k8s.conf <<'M'
overlay
br_netfilter
vxlan
M
modprobe overlay br_netfilter vxlan 2>/dev/null || true
cat > /etc/sysctl.d/k8s.conf <<'S'
net.bridge.bridge-nf-call-iptables=1
net.bridge.bridge-nf-call-ip6tables=1
net.ipv4.ip_forward=1
S
# 关键：/etc/sysctl.conf 在 sysctl --system 里最后应用、优先级最高；openEuler 默认把它写成 ip_forward=0，
# 会覆盖上面的 k8s.conf，导致 ip_forward 归 0、NodePort 外部访问超时（重启后必现）。这里把它也改成 1。
if [ -f /etc/sysctl.conf ]; then
  sed -i 's|^[[:space:]]*net\.ipv4\.ip_forward[[:space:]]*=.*|net.ipv4.ip_forward = 1|' /etc/sysctl.conf || true
  grep -q '^[[:space:]]*net\.ipv4\.ip_forward' /etc/sysctl.conf || echo 'net.ipv4.ip_forward = 1' >> /etc/sysctl.conf
fi
sysctl --system >/dev/null 2>&1 || true
# 应用完所有 sysctl 配置后再显式落一次运行态，确保最终为 1（不被任何后应用的配置文件覆盖）
sysctl -w net.bridge.bridge-nf-call-iptables=1 >/dev/null 2>&1 || true
sysctl -w net.bridge.bridge-nf-call-ip6tables=1 >/dev/null 2>&1 || true
sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true

# ---- 安装 containerd ----
echo "[*] installing containerd"
if ! command -v containerd >/dev/null 2>&1; then
  case "$PM" in
    dnf|yum) $PM install -y containerd ;;
    apt)
      # containerd.io 来自 docker-ce 官方仓库，不在 Ubuntu 默认源中，必须先加源
      case "$K8S_REPO" in
        aliyun) DOCKER_DEB_MIRROR="mirrors.aliyun.com/docker-ce/linux/ubuntu" ;;
        tuna)   DOCKER_DEB_MIRROR="mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/ubuntu" ;;
        ustc)   DOCKER_DEB_MIRROR="mirrors.ustc.edu.cn/docker-ce/linux/ubuntu" ;;
        *)      DOCKER_DEB_MIRROR="download.docker.com/linux/ubuntu" ;;
      esac
      apt-get update -y >/dev/null 2>&1
      apt-get install -y apt-transport-https ca-certificates curl gnupg >/dev/null 2>&1 || true
      echo "deb [trusted=yes] https://${DOCKER_DEB_MIRROR} $( . /etc/os-release && echo "$VERSION_CODENAME" ) stable" > /etc/apt/sources.list.d/docker-ce.list
      apt-get update -y >/dev/null 2>&1
      apt-get install -y containerd.io
      ;;
  esac
fi
mkdir -p /etc/containerd
containerd config default > /etc/containerd/config.toml 2>/dev/null || true
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
sed -i 's#sandbox_image = ".*"#sandbox_image = "registry.aliyuncs.com/google_containers/pause:3.9"#' /etc/containerd/config.toml
systemctl enable --now containerd
sleep 3
ctr -n k8s.io version >/dev/null 2>&1 || { echo "[ERROR] containerd 未就绪"; exit 1; }

# ---- k8s 镜像仓库（控制面镜像 + 软件源）----
case "$K8S_MIRROR" in
  aliyun) IMG_REPO="registry.aliyuncs.com/google_containers";;
  google) IMG_REPO="registry.k8s.io";;
  custom) IMG_REPO="$K8S_MIRROR_CUSTOM";;
  *) IMG_REPO="registry.aliyuncs.com/google_containers";;
esac

# ---- 安装 kubeadm/kubelet/kubectl ----
echo "[*] installing kubeadm/kubelet/kubectl (包源=$K8S_REPO, 镜像=$K8S_MIRROR)"
K8S_RPM="${K8S_VERSION#v}"
K8S_MINOR=$(printf '%s' "$K8S_RPM" | cut -d. -f1,2)   # 1.30.5 / 1.30 -> 1.30
# 版本号只给到分支(如 1.37)时锁定不了补丁号：装该分支最新补丁；给到 x.y.z 时按补丁精确装
case "$K8S_RPM" in
  *.*.*) K8S_PIN="-$K8S_RPM" ;;
  *)     K8S_PIN="" ;;
esac
# 软件包源(kubeadm/kubelet/kubectl)选择：均为新结构源(带 v1.XX 路径)，覆盖 1.24+ 全版本。
case "$K8S_REPO" in
  aliyun)         REPO_RPM="https://mirrors.aliyun.com/kubernetes-new/core/stable/v${K8S_MINOR}/rpm/"
                    REPO_DEB_LINE="deb [trusted=yes] https://mirrors.aliyun.com/kubernetes-new/core/stable/v${K8S_MINOR}/deb/ /" ;;
  tuna)           REPO_RPM="https://mirrors.tuna.tsinghua.edu.cn/kubernetes/core:/stable:/v${K8S_MINOR}/rpm/"
                    REPO_DEB_LINE="deb [trusted=yes] https://mirrors.tuna.tsinghua.edu.cn/kubernetes/core:/stable:/v${K8S_MINOR}/deb/ /" ;;
  ustc)           REPO_RPM="https://mirrors.ustc.edu.cn/kubernetes/core:/stable:/v${K8S_MINOR}/rpm/"
                    REPO_DEB_LINE="deb [trusted=yes] https://mirrors.ustc.edu.cn/kubernetes/core:/stable:/v${K8S_MINOR}/deb/ /" ;;
  official)       REPO_RPM="https://pkgs.k8s.io/core:/stable:/v${K8S_MINOR}/rpm/"
                    REPO_DEB_LINE="deb [trusted=yes] https://pkgs.k8s.io/core:/stable:/v${K8S_MINOR}/deb/ /" ;;
  *)              REPO_RPM="https://mirrors.aliyun.com/kubernetes-new/core/stable/v${K8S_MINOR}/rpm/"
                    REPO_DEB_LINE="deb [trusted=yes] https://mirrors.aliyun.com/kubernetes-new/core/stable/v${K8S_MINOR}/deb/ /" ;;
esac
case "$PM" in
  dnf|yum)
    cat > /etc/yum.repos.d/kubernetes.repo <<R
[kubernetes]
name=Kubernetes
baseurl=${REPO_RPM}
enabled=1
gpgcheck=0
R
    $PM install -y kubelet${K8S_PIN} kubeadm${K8S_PIN} kubectl${K8S_PIN} 2>/dev/null || $PM install -y kubelet kubeadm kubectl
    ;;
  apt)
    apt-get update -y >/dev/null 2>&1
    apt-get install -y apt-transport-https ca-certificates curl gnupg
    echo "$REPO_DEB_LINE" > /etc/apt/sources.list.d/kubernetes.list
    apt-get update -y >/dev/null 2>&1
    apt-get install -y kubelet kubeadm kubectl
    ;;
esac
# 以实际安装的版本为准（源里可能没有工具默认要求的版本，例如 aliyun el7 源只有 1.28.x）
K8S_INSTALLED=$(kubelet --version 2>/dev/null | awk '{print $2}')
if [ -n "$K8S_INSTALLED" ]; then
  echo "[*] 实际安装 kubeadm/kubelet 版本: $K8S_INSTALLED（用于 kubeadm init，覆盖默认要求版本）"
  K8S_VERSION="$K8S_INSTALLED"
fi

# ---- CNI 组件版本：按 K8s minor 推荐，支持「CNI 版本」参数覆盖 ----
# 官方支持窗口基线：1.28~1.30 为已确认值；1.31+ 未确认，沿用默认并在下面告警。
# 需按官方支持矩阵调整时，直接在「CNI 版本」参数里填具体版本即可。
K8S_MINOR=$(printf '%s' "${K8S_VERSION#v}" | cut -d. -f1,2)
DEF_CALICO=""; DEF_CILIUM=""
case "$K8S_MINOR" in
  1.24) DEF_CALICO="v3.26.0"; DEF_CILIUM="v1.14.0";;
  1.25) DEF_CALICO="v3.26.0"; DEF_CILIUM="v1.14.0";;
  1.26) DEF_CALICO="v3.26.0"; DEF_CILIUM="v1.15.0";;
  1.27) DEF_CALICO="v3.28.0"; DEF_CILIUM="v1.16.0";;
  1.28) DEF_CALICO="v3.28.0"; DEF_CILIUM="v1.16.0";;
  1.29) DEF_CALICO="v3.29.0"; DEF_CILIUM="v1.17.0";;
  1.30) DEF_CALICO="v3.29.0"; DEF_CILIUM="v1.18.0";;
  1.31) DEF_CALICO="v3.30.0"; DEF_CILIUM="v1.19.0";;
  1.32) DEF_CALICO="v3.31.0"; DEF_CILIUM="v1.19.0";;
  1.33) DEF_CALICO="v3.31.0"; DEF_CILIUM="v1.20.0";;
  1.34) DEF_CALICO="v3.32.0"; DEF_CILIUM="v1.20.0";;
  1.35) DEF_CALICO="v3.33.0"; DEF_CILIUM="v1.20.0";;
  1.36) DEF_CALICO="v3.33.0"; DEF_CILIUM="v1.20.0";;
  1.37) DEF_CALICO="v3.33.0"; DEF_CILIUM="";;        # Cilium 1.20 只 e2e 到 1.36
  *)    DEF_CALICO="v3.28.0"; DEF_CILIUM="";;        # 兜底（下拉不含 1.24 以下）
esac
DEF_FLANNEL="v0.25.5"; DEF_FLANNEL_CNI="v1.5.1-flannel1"
CILIUM_UNTESTED=0
if [ -z "$DEF_CILIUM" ]; then CILIUM_UNTESTED=1; fi

CALICO_VER="${CNI_VERSION_OVERRIDE:-$DEF_CALICO}"
CILIUM_VER="${CNI_VERSION_OVERRIDE:-${DEF_CILIUM:-v1.20.0}}"
FLANNEL_VER="${CNI_VERSION_OVERRIDE:-$DEF_FLANNEL}"
FLANNEL_CNI_VER="$DEF_FLANNEL_CNI"   # 插件版本单独固定，覆盖仅作用于主镜像
case "$CALICO_VER" in v*) ;; *) CALICO_VER="v$CALICO_VER";; esac
case "$CILIUM_VER" in v*) ;; *) CILIUM_VER="v$CILIUM_VER";; esac
case "$FLANNEL_VER" in v*) ;; *) FLANNEL_VER="v$FLANNEL_VER";; esac
if [ "$CILIUM_UNTESTED" = "1" ] && [ "$CNI" = "cilium" ] && [ -z "${CNI_VERSION_OVERRIDE:-}" ]; then
  echo "[警告] K8s $K8S_MINOR 无 Cilium 官方 e2e 支持版本（1.20 只测到 1.36），暂用 $CILIUM_VER；建议改用 Calico/Flannel 或用「CNI 版本」指定"
fi
echo "[*] CNI 版本: 目标=$CNI  Calico=$CALICO_VER  Cilium=$CILIUM_VER  Flannel=$FLANNEL_VER"
# 沙箱(pause)镜像必须与 k8s 版本匹配：前面按 1.30 时代写死的 pause:3.9 在新版本上会被 kubelet
# 判为过旧（1.31+ 最低 3.10），这里以 kubeadm 为该版本解析出的 pause 覆盖掉。
PAUSE_IMG=$(kubeadm config images list --kubernetes-version "$K8S_VERSION" --image-repository "$IMG_REPO" 2>/dev/null | grep -m1 '/pause:')
if [ -n "$PAUSE_IMG" ]; then
  echo "[*] 沙箱镜像: $PAUSE_IMG"
  sed -i "s#sandbox_image = \"[^\"]*\"#sandbox_image = \"${PAUSE_IMG}\"#" /etc/containerd/config.toml
  systemctl restart containerd
  sleep 3
fi
systemctl enable --now kubelet

# ---- 安装标准 CNI 插件（/opt/cni/bin：loopback/portmap/host-local/bridge 等）----
# kubeadm 不会自动安装；缺失会导致普通 Pod(非 hostNetwork) 建沙箱失败: failed to find plugin "loopback" in path [/opt/cni/bin]
# 优先离线（多数系统已在 /usr/libexec/cni 自带），避免 github/ghproxy 不通时下载挂死。
if [ ! -x /opt/cni/bin/loopback ]; then
  mkdir -p /opt/cni/bin
  # 1) 系统自带（containernetworking-plugins/kubernetes-cni 常装到 /usr/libexec/cni），直接复制，离线且最快
  if [ -x /usr/libexec/cni/loopback ]; then
    cp -a /usr/libexec/cni/. /opt/cni/bin/ 2>/dev/null || true
  fi
  # 2) 从包源安装（kubernetes-cni 的 x86_64 包在 aliyun kubernetes 源里）
  if [ ! -x /opt/cni/bin/loopback ]; then
    if [ "$PM" = "dnf" ] || [ "$PM" = "yum" ]; then
      $PM install -y kubernetes-cni 2>/dev/null || $PM install -y containernetworking-plugins 2>/dev/null || true
    elif [ "$PM" = "apt" ]; then
      apt-get update -y >/dev/null 2>&1 || true; apt-get install -y containernetworking-plugins 2>/dev/null || true
    fi
  fi
  # 3) 兜底：下载 tarball（必须带连接/总超时，避免网络不通时永久挂死）
  if [ ! -x /opt/cni/bin/loopback ]; then
    CNI_VER="v1.5.1"
    for p in "https://mirrors.aliyun.com/containernetworking/plugins/releases/download/${CNI_VER}/cni-plugins-linux-amd64-${CNI_VER}.tgz" \
             "https://mirror.ghproxy.com/https://github.com/containernetworking/plugins/releases/download/${CNI_VER}/cni-plugins-linux-amd64-${CNI_VER}.tgz" \
             "https://ghfast.top/https://github.com/containernetworking/plugins/releases/download/${CNI_VER}/cni-plugins-linux-amd64-${CNI_VER}.tgz" \
             "https://github.com/containernetworking/plugins/releases/download/${CNI_VER}/cni-plugins-linux-amd64-${CNI_VER}.tgz"; do
      if curl -fsSL --connect-timeout 8 --max-time 90 -o /tmp/cni-plugins.tgz "$p" 2>/dev/null; then
        tar -C /opt/cni/bin -xzf /tmp/cni-plugins.tgz 2>/dev/null && break || true
      fi
    done
    rm -f /tmp/cni-plugins.tgz
  fi
  [ -x /opt/cni/bin/loopback ] || echo "[警告] CNI 插件安装失败：coredns 可能无法启动（请检查 /usr/libexec/cni 或 kubernetes-cni 包）"
fi
echo "[*] CNI plugins: $(ls /opt/cni/bin 2>/dev/null | tr '\n' ' ')"

# ---- 防火墙放行（仅当 firewalld/ufw 存在时）----
if [ "$OPEN_FW" = "1" ]; then
  # 8472/udp=Flannel VXLAN；4789/udp=Calico VXLAN；179/tcp=Calico BGP；IPIP 为协议号 4（按需）
  if command -v firewall-cmd >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port=6443/tcp
    firewall-cmd --permanent --add-port=10250/tcp
    firewall-cmd --permanent --add-port=10259/tcp
    firewall-cmd --permanent --add-port=10257/tcp
    firewall-cmd --permanent --add-port=8472/udp
    firewall-cmd --permanent --add-port=4789/udp
    firewall-cmd --permanent --add-port=6081/udp
    firewall-cmd --permanent --add-port=179/tcp
    firewall-cmd --permanent --add-port=4240/tcp
    if [ "$CNI" = "calico" ] && [ "$CALICO_ENCAP" = "ipip" ]; then
      firewall-cmd --permanent --add-protocol=ipip >/dev/null 2>&1 || true
    fi
    if [ "$CNI" = "flannel" ] && [ "$FLANNEL_BACKEND" = "wireguard" ]; then
      firewall-cmd --permanent --add-port=51820/udp >/dev/null 2>&1 || true
    fi
    firewall-cmd --permanent --add-port=30000-32767/tcp
    firewall-cmd --reload
  elif command -v ufw >/dev/null 2>&1; then
    ufw allow 6443/tcp; ufw allow 10250/tcp; ufw allow 10259/tcp; ufw allow 10257/tcp
    ufw allow 8472/udp; ufw allow 4789/udp; ufw allow 6081/udp; ufw allow 179/tcp; ufw allow 4240/tcp
    if [ "$CNI" = "calico" ] && [ "$CALICO_ENCAP" = "ipip" ]; then
      ufw allow proto ipip >/dev/null 2>&1 || true
    fi
    if [ "$CNI" = "flannel" ] && [ "$FLANNEL_BACKEND" = "wireguard" ]; then
      ufw allow 51820/udp >/dev/null 2>&1 || true
    fi
    ufw allow 30000:32767/tcp
  fi
fi

# 预拉单个镜像：依次尝试若干镜像站（docker.io 类与 quay 类分别传对应站），拉到即停
_pull_img() {
  local img="$1"; shift
  local pulled=0
  for m in "$@"; do
    # 先用 curl -4 快速探测该镜像站 IPv4 是否可达：很多环境 DNS 同时返回 A/AAAA，
    # ctr(Go) 走 IPv6 会卡到超时（典型现象：dial tcp [2a03:...]:443: i/o timeout）；
    # 探测不通直接跳过，避免 ctr 在 IPv6 假死时挂 600s 卡死整个部署。
    if ! curl -4 -s -o /dev/null --connect-timeout 6 --max-time 10 "https://$m/v2/" 2>/dev/null; then
      echo "[警告] 镜像站 $m IPv4 不可达，跳过"
      continue
    fi
    # ctr pull 自身无连接/总超时，镜像站「能建连但不响应」时会无限挂起、卡死整个部署；
    # 用 timeout 兜底（单镜像最多 600s，虚机到国内镜像站冷拉首次可能 >90s），拉不到就换下一个站，
    # 全部失败则运行期由容器运行时回源。
    if timeout 600 ctr -n k8s.io images pull "$m/$img" >/dev/null 2>&1; then
      # ctr images ls 默认输出是「ref + 类型 + digest…」多列，ref 不在行尾；
      # 之前 grep -E "/$img$" 因行尾锚定永远匹配不到，导致已拉成功也误报失败，并把所有镜像站白跑一遍。
      # 用 --quiet 只输出镜像名，再做精确匹配。
      if ctr -n k8s.io images ls -q | grep -qx "$m/$img"; then pulled=1; break; fi
    fi
  done
  [ "$pulled" = "1" ] && echo "[*] $img 预拉取完成" || echo "[警告] $img 镜像拉取失败（运行时会回源重试）"
}

# ---- 预拉 CNI 镜像（所有节点都拉，避免 join/新节点启动 CNI Pod 时回源被墙）----
case "$CNI" in
  calico)
    echo "[*] pre-pulling calico images"
    for img in "calico/cni:$CALICO_VER" "calico/node:$CALICO_VER" "calico/kube-controllers:$CALICO_VER"; do
      _pull_img "$img" "docker.m.daocloud.io" "docker.1ms.run" "mirror.ccs.tencentyun.com" "docker.nju.edu.cn" "dockerproxy.com"
    done
    ;;
  cilium)
    echo "[*] pre-pulling cilium images"
    _pull_img "cilium/cilium:$CILIUM_VER" "quay.m.daocloud.io"
    ;;
  *)
    echo "[*] pre-pulling flannel images"
    for img in "flannel/flannel:$FLANNEL_VER" "flannel/flannel-cni-plugin:$FLANNEL_CNI_VER"; do
      _pull_img "$img" "docker.m.daocloud.io" "docker.1ms.run" "mirror.ccs.tencentyun.com" "docker.nju.edu.cn" "dockerproxy.com"
    done
    ;;
esac

# ---- 控制面 IP 稳定性检测（防 DHCP 动态地址导致重启后证书 SAN 不匹配、控制面崩溃）----
# 仅对「本节点将作为控制面(kubeadm init)」拦截：worker/join 节点(SERVER_URL 非空)不钉证书，无需此检查。
# 判据：主网卡 IPv4 若在 ip addr 中带 dynamic 标志，即 DHCP 动态分配；一旦重启重新分配（多台虚机甚至互换地址），
#       kubeadm 初始化时钉进证书 SAN / 清单的地址就与实际不符，etcd / kube-apiserver 无法绑定而 CrashLoopBackOff。
check_static_ip() {
  local line ip
  line=$(ip -o -4 addr show scope global 2>/dev/null | head -1)
  ip=$(printf '%s' "$line" | awk '{print $4}' | cut -d/ -f1)
  if printf '%s' "$line" | grep -qw 'dynamic'; then
    echo "[ERROR] 检测到控制面节点主 IP ($ip) 由 DHCP 动态分配（ip addr 含 dynamic 标志）。"
    echo "[ERROR] 安装后机器重启、DHCP 重新分配地址（多台虚机甚至可能互换地址）会导致证书 SAN 与实际 IP 不匹配，"
    echo "[ERROR] etcd / kube-apiserver 无法绑定而 CrashLoopBackOff，集群起不来。"
    echo "[处理] 请先给本虚机配置静态 IP（或路由器/DHCP 做地址保留），保证该 IP 永久不变，再重跑本工具。"
    return 1
  fi
  echo "[*] 控制面节点主 IP: $ip（静态/非 dynamic，符合安装前提）"
  return 0
}

# ---- init 或 join ----
if [ -z "$SERVER_URL" ]; then
  if [ -f /etc/kubernetes/admin.conf ]; then
    echo "[*] 已存在 admin.conf，跳过 kubeadm init（幂等）"
  else
    check_static_ip || exit 1
    echo "[*] kubeadm init (version=$K8S_VERSION, repo=$IMG_REPO)"
    # preflight 前再显式确认运行态（早期的 sysctl 设置可能被网络/服务重置，尤其 ip_forward）
    modprobe br_netfilter 2>/dev/null || true
    sysctl -w net.bridge.bridge-nf-call-iptables=1 >/dev/null 2>&1 || true
    sysctl -w net.bridge.bridge-nf-call-ip6tables=1 >/dev/null 2>&1 || true
    sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true
    # 配置 API 版本跟着 kubeadm 走：v1beta3 在 1.31+ 已弃用、更高版本已移除，
    # 不按版本号猜，直接问装好的 kubeadm（config print 纯本地渲染，不联网）
    KADM_API=$(kubeadm config print init-defaults 2>/dev/null | awk '/^apiVersion: kubeadm.k8s.io\//{print $2; exit}')
    if [ -z "$KADM_API" ]; then KADM_API="kubeadm.k8s.io/v1beta3"; fi
    echo "[*] kubeadm 配置 API 版本: $KADM_API"
    # Pod 网段随 CNI 走（需与对应 CNI 默认 IPAM 一致，否则 Pod 拿不到 IP）
    case "$CNI" in
      calico)
        POD_CIDR="192.168.0.0/16"
        if [ -n "$CALICO_POD_CIDR" ]; then POD_CIDR="$CALICO_POD_CIDR"; fi
        ;;
      cilium)
        POD_CIDR="10.0.0.0/16"
        if [ -n "$CILIUM_POD_CIDR" ]; then POD_CIDR="$CILIUM_POD_CIDR"; fi
        ;;
      flannel)
        POD_CIDR="10.244.0.0/16"
        if [ -n "$FLANNEL_POD_CIDR" ]; then POD_CIDR="$FLANNEL_POD_CIDR"; fi
        ;;
      *)       POD_CIDR="10.244.0.0/16" ;;
    esac
    cat > /tmp/kubeadm-init.yaml <<INITCFG_EOF
apiVersion: ${KADM_API}
kind: InitConfiguration
nodeRegistration:
  criSocket: unix:///run/containerd/containerd.sock
---
apiVersion: ${KADM_API}
kind: ClusterConfiguration
kubernetesVersion: ${K8S_VERSION}
imageRepository: ${IMG_REPO}
${INIT_ENDPOINT:+controlPlaneEndpoint: ${INIT_ENDPOINT}}
networking:
  podSubnet: ${POD_CIDR}
---
apiVersion: kubeproxy.config.k8s.io/v1alpha1
kind: KubeProxyConfiguration
mode: iptables
INITCFG_EOF
    kubeadm init --config /tmp/kubeadm-init.yaml 2>&1 | tail -40
  fi
  KUBE_HOME=$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6) || true
  [ -n "$KUBE_HOME" ] || KUBE_HOME=/root
  mkdir -p "$KUBE_HOME/.kube" 2>/dev/null || true
  # 注意：不能用 cp -i（目标已存在时非交互读到 EOF 会返回非 0，配合 set -e 会让重跑直接失败）
  cp -f /etc/kubernetes/admin.conf "$KUBE_HOME/.kube/config" 2>/dev/null || true
  chown "$(id -u):$(id -g)" "$KUBE_HOME/.kube/config" 2>/dev/null || true
  export KUBECONFIG=/etc/kubernetes/admin.conf
  kubectl taint nodes --all node-role.kubernetes.io/control-plane- 2>/dev/null || true
  echo "[*] 后续向本集群追加节点，请在「架构形态」选「节点扩容」并对目标虚机填写下方集群入口地址+Token 重新运行本工具："
  kubeadm token create --print-join-command 2>/dev/null || true
  # ---- CNI 镜像源选择：优先国内镜像源，探测不通自动回退官方（docker.io / quay.io）----
  # K8s 官方镜像仓库(registry.aliyuncs.com/google_containers)不含 calico/cilium/flannel 镜像，
  # 它们分布在 docker.io / quay.io；国内直连常拉不动，故部署前实测选源并替换清单内镜像前缀。
  # $1=镜像路径(不含域名) $2=官方域名 $3...=候选镜像源（按优先级）；逐个「/v2/ 探测 + 实拉确认」，
  # 能建连但不吐数据的假死源会被实拉(45s 超时)淘汰；全部失败回退 $2 官方。
  pick_cni_registry() {
    local img_path="$1" official="$2"; shift 2
    local cand code
    for cand in "$@" "$official"; do
      [ -n "$cand" ] || continue
      # -4 强制走 IPv4：很多环境 IPv6 能建连但不响应，会卡到超时（dial tcp [2a03:...]:443: i/o timeout），
      # 用 IPv4 实测更能反映「镜像站是否真能拉到镜像」。
      code=$(curl -4 -s -o /dev/null --connect-timeout 6 --max-time 10 -w '%{http_code}' "https://$cand/v2/" 2>/dev/null)
      if [ "$code" = "200" ] || [ "$code" = "401" ]; then
        if timeout 600 ctr -n k8s.io images pull "$cand/$img_path" >/dev/null 2>&1; then
          echo "$cand"; return 0
        fi
      fi
    done
    echo "$official"
  }

  # ---- CNI 部署函数（必须在虚机内定义：本脚本经 qm guest exec 在虚机 bash 进程内执行，
  #      宿主脚本里定义的同名函数虚机看不到，会报 command not found。故函数体写在 GUEST 脚本内）----
  deploy_cni_calico() {
    echo "[*] 部署 Calico CNI"
    local ok=0
    for u in \
      "https://cdn.jsdelivr.net/gh/projectcalico/calico@$CALICO_VER/manifests/calico.yaml" \
      "https://raw.githubusercontent.com/projectcalico/calico/$CALICO_VER/manifests/calico.yaml" \
      "https://mirror.ghproxy.com/https://raw.githubusercontent.com/projectcalico/calico/$CALICO_VER/manifests/calico.yaml" \
      "https://ghfast.top/https://raw.githubusercontent.com/projectcalico/calico/$CALICO_VER/manifests/calico.yaml"; do
      if curl -fsSL --connect-timeout 12 --max-time 600 -o /tmp/calico.yaml "$u"; then ok=1; break; fi
    done
    if [ "$ok" = "1" ]; then
      # 优化：按参数改清单里的 calico-node 环境变量（Pod 网段 / 封装方式 / 节点 IP 探测）
      python3 - "$POD_CIDR" "$CALICO_ENCAP" "$CALICO_AUTODETECT" <<'PY' 2>/dev/null || true
import sys, re
cidr, encap, autodetect = sys.argv[1], sys.argv[2], sys.argv[3]
want = {
    'CALICO_IPV4POOL_CIDR': cidr,
    'CALICO_IPV4POOL_IPIP': 'Never' if encap == 'vxlan' else 'Always',
    'CALICO_IPV4POOL_VXLAN': 'Always' if encap == 'vxlan' else 'Never',
    'IP_AUTODETECTION_METHOD': autodetect,
}
p = '/tmp/calico.yaml'
try:
    lines = open(p, encoding='utf-8').read().splitlines()
except Exception:
    raise SystemExit(0)
out = []
i = 0
while i < len(lines):
    line = lines[i]
    m = re.match(r'^(\s*)#?\s*-\s*name:\s*([A-Z0-9_]+)\s*$', line)
    if m and m.group(2) in want:
        name, indent = m.group(2), m.group(1)
        j = i + 1
        skip = []
        while j < len(lines) and not re.match(r'^\s*#?\s*value:', lines[j]):
            if re.match(r'^\s*#?\s*-\s*name:', lines[j]):
                break
            skip.append(lines[j]); j += 1
        if j < len(lines) and re.match(r'^\s*#?\s*value:', lines[j]):
            out.append('%s- name: %s' % (indent, name))
            out.extend(skip)
            out.append('%s  value: "%s"' % (indent, want[name]))
            i = j
        else:
            out.append(line)
    else:
        out.append(line)
    i += 1
open(p, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
PY
      # 校验：显式指定了 Pod 网段就必须确认清单已对齐，否则不 apply（避免与 kubeadm podSubnet 不一致）
      if [ -n "$CALICO_POD_CIDR" ]; then
        if ! grep -A1 "name: CALICO_IPV4POOL_CIDR" /tmp/calico.yaml 2>/dev/null | grep -qF "value: \"$POD_CIDR\""; then
          echo "[ERROR] 清单中 CALICO_IPV4POOL_CIDR 未能对齐到 $POD_CIDR，已中止（未 apply）"
          return 1
        fi
        echo "[*] Calico Pod 网段: $POD_CIDR  封装: $CALICO_ENCAP  探测: $CALICO_AUTODETECT"
      fi
      # calico 镜像在 docker.io/calico：按所选 CNI 镜像源实测选源并替换清单前缀，探测不通回退官方
      local reg
      case "$CNI_MIRROR" in
        daocloud) reg=$(pick_cni_registry "calico/cni:$CALICO_VER" "docker.io" "docker.m.daocloud.io") ;;
        163|baidu) reg="docker.io" ;;   # 网易/百度仅代理 library/* 且多已不可用，退化为官方回退
        official) reg="docker.io" ;;
        custom)   reg=$(pick_cni_registry "calico/cni:$CALICO_VER" "docker.io" "$CNI_MIRROR_CUSTOM") ;;
        *)        reg=$(pick_cni_registry "calico/cni:$CALICO_VER" "docker.io" "docker.m.daocloud.io" "docker.1ms.run" "mirror.ccs.tencentyun.com" "docker.nju.edu.cn" "dockerproxy.com") ;;
      esac
      if [ "$reg" != "docker.io" ]; then
        echo "[*] Calico 镜像源: $reg"
        sed -i "s#docker\.io/calico#$reg/calico#g" /tmp/calico.yaml
      else
        echo "[警告] 所有 docker.io 镜像源均不可达，Calico 镜像将回退 docker.io 直连；"
        echo "[警告] 若虚机网络无法访问 docker.io（常见：实验室网络仅放行阿里云），Calico Pod 会 ImagePullBackOff。"
        echo "[警告] 解决：在「CNI 镜像源」选 custom 并填写可达镜像源（如内网代理/私有仓库），或保证网络能访问 docker.io 后重跑。"
      fi
      kubectl apply -f /tmp/calico.yaml || echo "[警告] Calico 清单 apply 失败，请检查集群状态"
      echo "[*] 等待 Calico DaemonSet 就绪..."
      for i in $(seq 1 36); do kubectl -n kube-system get ds calico-node >/dev/null 2>&1 && break; sleep 5; done
    else
      echo "[警告] Calico 清单下载失败，CNI 未部署（集群将无网络，请检查 GitHub 镜像站连通性）"
    fi
  }
  deploy_cni_cilium() {
    echo "[*] 部署 Cilium CNI"
    local ok=0
    # 优先用 cilium CLI 渲染清单（classic 模式，不依赖 Helm）。
    # 官方 quick-install.yaml 已从仓库移除（1.15.6+ 全部 404），这是新版 Cilium 唯一可靠的免 Helm 方式。
    local cli=""
    if command -v cilium >/dev/null 2>&1; then cli=$(command -v cilium); fi
    # 复用已解压的二进制 / 已下载完整的压缩包，避免每次都重复拉 78MB
    if [ -z "$cli" ] && [ -x /tmp/cilium ]; then cli=/tmp/cilium; fi
    if [ -z "$cli" ] && [ -f /tmp/cilium-cli.tgz ] && python3 -c "import tarfile; tarfile.open('/tmp/cilium-cli.tgz').getnames()" >/dev/null 2>&1; then
      if python3 -m tarfile -e /tmp/cilium-cli.tgz /tmp >/dev/null 2>&1 && [ -f /tmp/cilium ]; then
        chmod +x /tmp/cilium; cli=/tmp/cilium
      fi
    fi
    if [ -z "$cli" ]; then
      for u in \
        "https://ghfast.top/https://github.com/cilium/cilium-cli/releases/latest/download/cilium-linux-amd64.tar.gz" \
        "https://ghproxy.com/https://github.com/cilium/cilium-cli/releases/latest/download/cilium-linux-amd64.tar.gz" \
        "https://github.com/cilium/cilium-cli/releases/latest/download/cilium-linux-amd64.tar.gz"; do
        # -4 强制 IPv4（IPv6 常连接重置）；--retry 应对镜像源偶发卡死（实测 ghfast 会中途 stall）；
        # 解压用 python3 的 tarfile（openEuler 最小镜像可能没装 tar）。
        rm -f /tmp/cilium-cli.tgz
        if curl -4 -fSL --retry 5 --retry-delay 5 --connect-timeout 12 --max-time 600 -o /tmp/cilium-cli.tgz "$u"; then
          if python3 -m tarfile -e /tmp/cilium-cli.tgz /tmp >/dev/null 2>&1 && [ -f /tmp/cilium ]; then
            chmod +x /tmp/cilium; cli=/tmp/cilium; break
          fi
        fi
      done
    fi
    if [ -n "$cli" ]; then
      echo "[*] 使用 cilium CLI 渲染清单（version=$CILIUM_VER，路由=$CILIUM_ROUTING，Pod 网段=$POD_CIDR，无需 Helm）"
      local setargs=()
      setargs+=(--set "ipam.operator.clusterPoolIPv4PodCIDRList={$POD_CIDR}")
      case "$CILIUM_ROUTING" in
        native)        setargs+=(--set routingMode=native) ;;
        tunnel-geneve) setargs+=(--set routingMode=tunnel --set tunnelProtocol=geneve) ;;
        *)             setargs+=(--set routingMode=tunnel --set tunnelProtocol=vxlan) ;;
      esac
      if [ -n "$CILIUM_DEVICES" ]; then setargs+=(--set "devices={$CILIUM_DEVICES}"); fi
      if KUBECONFIG=/etc/kubernetes/admin.conf "$cli" install --version "$CILIUM_VER" "${setargs[@]}" --dry-run > /tmp/cilium.yaml 2>/tmp/cilium-cli.err; then
        ok=1
      else
        echo "[警告] cilium CLI --dry-run 失败：$(tail -1 /tmp/cilium-cli.err 2>/dev/null)"
      fi
    fi
    # 回退：旧版 quick-install.yaml（仅 1.14 及更早仓库里还有；新版已移除）
    if [ "$ok" != "1" ]; then
      echo "[警告] cilium CLI 不可用/渲染失败，尝试旧版 quick-install.yaml（新版 Cilium 可能不存在此文件）"
      for u in \
        "https://cdn.jsdelivr.net/gh/cilium/cilium@$CILIUM_VER/install/kubernetes/quick-install.yaml" \
        "https://raw.githubusercontent.com/cilium/cilium/$CILIUM_VER/install/kubernetes/quick-install.yaml" \
        "https://mirror.ghproxy.com/https://raw.githubusercontent.com/cilium/cilium/$CILIUM_VER/install/kubernetes/quick-install.yaml" \
        "https://ghfast.top/https://raw.githubusercontent.com/cilium/cilium/$CILIUM_VER/install/kubernetes/quick-install.yaml"; do
        if curl -fsSL --connect-timeout 12 --max-time 600 -o /tmp/cilium.yaml "$u"; then ok=1; break; fi
      done
    fi
    if [ "$ok" = "1" ]; then
      # cilium 镜像在 quay.io/cilium：按所选 CNI 镜像源实测选源并替换清单前缀，探测不通回退官方
      local reg
      case "$CNI_MIRROR" in
        daocloud) reg=$(pick_cni_registry "cilium/cilium:$CILIUM_VER" "quay.io" "quay.m.daocloud.io") ;;
        163|baidu) reg="quay.io" ;;   # 网易/百度只有 docker.io 类镜像站，无 quay 代理
        official) reg="quay.io" ;;
        custom)   reg=$(pick_cni_registry "cilium/cilium:$CILIUM_VER" "quay.io" "$CNI_MIRROR_CUSTOM") ;;
        *)        reg=$(pick_cni_registry "cilium/cilium:$CILIUM_VER" "quay.io" "quay.m.daocloud.io" "quay.nju.edu.cn") ;;
      esac
      if [ "$reg" != "quay.io" ]; then
        echo "[*] Cilium 镜像源: $reg"
        sed -i "s#quay\.io/cilium#$reg/cilium#g" /tmp/cilium.yaml
      else
        echo "[警告] 所有 quay.io 镜像源均不可达，Cilium 镜像将回退 quay.io 直连；"
        echo "[警告] 若虚机网络无法访问 quay.io（常见：实验室网络仅放行阿里云），Cilium Pod 会 ImagePullBackOff。"
        echo "[警告] 解决：在「CNI 镜像源」选 custom 并填写可达镜像源（如内网代理/私有仓库），或保证网络能访问 quay.io 后重跑。"
      fi
      kubectl apply -f /tmp/cilium.yaml || echo "[警告] Cilium 清单 apply 失败，请检查集群状态"
      echo "[*] 等待 Cilium DaemonSet 就绪..."
      for i in $(seq 1 36); do kubectl -n kube-system get ds cilium >/dev/null 2>&1 && break; sleep 5; done
    else
      echo "[警告] Cilium 清单下载失败，CNI 未部署（集群将无网络，请检查 GitHub 镜像站连通性）"
    fi
  }

  # ---- 部署 CNI（按所选 CNI；仅首控制面节点执行）----
  if [ "$CNI" = "flannel" ]; then
    echo "[*] 部署 flannel CNI"
  # 注意：这里必须用「不带引号」的 heredoc，让 $FLANNEL_VER / $FLANNEL_CNI_VER 在虚机内展开；
  # 带引号会原样写入 `$FLANNEL_CNI_VER`，导致镜像名非法（InvalidImageName）。
  cat > /tmp/kube-flannel.yml <<FLANNEL_EOF
---
kind: Namespace
apiVersion: v1
metadata:
  name: kube-flannel
  labels:
    k8s-app: flannel
    pod-security.kubernetes.io/enforce: privileged
---
kind: ClusterRole
apiVersion: rbac.authorization.k8s.io/v1
metadata:
  labels:
    k8s-app: flannel
  name: flannel
rules:
- apiGroups:
  - ""
  resources:
  - pods
  verbs:
  - get
- apiGroups:
  - ""
  resources:
  - nodes
  verbs:
  - get
  - list
  - watch
- apiGroups:
  - ""
  resources:
  - nodes/status
  verbs:
  - patch
---
kind: ClusterRoleBinding
apiVersion: rbac.authorization.k8s.io/v1
metadata:
  labels:
    k8s-app: flannel
  name: flannel
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: flannel
subjects:
- kind: ServiceAccount
  name: flannel
  namespace: kube-flannel
---
apiVersion: v1
kind: ServiceAccount
metadata:
  labels:
    k8s-app: flannel
  name: flannel
  namespace: kube-flannel
---
kind: ConfigMap
apiVersion: v1
metadata:
  name: kube-flannel-cfg
  namespace: kube-flannel
  labels:
    tier: node
    k8s-app: flannel
    app: flannel
data:
  cni-conf.json: |
    {
      "name": "cbr0",
      "cniVersion": "1.0.0",
      "plugins": [
        {
          "type": "flannel",
          "delegate": {
            "hairpinMode": true,
            "isDefaultGateway": true
          }
        },
        {
          "type": "portmap",
          "capabilities": {
            "portMappings": true
          }
        }
      ]
    }
  net-conf.json: |
    {
      "Network": "10.244.0.0/16",
      "EnableNFTables": false,
      "Backend": {
        "Type": "vxlan"
      }
    }
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: kube-flannel-ds
  namespace: kube-flannel
  labels:
    tier: node
    app: flannel
    k8s-app: flannel
spec:
  selector:
    matchLabels:
      app: flannel
  template:
    metadata:
      labels:
        tier: node
        app: flannel
    spec:
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: kubernetes.io/os
                operator: In
                values:
                - linux
      hostNetwork: true
      priorityClassName: system-node-critical
      tolerations:
      - operator: Exists
        effect: NoSchedule
      serviceAccountName: flannel
      initContainers:
      - name: install-cni-plugin
        image: docker.io/flannel/flannel-cni-plugin:$FLANNEL_CNI_VER
        command:
        - cp
        args:
        - -f
        - /flannel
        - /opt/cni/bin/flannel
        volumeMounts:
        - name: cni-plugin
          mountPath: /opt/cni/bin
      - name: install-cni
        image: docker.io/flannel/flannel:$FLANNEL_VER
        command:
        - cp
        args:
        - -f
        - /etc/kube-flannel/cni-conf.json
        - /etc/cni/net.d/10-flannel.conflist
        volumeMounts:
        - name: cni
          mountPath: /etc/cni/net.d
        - name: flannel-cfg
          mountPath: /etc/kube-flannel/
      containers:
      - name: kube-flannel
        image: docker.io/flannel/flannel:$FLANNEL_VER
        command:
        - /opt/bin/flanneld
        args:
        - --ip-masq
        - --kube-subnet-mgr
        resources:
          requests:
            cpu: "100m"
            memory: "64Mi"
          limits:
            memory: "128Mi"
        securityContext:
          privileged: false
          capabilities:
            add: ["NET_ADMIN", "NET_RAW"]
        env:
        - name: POD_NAME
          valueFrom:
            fieldRef:
              fieldPath: metadata.name
        - name: POD_NAMESPACE
          valueFrom:
            fieldRef:
              fieldPath: metadata.namespace
        - name: EVENT_QUEUE_DEPTH
          value: "5000"
        volumeMounts:
        - name: run
          mountPath: /run/flannel
        - name: flannel-cfg
          mountPath: /etc/kube-flannel/
        - name: xtables-lock
          mountPath: /run/xtables.lock
      volumes:
      - name: run
        hostPath:
          path: /run/flannel
      - name: cni-plugin
        hostPath:
          path: /opt/cni/bin
      - name: cni
        hostPath:
          path: /etc/cni/net.d
      - name: flannel-cfg
        configMap:
          name: kube-flannel-cfg
      - name: xtables-lock
        hostPath:
          path: /run/xtables.lock
          type: FileOrCreate
FLANNEL_EOF
    # 按参数改内联清单：Pod 网段 / 后端类型 / 指定网卡
    sed -i "s|\"Network\": \"10.244.0.0/16\"|\"Network\": \"$POD_CIDR\"|" /tmp/kube-flannel.yml
    sed -i "s|\"Type\": \"vxlan\"|\"Type\": \"$FLANNEL_BACKEND\"|" /tmp/kube-flannel.yml
    if [ -n "$FLANNEL_IFACE" ]; then
      sed -i "s|- --kube-subnet-mgr|- --kube-subnet-mgr\n        - --iface=$FLANNEL_IFACE|" /tmp/kube-flannel.yml
    fi
    if [ "$FLANNEL_BACKEND" = "wireguard" ]; then
      modprobe wireguard 2>/dev/null || true
      grep -q '^wireguard' /etc/modules-load.d/k8s.conf 2>/dev/null || echo wireguard >> /etc/modules-load.d/k8s.conf
    fi
    echo "[*] Flannel 配置: Pod 网段=$POD_CIDR  后端=$FLANNEL_BACKEND  网卡=${FLANNEL_IFACE:-自动}"
    # flannel 镜像同走 CNI 镜像源（docker.io/flannel → 所选源，探测不通回退官方）
    case "$CNI_MIRROR" in
      daocloud) reg=$(pick_cni_registry "flannel/flannel:$FLANNEL_VER" "docker.io" "docker.m.daocloud.io") ;;
      163|baidu) reg="docker.io" ;;   # 网易/百度仅代理 library/* 且多已不可用，退化为官方回退
      official) reg="docker.io" ;;
      custom)   reg=$(pick_cni_registry "flannel/flannel:$FLANNEL_VER" "docker.io" "$CNI_MIRROR_CUSTOM") ;;
      *)        reg=$(pick_cni_registry "flannel/flannel:$FLANNEL_VER" "docker.io" "docker.m.daocloud.io" "docker.1ms.run") ;;
    esac
    if [ "$reg" != "docker.io" ]; then
      echo "[*] Flannel 镜像源: $reg"
      sed -i "s#docker\.io/flannel#$reg/flannel#g" /tmp/kube-flannel.yml
    fi
    kubectl apply -f /tmp/kube-flannel.yml
    echo "[*] 等待 flannel DaemonSet 就绪..."
    for i in $(seq 1 30); do
      if kubectl -n kube-flannel get ds kube-flannel-ds >/dev/null 2>&1; then break; fi
      sleep 5
    done
  elif [ "$CNI" = "calico" ]; then
    deploy_cni_calico
  elif [ "$CNI" = "cilium" ]; then
    deploy_cni_cilium
  else
    echo "[警告] 未知 CNI: $CNI，未部署任何网络插件"
  fi

  # ---- 保留部署清单到家目录（时间戳目录，便于排障/复用；/tmp 重启后会被清理）----
  if [ "$BACKUP_MANIFEST" = "1" ]; then
    BK_DIR="$KUBE_HOME/k8s-deploy-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$BK_DIR" 2>/dev/null || true
    [ -f /tmp/kubeadm-init.yaml ] && cp -f /tmp/kubeadm-init.yaml "$BK_DIR/" 2>/dev/null || true
    case "$CNI" in
      calico)  [ -f /tmp/calico.yaml ]      && cp -f /tmp/calico.yaml      "$BK_DIR/" 2>/dev/null || true ;;
      cilium)  [ -f /tmp/cilium.yaml ]      && cp -f /tmp/cilium.yaml      "$BK_DIR/" 2>/dev/null || true ;;
      flannel) [ -f /tmp/kube-flannel.yml ] && cp -f /tmp/kube-flannel.yml "$BK_DIR/" 2>/dev/null || true ;;
    esac
    # CNI 清单缺失时明确告警（例如 Cilium CLI 下载/渲染失败，/tmp/cilium.yaml 未生成），避免静默漏备份
    case "$CNI" in
      calico)  [ -f /tmp/calico.yaml ]      || echo "[警告] 未找到 Calico 清单 /tmp/calico.yaml，本次备份不含 CNI yaml" ;;
      cilium)  [ -f /tmp/cilium.yaml ]      || echo "[警告] 未找到 Cilium 清单 /tmp/cilium.yaml（CLI 下载/渲染失败时会出现），本次备份不含 CNI yaml" ;;
      flannel) [ -f /tmp/kube-flannel.yml ] || echo "[警告] 未找到 Flannel 清单 /tmp/kube-flannel.yml，本次备份不含 CNI yaml" ;;
    esac
    ( kubeadm token create --print-join-command 2>/dev/null || true ) > "$BK_DIR/join-command.txt" 2>/dev/null || true
    if [ -n "$(ls -A "$BK_DIR" 2>/dev/null)" ]; then
      echo "[*] 部署清单已备份到 $BK_DIR"
      ls -1 "$BK_DIR" 2>/dev/null | sed 's/^/      /'
    else
      echo "[警告] 未找到可备份的清单文件（/tmp 可能已被清理）"
    fi
  fi
else
  if [ -f /etc/kubernetes/kubelet.conf ]; then
    echo "[*] 已加入集群（幂等），跳过 kubeadm join"
  else
    echo "[*] kubeadm join -> $SERVER_URL"
    HOSTPORT="${SERVER_URL#*//}"; HOSTPORT="${HOSTPORT%%/*}"
    HEX=$(echo | openssl s_client -connect "$HOSTPORT" 2>/dev/null | openssl x509 -noout -fingerprint -sha256 | sed 's/.*=//;s/://g')
    if [ -n "$HEX" ]; then
      HASH=$(python3 -c "import binascii,base64;print(base64.b64encode(binascii.unhexlify('$HEX')).decode())" 2>/dev/null)
    else
      HASH=""
    fi
    JOIN="kubeadm join $SERVER_URL --token $JOIN_TOKEN"
    if [ -n "$HASH" ]; then JOIN="$JOIN --discovery-token-ca-cert-hash sha256:$HASH"; else JOIN="$JOIN --discovery-token-unsafe-skip-ca-verification"; fi
    if [ "$ROLE" = "master" ] && [ -n "$CERT_KEY" ]; then
      JOIN="$JOIN --control-plane --certificate-key $CERT_KEY"
    fi
    $JOIN 2>&1 | tail -30
  fi
fi

# ---- 常用命令环境（kubectl/kubeadm 补全 + 别名 k；crictl 安装与配置 + 别名 c）----
# 放在最后：二进制都已就位，补全/配置才生成得出来。
if [ "$SHELL_COMPLETION" = "1" ]; then
  echo "[*] configuring common k8s commands (kubectl/kubeadm completion, crictl)"
  # bash-completion 提供 __ltrim_colon_completions 等函数，缺了补全脚本会静默不生效
  if [ ! -f /usr/share/bash-completion/bash_completion ]; then
    case "$PM" in
      dnf|yum) $PM install -y bash-completion >/dev/null 2>&1 || true ;;
      apt)     apt-get update -y >/dev/null 2>&1; apt-get install -y bash-completion >/dev/null 2>&1 || true ;;
    esac
  fi

  # crictl：kubelet / 容器运行时排障必备。用 cri-tools 包安装（走已配置的 k8s 包源），
  # 并写入 /etc/crictl.yaml 指向 containerd socket，使裸 crictl 即可用、免 -r 指定。
  if ! command -v crictl >/dev/null 2>&1; then
    case "$PM" in
      dnf|yum) $PM install -y cri-tools >/dev/null 2>&1 || true ;;
      apt)     apt-get update -y >/dev/null 2>&1; apt-get install -y cri-tools >/dev/null 2>&1 || true ;;
    esac
  fi
  if command -v crictl >/dev/null 2>&1; then
    cat > /etc/crictl.yaml <<'CRI_EOF'
runtime-endpoint: unix:///run/containerd/containerd.sock
image-endpoint: unix:///run/containerd/containerd.sock
CRI_EOF
  fi

  cat > /etc/profile.d/k8s-completion.sh <<'COMP_EOF'
# kubectl / kubeadm / crictl 的 bash 补全与别名（由 PVE Web 工具写入，重新登录后生效）
if [ -n "$BASH_VERSION" ]; then
  [ -f /usr/share/bash-completion/bash_completion ] && . /usr/share/bash-completion/bash_completion
  if command -v kubectl >/dev/null 2>&1; then
    . <(kubectl completion bash) 2>/dev/null
    alias k=kubectl
    complete -o default -F __start_kubectl k 2>/dev/null
  fi
  command -v kubeadm >/dev/null 2>&1 && . <(kubeadm completion bash) 2>/dev/null
  if command -v crictl >/dev/null 2>&1; then
    . <(crictl completion bash) 2>/dev/null
    alias c=crictl
  fi
fi
COMP_EOF
  chmod 644 /etc/profile.d/k8s-completion.sh
  # 已开的终端 / su root 走的是 .bashrc，profile.d 不一定被读：在 root 的 .bashrc 里补一行（幂等）
  if [ -f /root/.bashrc ] && ! grep -q 'k8s-completion.sh' /root/.bashrc; then
    echo '[ -f /etc/profile.d/k8s-completion.sh ] && . /etc/profile.d/k8s-completion.sh' >> /root/.bashrc
  fi
  echo "[*] 常用命令已配置：重新登录后 kubectl/kubeadm/crictl 支持 Tab 补全；别名 k=kubectl，c=crictl"
fi

if [ -n "${BK_DIR:-}" ] && [ -d "$BK_DIR" ]; then
  echo "[*] 部署清单备份目录：$BK_DIR"
fi
echo "[done] kubeadm install finished"
GUEST_EOF
)
GUEST_SCRIPT=${GUEST_SCRIPT//__ROLE__/$ROLE}
GUEST_SCRIPT=${GUEST_SCRIPT//__SERVER_URL__/$SERVER_URL}
GUEST_SCRIPT=${GUEST_SCRIPT//__TOKEN__/$JOIN_TOKEN}
GUEST_SCRIPT=${GUEST_SCRIPT//__CERT_KEY__/$CERT_KEY}
GUEST_SCRIPT=${GUEST_SCRIPT//__K8S_VERSION__/$K8S_VERSION}
GUEST_SCRIPT=${GUEST_SCRIPT//__K8S_MIRROR__/$K8S_MIRROR}
GUEST_SCRIPT=${GUEST_SCRIPT//__K8S_MIRROR_CUSTOM__/$K8S_MIRROR_CUSTOM}
GUEST_SCRIPT=${GUEST_SCRIPT//__OPEN_FW__/$OPEN_FW}
GUEST_SCRIPT=${GUEST_SCRIPT//__CGROUP_V2__/$CGROUP_V2}
GUEST_SCRIPT=${GUEST_SCRIPT//__SHELL_COMPLETION__/$COMPLETION}
GUEST_SCRIPT=${GUEST_SCRIPT//__BACKUP_MANIFEST__/$BACKUP_MANIFEST}
GUEST_SCRIPT=${GUEST_SCRIPT//__CNI_VERSION_OVERRIDE__/$CNI_VERSION_OVERRIDE}
GUEST_SCRIPT=${GUEST_SCRIPT//__CALICO_POD_CIDR__/$CALICO_POD_CIDR}
GUEST_SCRIPT=${GUEST_SCRIPT//__CALICO_ENCAP__/$CALICO_ENCAP}
GUEST_SCRIPT=${GUEST_SCRIPT//__CALICO_AUTODETECT__/$CALICO_AUTODETECT}
GUEST_SCRIPT=${GUEST_SCRIPT//__FLANNEL_POD_CIDR__/$FLANNEL_POD_CIDR}
GUEST_SCRIPT=${GUEST_SCRIPT//__FLANNEL_BACKEND__/$FLANNEL_BACKEND}
GUEST_SCRIPT=${GUEST_SCRIPT//__FLANNEL_IFACE__/$FLANNEL_IFACE}
GUEST_SCRIPT=${GUEST_SCRIPT//__CILIUM_POD_CIDR__/$CILIUM_POD_CIDR}
GUEST_SCRIPT=${GUEST_SCRIPT//__CILIUM_ROUTING__/$CILIUM_ROUTING}
GUEST_SCRIPT=${GUEST_SCRIPT//__CILIUM_DEVICES__/$CILIUM_DEVICES}
GUEST_SCRIPT=${GUEST_SCRIPT//__RESET__/$RESET}
GUEST_SCRIPT=${GUEST_SCRIPT//__CNI__/$CNI}
GUEST_SCRIPT=${GUEST_SCRIPT//__CNI_MIRROR__/$CNI_MIRROR}
GUEST_SCRIPT=${GUEST_SCRIPT//__CNI_MIRROR_CUSTOM__/$CNI_MIRROR_CUSTOM}
GUEST_SCRIPT=${GUEST_SCRIPT//__K8S_REPO__/$K8S_REPO}
GUEST_SCRIPT=${GUEST_SCRIPT//__INIT_ENDPOINT__/$INIT_ENDPOINT}

B64=$(printf '%s' "$GUEST_SCRIPT" | base64 -w0)
echo "[4/6] 进入虚机执行安装（以下为虚机内实时日志）..."

# 启动前先探测 guest-exec 通道是否可用：QEMU guest-agent 串行执行 guest-exec，
# 若上一次会话(在虚机内)未退出，新的会被排队且长时间零输出。带超时探测，卡住则快速失败并提示处理。
echo "[*] 探测 guest-exec 通道是否可用（最多 35s）..."
if ! timeout 40 qm guest exec "$VMID" -- true >/dev/null 2>&1; then
  echo "[错误] guest-exec 通道无响应：很可能上一次部署的会话仍在虚机内未退出，被串行排队阻塞。"
  echo "[处理] 请在 PVE 控制台 / SSH 进入虚机，执行 'ps aux | grep -E \"base64|kubeadm|bash -c\"' 清理残留安装进程；"
  echo "        或直接重启该虚机（单节点集群会自动恢复），然后再运行本工具。"
  exit 1
fi
echo "[*] guest-exec 通道可用"

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
deadline = int(os.environ.get("PT_GA_DEADLINE", "2400") or 2400)


def emit(s):
    if s:
        sys.stdout.write(s)
        sys.stdout.flush()


def qm(args, timeout=60):
    try:
        return subprocess.run(["qm"] + args, capture_output=True, text=True,
                              errors="replace", timeout=timeout)
    except Exception:
        # guest-exec-status 在虚机高负载(如 kubeadm init)时响应可能超过 timeout 而被中断；
        # 返回非0哨兵让上层按"查询失败"重试，避免回传进程崩溃、日志中断。
        class _R:
            returncode = 1
            stdout = ""
            stderr = "qm subprocess timed out / errored"
        return _R()


_B64_ONLY = re.compile(r"\A[A-Za-z0-9+/]+={0,2}\Z")


def as_text(raw):
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
    try:
        raw_bytes = s.encode("latin-1")
    except (UnicodeEncodeError, ValueError):
        return s
    try:
        fixed = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return s
    return fixed if fixed != s and any(ord(c) > 127 for c in fixed) else s


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
    emit("[错误] 虚机内安装进程未返回 pid\n")
    sys.exit(1)

emit("[*] 安装已在虚机内启动（pid=%s），开始实时回传日志 ...\n" % pid)

start = time.time()
last_beat = start
prev = ""
fails = 0
exitcode = None

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
            emit("\n[错误] 连续多次无法查询虚机内进程状态，停止回传日志。\n")
            emit("[提示] 安装可能仍在虚机内继续，可进虚机执行 ps aux | grep -E 'kubeadm|containerd|curl' 确认。\n")
            sys.exit(1)
    else:
        fails = 0
        cur = as_text(st.get("out-data") or "")
        err = as_text(st.get("err-data") or "")
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
    if exitcode == 75:
        # cgroup v2 修正已写入并触发重启，属预期中断，非失败
        sys.exit(75)
    emit("\n[guest] install exited with code %s\n" % exitcode)
    sys.exit(1)
sys.exit(0)
PYEOF
_ga_rc=$?
if [ "$_ga_rc" -eq 75 ]; then
  # 重启信号由 deploy_vm_autoreboot 统一提示并续跑，这里只向上传递
  return 75
fi
[ "$_ga_rc" -eq 0 ] || exit 1
echo "[*] 虚机 $VMID 内安装完成。"
}

# 等待虚机重启完成：先等 guest agent 断开（确认重启真的发生），再等它重新就绪
wait_guest_reboot() {
  local i=0
  while [ "$i" -lt 30 ]; do
    if ! timeout 5 qm guest exec "$VMID" -- true >/dev/null 2>&1; then break; fi
    sleep 3; i=$((i+1))
  done
  [ "$(ga_probe_secs 300)" = "ready" ]
}

# 部署单台虚机；遇 cgroup v2 修正（exit 75）自动等重启并续跑一次
deploy_vm_autoreboot() {
  deploy_vm
  local rc=$?
  if [ "$rc" -eq 75 ]; then
    echo "[*] cgroup v2 参数已写入并触发重启，等待虚机重启后自动继续安装..."
    if wait_guest_reboot; then
      echo "[*] 虚机已重启完成，自动继续安装（无需手动重跑）"
      deploy_vm
      rc=$?
      if [ "$rc" -eq 75 ]; then
        echo "[警告] 重启后仍要求重启（参数可能未生效），已停止；请检查 grub/内核参数后重跑。"
        return 1
      fi
    else
      echo "[警告] 等待虚机重启超时，请稍后重新运行本工具。"
      return 1
    fi
  fi
  return "$rc"
}

# ===== 集群编排：从控制面首节点取 join 信息，依次加入其余节点 =====
# 在控制面首节点上读取「worker join 命令」与「控制面证书密钥」，供后续节点复用
fetch_cp0_info() {
  local vmid="$1"
  local jc ck ip su
  jc=$(qm guest exec "$vmid" -- kubeadm token create --print-join-command 2>/dev/null | python3 -c 'import sys,json
try:
    d=json.loads(sys.stdin.read()) or {}
except Exception:
    d={}
print((d.get("out-data") or "").strip())')
  ck=$(qm guest exec "$vmid" -- kubeadm init phase upload-certs --upload-certs 2>/dev/null | python3 -c 'import sys,json,re
try:
    d=json.loads(sys.stdin.read()) or {}
except Exception:
    d={}
t=(d.get("out-data") or "")
m=re.search(r"certificate key:\s*(\S+)", t)
print(m.group(1) if m else "")')
  ip=$(echo "$jc" | grep -oE 'kubeadm join [0-9a-zA-Z._-]+:[0-9]+' | awk '{print $3}')
  su=""
  [ -n "$ip" ] && su="https://$ip"
  printf '%s|%s|%s' "$su" "$jc" "$ck"
}

# 取虚机主网卡 IP（高可用集群 init 时需要 controlPlaneEndpoint）
vm_ip() {
  local vmid="$1"
  qm guest exec "$vmid" -- bash -c 'hostname -I 2>/dev/null | awk "{print \$1}"' 2>/dev/null \
    | python3 -c 'import sys,json
try:
    d=json.loads(sys.stdin.read()) or {}
except Exception:
    d={}
print((d.get("out-data") or "").strip())'
}

# ===== 按架构形态分派（拓扑由「架构形态 + 勾选虚机数」共同决定）=====
case "$ARCH" in
  allinone)
    # 单机一体化：1 台虚机既控制面又跑业务（kubeadm init + 自动去污点）
    [ "$N" -eq 1 ] || { echo "[错误] 单机 All-in-One 仅支持勾选 1 台虚机（多选请改用「高可用集群」或「节点扩容」）"; exit 1; }
    VMID="$1"; ROLE="master"; SERVER_URL=""; JOIN_TOKEN=""; CERT_KEY=""; INIT_ENDPOINT=""
    echo "===== 单机 All-in-One：虚机 $VMID（控制面+业务一体）====="
    deploy_vm_autoreboot
    rc=$?
    [ "$rc" -eq 0 ] || exit "$rc"
    echo "[*] 完成。在虚机 $VMID 内执行 kubectl get nodes 查看状态。"
    exit 0
    ;;
  expand)
    # 节点扩容：向已有集群追加节点，需填集群入口地址 + Token（填了证书密钥则该节点作为控制面加入 HA）
    [ -n "$SERVER_URL" ] || { echo "[错误] 节点扩容需填写集群入口地址（server_url）"; exit 1; }
    [ -n "$JOIN_TOKEN" ] || { echo "[错误] 节点扩容需填写加入 Token（token）"; exit 1; }
    echo "===== 节点扩容：向 $SERVER_URL 加入 $N 台虚机 ====="
    for vmid in "$@"; do
      VMID="$vmid"
      # 填了证书密钥则该节点作为控制面加入 HA；否则作为工作节点加入
      if [ -n "$CERT_KEY" ]; then ROLE="master"; else ROLE="node"; fi
      echo "===== 扩容节点：$vmid（$([ -n "$CERT_KEY" ] && echo 控制面 || echo 工作) 节点）====="
      deploy_vm_autoreboot || { echo "[错误] 节点 $vmid 加入失败，已加入的节点仍可用。"; exit 1; }
    done
    echo "===== 节点扩容完成：$N 台已加入集群 ====="
    exit 0
    ;;
  ha)
    # 高可用集群：勾选虚机全部作为控制面（etcd 堆叠），去污点后每台既是 master 也能跑业务
    if [ "$N" -ne 3 ]; then
      echo "[错误] 高可用集群需恰好勾选 3 台虚机（当前已选 $N 台）；请调整勾选数量后重试。"
      exit 1
    fi
    CP0="$1"
    CP0_IP=$(vm_ip "$CP0")
    [ -n "$CP0_IP" ] || { echo "[错误] 无法获取控制面首节点 $CP0 的 IP（请确认虚机已开机且 QEMU Guest Agent 正常）"; exit 1; }
    INIT_ENDPOINT="${CP0_IP}:6443"
    VMID="$CP0"; ROLE="master"; SERVER_URL=""; JOIN_TOKEN=""; CERT_KEY=""
    echo "===== 高可用集群：共 $N 台，全部为控制面（etcd 堆叠），去污点后兼跑业务 ====="
    echo "===== 节点 1/$N：$CP0（控制面首节点，kubeadm init，端点 $INIT_ENDPOINT）====="
    deploy_vm_autoreboot || { echo "[错误] 控制面首节点 $CP0 部署失败，集群中止。"; exit 1; }

    INFO=$(fetch_cp0_info "$CP0")
    SRV_URL=$(echo "$INFO" | cut -d'|' -f1)
    WORKER_JC=$(echo "$INFO" | cut -d'|' -f2)
    CK=$(echo "$INFO" | cut -d'|' -f3)
    if [ -z "$SRV_URL" ]; then
      echo "[错误] 未能从控制面首节点 $CP0 取得集群入口地址（kubeadm init 可能未完成，或被 cgroup v2 修正重启打断）。"
      echo "[处理] 请单独对 $CP0 重新运行本工具（架构=高可用集群）完成 init，再运行其余节点加入。"
      exit 1
    fi
    TOKEN=$(echo "$WORKER_JC" | grep -oE '\-\-token [^ ]+' | awk '{print $2}')
    echo "[*] 集群入口：$SRV_URL　控制面证书密钥：$([ -n "$CK" ] && echo 已获取 || echo 无)"

    i=1
    for vmid in "$@"; do
      [ "$i" -eq 1 ] && { i=$((i+1)); continue; }   # 跳过控制面首节点
      VMID="$vmid"; ROLE="master"; SERVER_URL="$SRV_URL"; JOIN_TOKEN="$TOKEN"; CERT_KEY="$CK"
      echo "===== 节点 $i/$N：$vmid（控制面，加入 HA）====="
      deploy_vm_autoreboot || { echo "[错误] 节点 $vmid 部署失败，集群中止（已部署节点仍可用，可单独补装剩余节点）。"; exit 1; }
      i=$((i+1))
    done
    echo "===== 高可用集群部署完成：$N 台控制面（既是 master 也是 node）====="
    echo "[*] 在控制面首节点 $CP0 执行：kubectl get nodes 查看集群状态。"
    exit 0
    ;;
  *)
    echo "[错误] 未知的架构形态：$ARCH（应为 allinone/ha/expand）"; exit 1
    ;;
esac
