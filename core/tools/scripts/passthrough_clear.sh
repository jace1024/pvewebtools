
VMID="$PT_VMID"
qm status "$VMID" >/dev/null 2>&1 || { echo "[错误] VM $VMID 不存在"; exit 1; }
CFG="/etc/pve/qemu-server/${VMID}.conf"
# 收集三类直通槽位：hostpci（PCIe）、usb、RDM 盘（普通镜像盘不动）。
# RDM 有两种存储形式：file=/dev/...（PVE 生成的 RDM）或直接写 /dev/disk/by-id/...（直接引用路径），
# 统一按含 /dev/ 路径匹配，避免漏掉直接引用 by-id 的 RDM 盘
S1=$(qm config "$VMID" | { grep -E '^(hostpci|usb)[0-9]+:' || true; } | cut -d: -f1)
S2=$(qm config "$VMID" | { grep -E '^\S+:.*(/dev/|file=/dev/)' || true; } | cut -d: -f1)
SLOTS=$(printf '%s\n%s\n' "$S1" "$S2" | { grep -v '^[[:space:]]*$' || true; } | sort -u)
if [ -z "$SLOTS" ]; then
 echo "[提示] VM $VMID 没有任何直通设备，无需操作"
 exit 0
fi
echo "待移除槽位: $(printf '%s' "$SLOTS" | tr '\n' ' ')"

# PCIe 直通不支持热插：运行中的 VM 必须先关机才能改配置，移除后再开回来（一次开关机闭环）；
# 开始时是关机状态则只移除、保持关机
WAS_RUNNING=0
RESTARTED=0
QST=$(qm status "$VMID" 2>/dev/null | awk '{print $2}')
if [ "$QST" = "running" ]; then
 WAS_RUNNING=1
 echo "[关机] 直通配置改动需要关机状态，正在关机 VM $VMID（最长等 120 秒）..."
 qm shutdown "$VMID" >/dev/null 2>&1 || true
 WAIT=0
 while [ "$WAIT" -lt 120 ]; do
   sleep 2
   WAIT=$((WAIT + 2))
   if [ "$(qm status "$VMID" 2>/dev/null | awk '{print $2}')" = "stopped" ]; then break; fi
 done
 if [ "$(qm status "$VMID" 2>/dev/null | awk '{print $2}')" != "stopped" ]; then
   echo "[错误] VM $VMID 未在 120 秒内关机（虚机可能未响应 ACPI 关机）。请手动关机后重新执行本工具。"
   exit 1
 fi
 echo "[关机] 已关机，开始移除直通配置"
fi
# 恢复原有运行状态：无论成功失败都把（原本运行中的）VM 开回来，不留在关机状态
restore_power() {
 if [ "$WAS_RUNNING" != "1" ] || [ "$RESTARTED" = "1" ]; then return 0; fi
 RESTARTED=1
 echo
 echo "[开机] 正在开机 VM $VMID（恢复原有运行状态）..."
 if qm start "$VMID" >/dev/null 2>&1; then
   echo "[完成] VM $VMID 已开机（直通设备已不再挂载到该 VM）"
 else
   echo "[警告] 自动开机失败，请手动开机：qm start $VMID"
 fi
}

backup_file "$CFG"
FAIL=0
# 记录被移除的 PCIe 设备 BDF，便于移除后检查 vfio 驱动是否归还
PCI_BDFS=""
for S in $SLOTS; do
 # 若是 hostpci 槽位，先记录其设备地址（如 0000:01:00 或 0000:01:00.0）
 if echo "$S" | grep -qE '^hostpci[0-9]+$'; then
   HLINE=$(qm config "$VMID" 2>/dev/null | grep "^${S}:" | sed 's/^[^:]*:[[:space:]]*//' | cut -d, -f1)
   # 解析出 BDF 部分（去掉逗号参数与空白）
   for HBD in $HLINE; do
     PCI_BDFS="${PCI_BDFS}${PCI_BDFS:+ }$(echo "$HBD" | cut -d, -f1)"
   done
 fi
 # PCIe 不支持热插：运行中 VM 移除 hostpci 会失败并计入 FAIL，提示先关机
 if qm set "$VMID" -delete "$S" 2>/dev/null; then
   echo "[移除] $S ✓"
 else
   echo "[失败] $S 移除失败（VM 运行中时 PCIe 直通需先关机）"; FAIL=1
 fi
done
# 写后验证：三类直通配置应全部清空
LEFT=$(qm config "$VMID" | { grep -E '^(hostpci|usb)[0-9]+:' || true; } | wc -l)
LEFT=$((LEFT + $(qm config "$VMID" | { grep -cE '^\S+:.*(/dev/|file=/dev/)' || true; })))
if [ "$LEFT" -gt 0 ]; then echo "[错误] 仍有 $LEFT 个直通项未移除（PCIe 项请先关闭 VM 后重试）"; restore_power; exit 1; fi
if [ "$FAIL" = 1 ]; then restore_power; exit 1; fi
echo "[完成] VM $VMID 的全部直通设备已移除（原配置已备份）"

# ===== 归还驱动：PVE 删掉 hostpci 配置后不会自动归还驱动，设备仍卡在 vfio-pci 上 ====
# 这里直接解绑 vfio 并装回原生驱动（如 amdgpu/nvme/igb），无需重启宿主机。
# hostpci 可能只写到 bus:device（不带 function），按 glob 展开出该设备的所有 function 逐个处理。
NEED_REBOOT=0
for BDF in $PCI_BDFS; do
 # 补 0000: 前缀（sysfs 路径需要完整形式），再展开为该 BDF 下的全部 function
 BASE="$BDF"
 echo "$BASE" | grep -qE '^[0-9a-fA-F]{4}:' || BASE="0000:${BASE}"
 FUNCS=$(ls -d /sys/bus/pci/devices/${BASE}* 2>/dev/null | xargs -n1 basename 2>/dev/null || true)
 [ -n "$FUNCS" ] || FUNCS="$BASE"
 for FULLBDF in $FUNCS; do
   DRV=""
   if [ -e "/sys/bus/pci/devices/$FULLBDF/driver" ]; then
     DRV=$(basename "$(readlink -f "/sys/bus/pci/devices/$FULLBDF/driver" 2>/dev/null)" 2>/dev/null || true)
   fi
   # 只处理仍卡在 vfio、或上次释放后没装回驱动的；原生驱动已持有则跳过
   if [ -n "$DRV" ] && [ "$DRV" != "vfio-pci" ]; then continue; fi
   if release_vfio_device "$FULLBDF"; then
     echo "[归还] $FULLBDF 已装回原生驱动（宿主机可立即使用，无需重启）"
   else
     echo "[提示] $FULLBDF 仍被 vfio-pci 占用，未能自动归还"
     NEED_REBOOT=1
   fi
 done
done
if [ -z "$PCI_BDFS" ]; then
 echo "[提示] 本次未移除 PCIe 直通设备，无需归还驱动"
elif [ "$NEED_REBOOT" = "1" ]; then
 echo "[重要] 部分 PCIe 设备仍被 vfio-pci 占用，宿主机暂无法使用这些设备"
 echo "[建议] 请重启宿主机以释放 vfio 并归还原生驱动：reboot"
 echo "[说明] 重启后设备将回归原生驱动（如 amdgpu/nvme/igb），概览将显示为正常使用状态"
else
 echo "[完成] PCIe 设备均已归还宿主机驱动，无需重启"
fi

# 开始时是运行中的：开回来（移除已生效，设备不会再挂到该 VM）
restore_power
