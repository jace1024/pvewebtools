
# 宿主占用网口（有 IP / 默认路由出口 / vmbr 桥接成员 / bond 成员）对应的 PCI 地址集合：
# 直通任一都会导致宿主断网或丢管理通道，必须拦截
HOST_NIC_PCI=""
for IF in $(ip -o addr show 2>/dev/null | awk '/ inet /{print $2}' | sed 's/@.*//' | sort -u); do
 [ -e "/sys/class/net/$IF/device" ] && HOST_NIC_PCI="${HOST_NIC_PCI} $(basename "$(readlink -f "/sys/class/net/$IF/device")")"
done
for IF in $(grep -oE '^\s*(bridge-ports|bond-slaves)\s+.*' /etc/network/interfaces 2>/dev/null | awk '{$1=""; print}' | tr ' ' '\n' | grep -v '^$' | sort -u); do
 [ -e "/sys/class/net/$IF/device" ] && HOST_NIC_PCI="${HOST_NIC_PCI} $(basename "$(readlink -f "/sys/class/net/$IF/device")")"
done
DEFAULT_PCI=""
DEF_IF=$(ip -o route show default 2>/dev/null | awk '{print $5; exit}')
if [ -n "$DEF_IF" ] && [ -e "/sys/class/net/$DEF_IF/device" ]; then
 DEFAULT_PCI=$(basename "$(readlink -f "/sys/class/net/$DEF_IF/device")")
 HOST_NIC_PCI="${HOST_NIC_PCI} $DEFAULT_PCI"
fi

if [ "$PT_ACTION" = "add" ]; then
 VMID="$PT_VMID"
 qm status "$VMID" >/dev/null 2>&1 || { echo "[错误] VM $VMID 不存在"; exit 1; }
 # PCIe 直通前置条件：IOMMU 已开启（iommu_groups 非空）
 ls /sys/kernel/iommu_groups 2>/dev/null | grep -q . || { echo "[错误] 未开启 IOMMU，请先执行「开启硬件直通」开启并重启宿主机"; exit 1; }
 # 宿主占用网口拦截：PT_DEVICE 是完整 BDF（0000:xx:xx.x），逐项比对
 for HP in $HOST_NIC_PCI; do
   if [ "$PT_DEVICE" = "$HP" ]; then
     echo "[错误] $PT_DEVICE 是宿主占用的网口（有 IP/管理网口），直通后宿主将断网，已拦截"; exit 1
   fi
 done
 # 无线网卡提醒（不拦截）：无线（尤其 CNVi）在虚机里常认不到网口，软路由（爱快等）也不支持当 WAN/LAN；
 # 若确实要直通给 Linux 虚机做 AP（hostapd 等），可自行继续
 if lspci -nn -s "$PT_DEVICE" 2>/dev/null | grep -q '0280\|0281\|0282'; then
   echo "[警告] $PT_DEVICE 是无线网卡：虚机（尤其软路由）通常无法当网口使用，请确认你的用途"
 fi
 # 查重：按 bus:device 去 function、忽略可选 0000: domain 前缀匹配，避免漏判重复
 DUP_BD=$(echo "${PT_DEVICE%.*}" | sed 's/^0000://')
 if qm config "$VMID" | grep -qE "(^|;|,| )(0000:)?${DUP_BD}(;|,| |$)"; then echo "[错误] 该网卡已直通给 VM $VMID"; exit 1; fi
 # 隔离组整组闭环：同组内非桥接设备整组直通（组内共享 IOMMU 上下文，必须一起过），
 # 同 bus:device 多 function 用缩短语法合并，根端口/桥自动跳过
 GROUP_DEVS=$(iommu_group_devices "$PT_DEVICE" || true)
 DEV_LIST="$PT_DEVICE"
 if [ -n "$GROUP_DEVS" ]; then
   DEV_LIST=$(printf '%s\n' "$GROUP_DEVS" | paste -sd ';' -)
   N_DEV=$(printf '%s\n' "$GROUP_DEVS" | grep -c .)
   if [ "$N_DEV" -gt 1 ]; then
     # 整组连带时若组内含宿主占用网口，必须拦截（否则宿主断网）
     # HOST_NIC_PCI 里是完整 BDF（0000:xx:xx.x），组内是缩短 bus:device，取前 7 位 bus:device 比对
     for d in $(printf '%s\n' "$GROUP_DEVS"); do
       for HP in $HOST_NIC_PCI; do
         HP_BD="${HP%.*}"
         [ "$d" = "$HP_BD" ] && { echo "[错误] 整组直通会连带宿主占用网口 $HP，宿主将断网，已拦截"; exit 1; }
       done
     done
     # 组内混入存储控制器同样要拦（与「显卡直通」同口径）：整组直通会把该控制器交给虚机 →
     # 宿主机掉盘；虚机磁盘若也挂在这个控制器上，qm start 会因读不到 qcow2 而失败。
     # 统一走 COMMON_HELPERS 的 pw_guard_storage_ctrl：承载宿主在用盘、或正被 vfio 占用
     # （此时宿主看不到它上面的盘，无法证明安全）都直接拒绝执行。
     for d in $(printf '%s\n' "$GROUP_DEVS"); do
       pw_guard_storage_ctrl "$d" "整组直通（$PT_DEVICE 所在隔离组）" || exit 1
     done
     echo "[隔离组] $PT_DEVICE 所在组含 $N_DEV 个可直通设备，已自动整组直通：$DEV_LIST"
     echo "       整组直通的代价：这些设备在宿主机上会一并消失（含同组 USB/音频等），VM 关机后自动归还"
   fi
 fi
 SLOT=""
 for i in $(seq 0 9); do
   qm config "$VMID" | grep -q "^hostpci${i}:" || { SLOT="hostpci${i}"; break; }
 done
 [ -n "$SLOT" ] || { echo "[错误] 没有空闲 hostpci 槽位"; exit 1; }
 backup_file "/etc/pve/qemu-server/${VMID}.conf"
 # PCIe 设备不支持热插，运行中 VM 会在此报错，需先关机
 # 多设备用分号连接写入同一 hostpci 槽位（整组一起过）
 run "直通失败（VM 运行中请先关机）" qm set "$VMID" -$SLOT "$DEV_LIST"
 MAIN_BD="${PT_DEVICE%.*}"
 verify_qm_change "$VMID" "$SLOT" "$MAIN_BD"
 echo "[完成] PCIe 网卡 $DEV_LIST 已直通到 VM $VMID 的 $SLOT"
 echo "[重要] PCIe 直通不支持热插，需先「关机」再「开机」才真正生效：qm shutdown $VMID && qm start $VMID"
 exit 0
fi

if [ "$PT_ACTION" = "del" ]; then
 VMID="$PT_VMID"
 qm status "$VMID" >/dev/null 2>&1 || { echo "[错误] VM $VMID 不存在"; exit 1; }
 # 直通时是整组一起写入同一槽位的，移除时按槽位整组删除（附带设备一并移除，不留孤儿）
 # 匹配用 bus:device（去 .function 后缀），与 add 写入的缩短语法一致
 MAIN_BD="${PT_DEVICE%.*}"
 SLOT=$(qm config "$VMID" | grep "^hostpci[0-9]*:.*${MAIN_BD}" | head -1 | cut -d: -f1 || true)
 [ -n "$SLOT" ] || { echo "[错误] 网卡 $PT_DEVICE 未直通给 VM $VMID"; exit 1; }
 backup_file "/etc/pve/qemu-server/${VMID}.conf"
 run "移除失败（VM 运行中请先关机）" qm set "$VMID" -delete "$SLOT"
 verify_qm_change "$VMID" "$SLOT" "$MAIN_BD" neg
 echo "[完成] 已从 VM $VMID 移除网卡直通（原槽位 $SLOT，含同组附带设备）"
 exit 0
fi

echo "[错误] 未知操作: $PT_ACTION"; exit 1
