
VMID="$PT_VMID"
qm status "$VMID" >/dev/null 2>&1 || { echo "[错误] VM $VMID 不存在"; exit 1; }
CFG="/etc/pve/qemu-server/${VMID}.conf"

if [ "$PT_ACTION" = "pcie_add" ]; then
 [ -n "$PT_DEVICE" ] || { echo "[错误] 请选择 PCIe 设备"; exit 1; }
 # PCIe 直通前置条件：IOMMU 已开启
 ls /sys/kernel/iommu_groups 2>/dev/null | grep -q . || { echo "[错误] 未开启 IOMMU，请先执行「开启硬件直通」开启并重启宿主机"; exit 1; }
 # 同一设备已被其他 VM 直通时拦截（同设备不能分给两台 VM）
 OCC=$(grep -l "hostpci[0-9]*:.*$PT_DEVICE" /etc/pve/qemu-server/*.conf 2>/dev/null | head -1 || true)
 [ -z "$OCC" ] || { echo "[错误] 该设备已直通给 VM $(basename "$OCC" .conf)，请先从那边移除"; exit 1; }
 # 安全闸门：目标若是存储控制器（承载宿主在用盘 / 正被 vfio 占用无法确认）→ 拒绝执行。
 # UI 已把宿主在用盘置灰，此处再兜一层：陈旧下拉值、或直接调接口也绕不过去。
 # 只想直通整块磁盘的场景建议改用「磁盘直通 → 块设备映射（RDM）」。
 pw_guard_storage_ctrl "$PT_DEVICE" "直通（PCIe 设备）" || exit 1
 SLOT=""
 for i in $(seq 0 9); do
   qm config "$VMID" | grep -q "^hostpci${i}:" || { SLOT="hostpci${i}"; break; }
 done
 [ -n "$SLOT" ] || { echo "[错误] 没有空闲 hostpci 槽位"; exit 1; }
 backup_file "$CFG"
 # PCIe 设备不支持热插，运行中 VM 会在此报错，需先关机
 run "直通失败（VM 运行中请先关机）" qm set "$VMID" -$SLOT "$PT_DEVICE"
 verify_qm_change "$VMID" "$SLOT" "$PT_DEVICE"
 # 宿主驱动占用提示：驱动仍绑定设备时 VM 可能拿不到，需屏蔽驱动后重启宿主
 DRV=""
 [ -e "/sys/bus/pci/devices/$PT_DEVICE/driver" ] && DRV=$(basename "$(readlink -f "/sys/bus/pci/devices/$PT_DEVICE/driver")")
 [ -n "$DRV" ] && echo "[提示] 宿主当前由 $DRV 驱动占用该设备，若 VM 启动后无法使用，需屏蔽该驱动后重启宿主机"
 echo "[完成] PCIe 设备 $PT_DEVICE 已直通到 VM $VMID 的 $SLOT（启动 VM 后生效）"
 exit 0
fi

if [ "$PT_ACTION" = "pcie_del" ]; then
 [ -n "$PT_DEVICE" ] || { echo "[错误] 请选择 PCIe 设备"; exit 1; }
 SLOT=$(qm config "$VMID" | grep "^hostpci[0-9]*:.*$PT_DEVICE" | head -1 | cut -d: -f1 || true)
 [ -n "$SLOT" ] || { echo "[错误] 设备 $PT_DEVICE 未直通给 VM $VMID"; exit 1; }
 backup_file "$CFG"
 run "移除失败（VM 运行中请先关机）" qm set "$VMID" -delete "$SLOT"
 verify_qm_change "$VMID" "$SLOT" "$PT_DEVICE" neg
 echo "[完成] 已从 VM $VMID 移除 PCIe 直通 $PT_DEVICE（原槽位 $SLOT）"
 exit 0
fi

if [ "$PT_ACTION" = "usb_add" ]; then
 [ -n "$PT_USB_DEVICE" ] || { echo "[错误] 请选择 USB 设备"; exit 1; }
 if qm config "$VMID" | grep -q "^usb[0-9]*:.*host=$PT_USB_DEVICE"; then
   echo "[错误] $PT_USB_DEVICE 已直通给 VM $VMID"; exit 1
 fi
 # PVE 仅提供 usb0~usb4 五个槽位
 SLOT=""
 for i in 0 1 2 3 4; do
   qm config "$VMID" | grep -q "^usb${i}:" || { SLOT="usb${i}"; break; }
 done
 [ -n "$SLOT" ] || { echo "[错误] 没有空闲 usb 槽位（usb0-usb4 已占满）"; exit 1; }
 OPTS="host=$PT_USB_DEVICE"
 [ "$PT_USB3" = "yes" ] && OPTS="${OPTS},usb3=1"
 backup_file "$CFG"
 run "直通失败" qm set "$VMID" -$SLOT "$OPTS"
 # 写后验证：回读 VM 配置确认 USB 设备已挂上
 verify_qm_change "$VMID" "$SLOT" "host=$PT_USB_DEVICE"
 echo "[完成] USB 设备 $PT_USB_DEVICE 已直通到 VM $VMID 的 $SLOT（运行中 VM 即时生效，无需重启）"
 exit 0
fi

if [ "$PT_ACTION" = "usb_del" ]; then
 [ -n "$PT_USB_DEVICE" ] || { echo "[错误] 请选择 USB 设备"; exit 1; }
 SLOT=$(qm config "$VMID" | grep "^usb[0-9]*:.*host=$PT_USB_DEVICE" | head -1 | cut -d: -f1 || true)
 [ -n "$SLOT" ] || { echo "[错误] USB 设备 $PT_USB_DEVICE 未直通给 VM $VMID"; exit 1; }
 backup_file "$CFG"
 run "移除失败" qm set "$VMID" -delete "$SLOT"
 verify_qm_change "$VMID" "$SLOT" "host=$PT_USB_DEVICE" neg
 echo "[完成] 已从 VM $VMID 移除 USB 直通 $PT_USB_DEVICE（原槽位 $SLOT）"
 exit 0
fi

echo "[错误] 未知操作: $PT_ACTION"; exit 1
