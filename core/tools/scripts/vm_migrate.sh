
VMID="$PT_VMID"
TARGET="$PT_TARGET"
MODE="${PT_MODE:-online}"
MIG_LOCAL_DISKS="${PT_LOCAL_DISKS:-yes}"

[ -n "$VMID" ]   || { echo "[错误] 请选择要迁移的虚机/容器"; exit 1; }
[ -n "$TARGET" ] || { echo "[错误] 请选择目标节点"; exit 1; }

# 集群失去仲裁时 /etc/pve 只读，迁移的写操作必然失败；先探一次给出可操作指引
pw_quorum_precheck

LOCAL_NODE=$(hostname -s)
if [ "$TARGET" = "$LOCAL_NODE" ]; then
  echo "[错误] 目标节点与当前执行节点相同（$LOCAL_NODE）：请选择另一个节点"
  exit 1
fi

# 目标节点必须是本集群成员且在线（离线节点取状态会失败）
if ! pvesh get "/nodes/$TARGET/status" --output-format json >/dev/null 2>&1; then
  echo "[错误] 目标节点 $TARGET 不可达或不是本集群节点"
  echo "[提示] 目标节点需在线且已加入集群（到「节点管理」页确认）；集群只有本机一台时无法迁移"
  exit 1
fi

# qm/pct 都是节点本地命令：执行节点必须是源虚机/容器当前所在的节点
GTYPE=""
qm status "$VMID" >/dev/null 2>&1 && GTYPE=qm
[ -z "$GTYPE" ] && pct status "$VMID" >/dev/null 2>&1 && GTYPE=pct
if [ -z "$GTYPE" ]; then
  echo "[错误] 当前执行节点（$LOCAL_NODE）上没有虚机/容器 $VMID"
  echo "[提示] 迁移在源节点上执行：请把弹窗上方「执行节点」切到 $VMID 当前所在的节点后重试"
  exit 1
fi

# qm 与 pct 的差异只收敛在这两个入口里。末尾带 || true：这两个入口只用于取值、不参与条件判断，
# 取不到就返回空串，免得 COMMON_HELPERS 的 ERR trap 把它渲染成一条误导性的「命令执行失败」
pw_mig_config() { if [ "$GTYPE" = qm ]; then qm config "$1" 2>/dev/null || true; else pct config "$1" 2>/dev/null || true; fi; }
pw_mig_status() { if [ "$GTYPE" = qm ]; then qm status "$1" 2>/dev/null || true; else pct status "$1" 2>/dev/null || true; fi; }
# 存储是否共享（shared=1）：共享存储上的磁盘不参与复制，迁移通常秒级完成
pw_mig_is_shared() {
  pvesh get "/storage/$1" --output-format json 2>/dev/null | grep -qE '"shared"[[:space:]]*:[[:space:]]*1'
}
# 取 dir 类型存储的根目录（非 dir 类型回显空）；用于估算本地盘待复制量时解析磁盘文件位置
pw_storage_path() {
  awk -v sid="$1" '
    /^[a-z]+:[[:space:]]/ { inblk = ($1 == ("dir:" sid)) }
    inblk && $1 == "path" { sub(/^path[[:space:]]+/, ""); print; exit }
  ' /etc/pve/storage.cfg 2>/dev/null || true
}
# 从磁盘配置行解析分配大小（size=100G）→ 字节；取不到回显空
pw_size_to_bytes() {
  local raw n u
  raw=$(printf '%s' "$1" | grep -oE 'size=[0-9.]+[KMGTB]?' | head -1)
  [ -n "$raw" ] || return 0
  n=$(printf '%s' "$raw" | grep -oE '[0-9.]+' | head -1)
  u=$(printf '%s' "$raw" | grep -oiE '[KMGTB]$')
  n=${n%.*}
  case "$u" in
    K) printf '%s' "$(( n * 1024 ))" ;;
    M) printf '%s' "$(( n * 1048576 ))" ;;
    G) printf '%s' "$(( n * 1073741824 ))" ;;
    T) printf '%s' "$(( n * 1099511627776 ))" ;;
    *) printf '%s' "$n" ;;
  esac
}
# 估算单个本地卷待复制字节数：尽量取磁盘实际已用（qcow2/raw 的 actual-size），
# 取不到（块存储/文件不可见）回显空，由调用方退回 size=
pw_mig_vol_bytes() {
  local vol="$1" sid sub path file b
  case "$vol" in
    *:*) sid="${vol%%:*}"; sub="${vol#*:}" ;;
    *) return 0 ;;
  esac
  path=$(pw_storage_path "$sid")
  [ -n "$path" ] || return 0
  file="$path/images/$sub"
  [ -f "$file" ] || return 0
  b=$(qemu-img info --output=json "$file" 2>/dev/null | grep -oE '"actual-size":[[:space:]]*[0-9]+' | grep -oE '[0-9]+' | head -1)
  [ -n "$b" ] && printf '%s' "$b"
  return 0
}
# 迁移前的链路体检（仅本地盘必须随迁时需要）：此时唯一的上限就是节点间链路 ——
# 先量一次「能跑多快 / 这么多数据要多久 / 瓶颈在哪」，免得大磁盘跑到一半才发现要几小时。
# 速率优先取「沿迁移同通道(SSH)实测吞吐」：链路协商速率读不到（虚拟口/无线/OVS）时照样能估算。
# 估算耗时较长时给出提示（不拦截，是否继续由用户判断）。
#
# 取单个接口的协商速率（桥/bond/vlan 自动下钻到物理口，sysfs 读不到再试 ethtool）；
# 回显 "<端口> <Mb/s>"，取不到回显空。
pw_dev_speed() {
  local dev="$1" depth="${2:-4}" s p slave
  [ -n "$dev" ] && [ "$depth" -gt 0 ] || return 0
  [ -d "/sys/class/net/$dev" ] || return 0
  # 1) 直接读 sysfs
  s=$(cat "/sys/class/net/$dev/speed" 2>/dev/null || true)
  case "$s" in ''|*[!0-9]*) s="" ;; 0) s="" ;; esac
  [ -n "$s" ] && { printf '%s %s' "$dev" "$s"; return 0; }
  # 2) 物理口再试 ethtool（部分网卡 sysfs 不报速）
  if [ -e "/sys/class/net/$dev/device" ] && command -v ethtool >/dev/null 2>&1; then
    s=$(ethtool "$dev" 2>/dev/null | grep -oE 'Speed:[[:space:]]*[0-9]+' | grep -oE '[0-9]+' | head -1 || true)
    case "$s" in ''|*[!0-9]*) s="" ;; 0) s="" ;; esac
    [ -n "$s" ] && { printf '%s %s' "$dev" "$s"; return 0; }
  fi
  # 3) 桥 → 桥端口
  for p in /sys/class/net/"$dev"/brif/*; do
    [ -e "$p" ] || continue
    pw_dev_speed "$(basename "$p")" $((depth-1)) && return 0
  done
  # 4) bond → 成员口
  if [ -r "/sys/class/net/$dev/bonding/slaves" ]; then
    for slave in $(cat "/sys/class/net/$dev/bonding/slaves" 2>/dev/null); do
      pw_dev_speed "$slave" $((depth-1)) && return 0
    done
  fi
  # 5) vlan → 下层口
  for p in /sys/class/net/"$dev"/lower_*; do
    [ -e "$p" ] || continue
    pw_dev_speed "$(basename "$p" | sed 's/^lower_//')" $((depth-1)) && return 0
  done
  return 0
}
# 到目标节点的出口网卡与协商速率（回显 "<网卡> <Mb/s>"；取不到回显空）
pw_mig_link_probe() {
  local addr="$1" dev info
  info=$(pw_link_speed "$addr")
  if [ -n "$info" ]; then printf '%s' "$info"; return 0; fi
  dev=$(ip route get "$addr" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}' || true)
  [ -n "$dev" ] || return 0
  pw_dev_speed "$dev" 4
  return 0
}
# 沿迁移同通道(SSH)往目标传一小段零数据实测可用吞吐（MB/s）；失败/取不到回显空。
# 这是最可靠的信号：链路速率读不到时用它照样能估算耗时并拦截。
pw_mig_net_measure() {
  local target="$1" mb="${2:-48}" t0 t1 el rc
  [ -n "$target" ] || return 0
  command -v ssh >/dev/null 2>&1 || return 0
  t0=$(date +%s%N 2>/dev/null || true)
  case "$t0" in ''|*[!0-9]*) return 0 ;; esac
  dd if=/dev/zero bs=1048576 count="$mb" 2>/dev/null | \
    ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
        "root@$target" "dd of=/dev/null bs=1048576 2>/dev/null" >/dev/null 2>&1
  rc=${PIPESTATUS[1]}
  t1=$(date +%s%N 2>/dev/null || true)
  case "$t1" in ''|*[!0-9]*) return 0 ;; esac
  [ "$rc" -eq 0 ] || return 0
  el=$(( (t1 - t0) / 1000000 ))
  [ "$el" -ge 50 ] || el=50          # 太快说明带宽远高于本次采样，给个下限即可
  awk -v mb="$mb" -v ms="$el" 'BEGIN{ r=mb*1000/ms; if(r>1200) r=1200; printf "%.1f", r }'
  return 0
}
# 秒数 → 中文 "x 小时 y 分 / x 分 y 秒 / x 秒"，写入 $2 指定的变量名
pw_eta_fmt() {
  local s="$1" out="$2" v
  if [ "$s" -ge 3600 ]; then v="$((s/3600)) 小时 $(( (s%3600)/60 )) 分"
  elif [ "$s" -ge 60 ]; then v="$((s/60)) 分 $((s%60)) 秒"
  else v="${s} 秒"; fi
  printf -v "$out" '%s' "$v"
}
pw_mig_link_check() {
  local target="$1" bytes="${2:-0}" ip info dev spd cap=0 mbs eff esrc eta ETA
  # 目标节点地址：优先 /etc/hosts（集群必有），取不到就把节点名交给 ip route get 自己解析
  ip=$(getent hosts "$target" 2>/dev/null | awk '{print $1; exit}' || true)
  # 链路协商速率（桥/bond/vlan 会下钻到物理口）
  info=$(pw_mig_link_probe "${ip:-$target}")
  dev=$(printf '%s' "$info" | awk '{print $1}')
  spd=$(printf '%s' "$info" | awk '{print $2}')
  if [ -n "$spd" ]; then
    cap=$(( spd / 8 ))
    echo "[链路] 到 $target 的出口网卡: $dev，协商速率: ${spd} Mb/s（理论上限约 ${cap} MB/s）"
    # 链路够快、按它算也不超阈值 → 无需实测，直接放行
    if [ "$bytes" -gt 0 ] 2>/dev/null && [ "$cap" -gt 0 ]; then
      eff=$(awk -v c="$cap" 'BEGIN{printf "%.1f", c*0.8}'); esrc="链路协商上限×80%"
      eta=$(awk -v b="$bytes" -v r="$eff" 'BEGIN{ printf "%.0f", b/(r*1048576) }')
      if [ "$eta" -lt 1800 ]; then
        pw_eta_fmt "$eta" ETA
        echo "[链路] 待复制数据: $(pw_human_size "$bytes")，预计耗时约 $ETA（按$esrc保守估算，含磁盘/SSH 开销会更久）"
        pw_mig_link_verdict "$spd"
        return 0
      fi
    fi
  else
    echo "[链路] 到 $target 的出口未读到协商速率（桥/虚拟口/无线/OVS 常无 speed 上报）"
  fi
  # 走到这里说明：速率读不到，或链路慢/数据大（耗时可能较长）——沿 SSH 实测一次再定论
  if [ "$bytes" -ge 1073741824 ] 2>/dev/null; then
    mbs=$(pw_mig_net_measure "${ip:-$target}")
    # 防御：测速命令失败时若错误文本被渗进 stdout，这里会非数字→清空，
    # 走“无法测量”分支的保守提示，而不是被当成“实测成功”
    case "$mbs" in ''|*[!0-9.]*) mbs="" ;; esac
    [ -n "$mbs" ] && echo "[链路] 实测到 $target 的可用吞吐: 约 ${mbs} MB/s（走 SSH 实测，与迁移同通道）"
  fi
  if [ -n "$mbs" ]; then eff="$mbs"; esrc="实测吞吐"
  elif [ "$cap" -gt 0 ]; then eff=$(awk -v c="$cap" 'BEGIN{printf "%.1f", c*0.8}'); esrc="链路协商上限×80%"
  fi
  if [ -z "$eff" ]; then
    # 既没实测到、也没读到协商速率：只提示不拦截，是否继续由用户判断
    [ "$bytes" -gt 0 ] 2>/dev/null && echo "[链路] 待复制数据: $(pw_human_size "$bytes")（无法测出带宽，耗时未知）"
    echo "[链路] 提示: 可用 iperf3 在两节点间复测实际带宽（对端 iperf3 -s，本端 iperf3 -c <对端IP>）"
    [ -n "$spd" ] && pw_mig_link_verdict "$spd"
    return 0
  fi
  if [ "$bytes" -gt 0 ] 2>/dev/null; then
    eta=$(awk -v b="$bytes" -v r="$eff" 'BEGIN{ printf "%.0f", b/(r*1048576) }')
    pw_eta_fmt "$eta" ETA
    echo "[链路] 待复制数据: $(pw_human_size "$bytes")，预计耗时约 $ETA（按$esrc估算）"
    # 耗时较长只提示不拦截：是否继续由用户判断
    if [ "$eta" -ge 1800 ]; then
      echo "[提示] 预计耗时较长：如需提速，优先修链路（千兆/2.5G 网线 + 交换机口）或把磁盘放到共享存储（共享存储上迁移不复制数据，秒级完成）"
    fi
  else
    per10=$(awk -v r="$eff" 'BEGIN{ if(r<=0){print 0; exit} printf "%.0f", 10240/r }')
    pw_eta_fmt "$per10" ETA
    echo "[链路] 参考耗时: 按 $esrc 复制 10 GB 约 $ETA"
  fi
  [ -n "$spd" ] && pw_mig_link_verdict "$spd"
  return 0
}
# 链路结论（按速率给结论与建议）：≤100M 点明网口瓶颈；<1000M 提示不足千兆；否则提示非链路瓶颈
pw_mig_link_verdict() {
  local spd="$1" cap=$(( spd / 8 ))
  if [ "$spd" -le 100 ]; then
    echo "[结论] 瓶颈是网口速率（${spd}Mb/s ≈ ${cap}MB/s）：12MB/s 上下就是百兆的物理极限，调参数/换存储架构都突破不了"
    echo "[建议] 查两端网线与交换机口是否被协商成 100M（六类线 + 千兆口，ethtool 看 Speed/Duplex），修好后约提速 10 倍"
  elif [ "$spd" -lt 1000 ]; then
    echo "[结论] 链路不足千兆（${spd}Mb/s）：确认网线是否只有 4 芯，或交换机口是百兆"
  else
    echo "[结论] 链路（${spd}Mb/s）不是瓶颈：若实测仍慢，瓶颈在源盘读/目标盘写，或老 CPU 上的 SSH 加密开销"
  fi
}

if [ "$GTYPE" = qm ]; then GUEST_CN="虚拟机"; else GUEST_CN="容器"; fi

# 状态与锁：锁定（备份/快照/克隆/上一次迁移未收尾）时 PVE 会直接拒绝迁移
ST=$(pw_mig_status "$VMID" || true)
case "$ST" in
  *lock*)
    echo "[错误] $VMID 当前处于锁定状态：$ST"
    echo "[解决] 等正在进行的备份/快照/克隆结束后重试；若是上次迁移残留的锁："
    echo "       虚机执行 qm unlock $VMID，容器执行 pct unlock $VMID"
    exit 1 ;;
esac
RUNNING=0
echo "$ST" | grep -q running && RUNNING=1

echo "===== 迁移$GUEST_CN $VMID：$LOCAL_NODE → $TARGET ====="
if [ "$MODE" = online ]; then
  echo "[方式] 在线迁移；当前状态: $([ "$RUNNING" = 1 ] && echo "运行中" || echo "已关机（将直接迁移，无需在线参数）")"
else
  echo "[方式] 离线迁移；当前状态: $([ "$RUNNING" = 1 ] && echo "运行中（迁移前会自动关机）" || echo "已关机")"
fi

# ── 在线迁移的硬性限制：直通设备与 host CPU 无法同步内存状态 ──
if [ "$MODE" = online ] && [ "$RUNNING" = 1 ]; then
  PTLINE=$(pw_mig_config "$VMID" | grep -E '^(hostpci|usb)[0-9]+:' || true)
  if [ -n "$PTLINE" ]; then
    echo "[错误] 该$GUEST_CN配置了硬件直通设备，无法在线迁移（内存状态在目标节点无法恢复）："
    echo "$PTLINE" | sed 's/^/       /'
    echo "[解决] 改选「离线迁移」（先关机、迁完自动开机）；直通设备需 $TARGET 上具备同样的硬件才能继续使用"
    exit 1
  fi
  CPU=$(pw_mig_config "$VMID" | awk -F': ' '/^cpu:/{print $2}')
  if [ "$CPU" = "host" ]; then
    echo "[错误] CPU 类型为 host，无法在线迁移：目标节点 CPU 特性未必一致，内存状态无法恢复"
    echo "[解决] 先改成通用类型再迁移（如 qm set $VMID --cpu x86-64-v2-AES），或改选「离线迁移」"
    exit 1
  fi
fi

# ── 磁盘位置探测：本地存储上的磁盘必须带 --with-local-disks ──
if [ "$GTYPE" = qm ]; then
  DISK_KEYS=$(pw_mig_config "$VMID" | grep -oE '^(scsi|virtio|sata|ide|efidisk|tpmstate)[0-9]+:' | tr -d ':' || true)
else
  DISK_KEYS=$(pw_mig_config "$VMID" | grep -oE '^(rootfs|mp[0-9]+):' | tr -d ':' || true)
fi
LOCAL_VOLS=""
LOCAL_BYTES=0
RDM_VOLS=""
for KEY in $DISK_KEYS; do
  LINE=$(pw_mig_config "$VMID" | grep "^$KEY:" || true)
  VOL=$(printf '%s' "$LINE" | sed 's/^[^:]*:[[:space:]]*//' | cut -d, -f1 || true)
  [ -n "$VOL" ] || continue
  case "$VOL" in
    none|*:iso/*|*:vztmpl/*) continue ;;   # 光驱/无介质：ISO 不参与磁盘迁移，别误报成「本地磁盘」
    /dev/*) RDM_VOLS="${RDM_VOLS}${RDM_VOLS:+, }$VOL（$KEY）" ;;
    *:*)
      # 共享存储上的磁盘不参与复制：跳过，也不计入待复制量
      pw_mig_is_shared "${VOL%%:*}" && continue
      LOCAL_VOLS="${LOCAL_VOLS}${LOCAL_VOLS:+, }$VOL"
      # 估算该卷待复制字节：优先磁盘实际已用（actual-size），取不到退回配置里的分配大小 size=
      b=$(pw_mig_vol_bytes "$VOL"); [ -z "$b" ] && b=$(pw_size_to_bytes "$LINE")
      LOCAL_BYTES=$(( LOCAL_BYTES + ${b:-0} ))
      ;;
    *)      LOCAL_VOLS="${LOCAL_VOLS}${LOCAL_VOLS:+, }$VOL" ;;
  esac
done
if [ -n "$RDM_VOLS" ]; then
  echo "[警告] 检测到直通物理盘（块映射 RDM）：$RDM_VOLS"
  echo "       物理盘不随迁移复制，目标节点需存在同一设备，否则迁移会失败并可能残留配置"
fi
if [ -n "$LOCAL_VOLS" ]; then
  echo "[磁盘] 以下磁盘在本地存储上，需随迁移一起复制：$LOCAL_VOLS"
  if [ "$MIG_LOCAL_DISKS" != "yes" ]; then
    if [ "$GTYPE" = pct ]; then
      # 容器没有「保留本地磁盘」这种选项：pct migrate 的合法选项只有
      # --bwlimit/--online/--restart/--target-storage/--timeout，本地卷由 PVE 自动经网络复制
      echo "[错误] 容器迁移没有「保留本地磁盘」这个选项（pct migrate 不支持 --with-local-disks）："
      echo "       本地卷一定会被复制到目标节点"
      echo "[解决] 把「本地磁盘」改成「一并迁移」后重试；大磁盘耗时取决于节点间带宽（进度见下方输出）"
    else
      echo "[错误] 磁盘在本地存储上，但「本地磁盘」选了「不迁移」——PVE 会直接拒绝这次迁移"
      echo "[解决] 把「本地磁盘」改成「一并迁移」；大磁盘会按节点间网络带宽耗时（进度见下方输出）"
    fi
    exit 1
  fi
  # 本地盘必须随迁：本次耗时完全由节点间链路决定，开始前先量一次并给出结论（含慢速拦截）
  pw_mig_link_check "$TARGET" "$LOCAL_BYTES"
else
  echo "[磁盘] 磁盘均在共享存储上：迁移不需要复制数据，通常秒级完成"
fi

# ── 离线迁移：PVE 只允许迁移已关机的实例，先把实例关掉 ──
if [ "$MODE" = offline ] && [ "$RUNNING" = 1 ]; then
  echo "[关机] 离线迁移需先关机：已发送关机指令（ACPI，最长等 180 秒）"
  if [ "$GTYPE" = qm ]; then qm shutdown "$VMID" >/dev/null 2>&1 || true; else pct shutdown "$VMID" >/dev/null 2>&1 || true; fi
  WAIT=0
  while [ "$WAIT" -lt 180 ]; do
    sleep 3
    WAIT=$((WAIT + 3))
    [ "$(pw_mig_status "$VMID" | awk '{print $2}')" = "stopped" ] && break
  done
  if [ "$(pw_mig_status "$VMID" | awk '{print $2}')" != "stopped" ]; then
    echo "[错误] $VMID 未在 180 秒内关机，已中止迁移（未做任何改动，实例仍在原节点运行）"
    echo "[解决] 到实例内正常关机后重试，或先在概览页「强制停止」再迁"
    exit 1
  fi
  echo "[关机] 已关机"
fi

# ── 组装迁移命令 ──
# pct migrate 的合法选项只有 --bwlimit / --online / --restart / --target-storage / --timeout
# （PVE 官方手册 pct(1)），所以：
#   · 不能传 --with-local-disks（那是 qm 的）——传了就是 "400 unable to parse option"；
#     容器的本地卷/挂载点由 PVE 自动经网络复制到目标节点，没有也不需要有开关。
#   · 不能传 --online——官方写明「Running containers cannot live-migrated due to technical
#     limitations」，运行中的容器只能用重启式迁移（--restart：停 → 迁 → 在目标节点启动）。
MIG_CMD=("$GTYPE" migrate "$VMID" "$TARGET")
if [ "$MODE" = online ] && [ "$RUNNING" = 1 ]; then
  if [ "$GTYPE" = qm ]; then
    MIG_CMD+=(--online)
    [ "$PT_CONNTRACK" = "1" ] && MIG_CMD+=(--with-conntrack-state)
  else
    # 容器：重启式迁移；切换瞬间秒级停机（下方会再提示一次）
    MIG_CMD+=(--restart)
  fi
fi
# --with-local-disks 只有 qm migrate 有；pct 传了会直接 400（容器不传，本地卷照样复制）
if [ "$MIG_LOCAL_DISKS" = "yes" ] && [ "$GTYPE" = qm ]; then
  MIG_CMD+=(--with-local-disks)
fi

echo ""
if [ "$MODE" = online ] && [ "$GTYPE" = pct ] && [ "$RUNNING" = 1 ]; then
  echo "[提示] 容器在线迁移采用「重启式迁移」：切换瞬间会有秒级停机，网络连接可能短暂中断"
fi
echo "[迁移] 开始执行：${MIG_CMD[*]}"
echo "[提示] 本地磁盘较大时耗时较长，请保持页面打开（进度实时输出在下方）"
run "迁移失败" "${MIG_CMD[@]}"

# ── 写后校验：VMID 确实已出现在目标节点上 ──
SUB=$([ "$GTYPE" = qm ] && echo qemu || echo lxc)
if pvesh get "/nodes/$TARGET/$SUB" --output-format json 2>/dev/null | grep -qE "\"vmid\"[[:space:]]*:[[:space:]]*$VMID([^0-9]|$)"; then
  echo "[完成] $VMID 已迁移到 $TARGET"
else
  echo "[错误] 迁移命令已返回，但在目标节点 $TARGET 的实例列表里没查到 $VMID"
  echo "[排查] 请核对上方完整输出；可手动确认：pvesh get /nodes/$TARGET/$SUB"
  exit 1
fi

# 离线迁移完成后的启动（在线迁移的实例本就是运行中，无需处理）
if [ "$MODE" = offline ] && [ "$PT_START" = "1" ]; then
  echo "[启动] 正在 $TARGET 上启动 $VMID ..."
  run "启动失败" "$GTYPE" start "$VMID"
  echo "[完成] $VMID 已在 $TARGET 上运行"
fi

echo ""
echo "[提示] 实例配置（含 MAC 地址）保持不变。若它引用了源节点本地的 ISO 镜像或 cloud-init 片段，"
echo "       迁移后需在 $TARGET 上补齐同名文件，否则光驱/初始化会失效"
true
