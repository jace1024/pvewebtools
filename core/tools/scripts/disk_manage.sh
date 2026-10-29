
# 宿主在用盘（枚举口径见 COMMON_HELPERS 的 pw_host_used_disks；RDM 路径用它拦系统盘/已挂载盘）。
# 「存储控制器」的直通安全判定统一走 COMMON_HELPERS 的 pw_guard_storage_ctrl（风险不明即拒绝执行）。
HOST_USED_DISKS=$(pw_host_used_disks)

if [ "$PT_ACTION" = "add" ]; then
 VMID="$PT_VMID"
 qm status "$VMID" >/dev/null 2>&1 || { echo "[错误] VM $VMID 不存在"; exit 1; }

 if [ "$PT_MODE" = "pcie" ]; then
   # PCIe 直通前置条件：IOMMU 已开启（iommu_groups 非空）
   ls /sys/kernel/iommu_groups 2>/dev/null | grep -q . || { echo "[错误] 未开启 IOMMU，请先执行「开启硬件直通」开启并重启宿主机"; exit 1; }
   # 安全闸门：目标是存储控制器时，承载宿主在用盘 / 正被 vfio 占用（无法确认）→ 拒绝执行
   pw_guard_storage_ctrl "$PT_DEVICE" "直通（PCIe 设备）" || exit 1
   # 已知 MSI-X 布局与 QEMU vfio 不兼容的存储设备（如 Intel SSD 760p/Pro 7600p 系列），
   # 直通会报 "failed to add PCI capability 0x11" 启动失败，直接拦截并引导改用块映射 RDM
   PCI_VD=$(lspci -nn -s "$PT_DEVICE" 2>/dev/null | grep -oE '\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]' | head -1 | tr -d '[]' | tr 'A-F' 'a-f')
   case "$PCI_VD" in
     8086:f1a6)
       echo "[错误] 设备 $PT_DEVICE（$PCI_VD）的 MSI-X 布局与 QEMU 直通不兼容，PCIe 直通会启动失败"
       echo "[建议] 请改用「块设备映射（RDM）」方式直通该磁盘（不经过 vfio/MSI-X，稳定可用）"
       exit 1
       ;;
   esac
   # 查重：配置里存的是缩短语法 bus:device（如 01:00，可能带/不带 0000: domain 前缀），
   # 须按 bus:device 去 function 且忽略可选 domain 前缀来匹配，否则完整 BDF 永远匹配不到会漏判重复
   DUP_BD=$(echo "${PT_DEVICE%.*}" | sed 's/^0000://')
   if qm config "$VMID" | grep -qE "(^|;|,| )(0000:)?${DUP_BD}(;|,| |$)"; then echo "[错误] 该设备已直通给 VM $VMID"; exit 1; fi
   # 隔离组整组闭环：同组内非桥接设备整组直通（组内共享 IOMMU 上下文，必须一起过），
   # 同 bus:device 多 function 用缩短语法合并，根端口/桥自动跳过
   GROUP_DEVS=$(iommu_group_devices "$PT_DEVICE" || true)
   DEV_LIST="$PT_DEVICE"
   if [ -n "$GROUP_DEVS" ]; then
     # 整组连带：组内每个设备都过一遍安全闸门（存储控制器承载宿主盘 / 正被 vfio 占用 → 拒绝执行）
     for d in $(printf '%s\n' "$GROUP_DEVS"); do
       pw_guard_storage_ctrl "$d" "整组直通（$PT_DEVICE 所在隔离组）" || exit 1
     done
     DEV_LIST=$(printf '%s\n' "$GROUP_DEVS" | paste -sd ';' -)
     N_DEV=$(printf '%s\n' "$GROUP_DEVS" | grep -c .)
     [ "$N_DEV" -gt 1 ] && echo "[隔离组] $PT_DEVICE 所在组含 $N_DEV 个可直通设备，已自动整组直通：$DEV_LIST"
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
   # 写后验证：主设备（去 function/domain 前缀后取 bus:device）应出现在槽位里
   MAIN_BD="${PT_DEVICE%.*}"
   verify_qm_change "$VMID" "$SLOT" "$MAIN_BD"
   echo "[完成] PCIe 设备 $DEV_LIST 已直通到 VM $VMID 的 $SLOT"
   echo "[重要] PCIe 直通不支持热插，需先「关机」再「开机」才真正生效：qm shutdown $VMID && qm start $VMID"
   exit 0
 fi

 # 块设备映射（RDM）
 DISK="/dev/disk/by-id/$PT_DEVICE"; BUS="$PT_BUS"
 [ -e "$DISK" ] || { echo "[错误] 磁盘不存在: $DISK"; exit 1; }
 # 系统盘/已挂载盘拦截：RDM 直通这些盘会毁掉宿主机
 REAL_DEV=$(basename "$(readlink -f "$DISK" 2>/dev/null || echo "$DISK")")
 for HD in $HOST_USED_DISKS; do
   [ "$REAL_DEV" = "$HD" ] && { echo "[错误] 磁盘 $PT_DEVICE（$REAL_DEV）是系统盘/已挂载盘，禁止直通"; exit 1; }
 done
 if qm config "$VMID" | grep -q "$PT_DEVICE"; then echo "[错误] 该磁盘已挂载到 VM $VMID"; exit 1; fi
 SLOT=""
 for i in $(seq 1 15); do
   qm config "$VMID" | grep -q "^${BUS}${i}:" || { SLOT="${BUS}${i}"; break; }
 done
 [ -n "$SLOT" ] || { echo "[错误] 没有空闲 $BUS 槽位"; exit 1; }
 backup_file "/etc/pve/qemu-server/${VMID}.conf"
 run "qm set 执行失败" qm set "$VMID" -$SLOT "$DISK"
 # 写后验证：回读 VM 配置确认磁盘真的挂上了
 verify_qm_change "$VMID" "$SLOT" "$PT_DEVICE"
 echo "[完成] $DISK 已挂载到 VM $VMID 的 $SLOT"
 exit 0
fi

if [ "$PT_ACTION" = "del" ]; then
 VMID="$PT_VMID"
 qm status "$VMID" >/dev/null 2>&1 || { echo "[错误] VM $VMID 不存在"; exit 1; }

 if [ "$PT_MODE" = "pcie" ]; then
   # 直通时是整组一起写入同一槽位的，移除时按槽位整组删除（附带设备一并移除，不留孤儿）
   # 匹配用 bus:device（去 .function 后缀），与 add 写入的缩短语法一致
   MAIN_BD="${PT_DEVICE%.*}"
   SLOT=$(qm config "$VMID" | grep "^hostpci[0-9]*:.*${MAIN_BD}" | head -1 | cut -d: -f1 || true)
   [ -n "$SLOT" ] || { echo "[错误] 设备 $PT_DEVICE 未直通给 VM $VMID"; exit 1; }
   backup_file "/etc/pve/qemu-server/${VMID}.conf"
   run "移除失败（VM 运行中请先关机）" qm set "$VMID" -delete "$SLOT"
   verify_qm_change "$VMID" "$SLOT" "$MAIN_BD" neg
   echo "[完成] 已从 VM $VMID 移除 PCIe 直通设备（原槽位 $SLOT，含同组附带设备）"
   # 自动归还驱动：移除直通后，设备可能仍绑在 vfio-pci 上，导致宿主机「掉盘」
   # （/dev/nvmeXnY 消失）。逐个把该组内被 vfio-pci 占用的设备归还给原生驱动（如 nvme）。
   # PT_DEVICE 可能是完整 BDF（0000:03:00.0）或缩短语法，统一按 sysfs 里实际存在的 BDF 处理。
   for BDF in $(ls /sys/kernel/iommu_groups/*/devices/ 2>/dev/null | xargs -n1 basename 2>/dev/null); do
     BD="${BDF%.*}"
     [ "$BD" = "$MAIN_BD" ] || [ "$BDF" = "$PT_DEVICE" ] || continue
     if [ -e "/sys/bus/pci/devices/$BDF/driver" ] && [ "$(basename "$(readlink -f "/sys/bus/pci/devices/$BDF/driver")")" = "vfio-pci" ]; then
       if release_vfio_device "$BDF"; then
         echo "[归还] 已将 $BDF 从 vfio-pci 归还给原生驱动（宿主机重新识别该设备）"
       else
         echo "[提示] $BDF 仍被 vfio-pci 占用且无法自动归还，可手动执行: echo $BDF > /sys/bus/pci/drivers/vfio-pci/unbind"
       fi
     fi
   done
   exit 0
 fi

 # 块设备映射移除：从 VM 配置里找到该磁盘占用的槽位（可能挂在任意总线）
 SLOT=$(qm config "$VMID" | grep "file=.*$PT_DEVICE\|$PT_DEVICE" | head -1 | cut -d: -f1 || true)
 [ -n "$SLOT" ] || { echo "[错误] 磁盘 $PT_DEVICE 未挂载到 VM $VMID"; exit 1; }
 backup_file "/etc/pve/qemu-server/${VMID}.conf"
 # 运行中的 VM 会走热拔插；Windows 等客户机里磁盘仍被占用时会报 "still busy in guest"
 # 导致热拔失败。这里区分两种状态给出明确指引，而不是让用户面对生硬的原始报错。
 VM_STATUS=$(qm status "$VMID" 2>/dev/null | awk '{print $2}')
 if [ "$VM_STATUS" = "running" ]; then
   echo "[提示] VM $VMID 正在运行，尝试热拔除 $SLOT（客户机内请先停用/弹出该磁盘）..."
   if qm set "$VMID" -delete "$SLOT" 2>/tmp/qm_hotunplug.err; then
     :
   else
     if grep -q "hot-unplugging\|still busy" /tmp/qm_hotunplug.err 2>/dev/null; then
       echo "[失败] 热拔除失败：磁盘 $PT_DEVICE 在客户机内仍被占用（still busy in guest）"
       echo "[提示] Windows 客户机请先「磁盘管理 → 将磁盘脱机」或关机后再移除"
       echo "[建议] 最稳妥：先执行 qm shutdown $VMID，关机后再重新执行本操作"
     else
       cat /tmp/qm_hotunplug.err 2>/dev/null
       echo "[失败] 移除磁盘 $PT_DEVICE 失败，请检查客户机内磁盘是否正在使用"
     fi
     rm -f /tmp/qm_hotunplug.err
     exit 1
   fi
   rm -f /tmp/qm_hotunplug.err
 else
   run "移除失败" qm set "$VMID" -delete "$SLOT"
 fi
 # 写后验证：回读配置确认磁盘已移除
 verify_qm_change "$VMID" "$SLOT" "$PT_DEVICE" neg
 echo "[完成] 已从 VM $VMID 移除磁盘 $PT_DEVICE（原槽位 $SLOT）"
 exit 0
fi

echo "[错误] 未知操作: $PT_ACTION"; exit 1
