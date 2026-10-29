
# ============ 磁盘新增与移除 ============
VMID="$PT_VMID"
[ -n "$VMID" ] || { echo "[错误] 未选择虚拟机"; exit 1; }
qm status "$VMID" >/dev/null 2>&1 || { echo "[错误] 虚拟机 $VMID 不存在"; exit 1; }

if [ "$PT_ACTION" = "add" ]; then
 BUS="$PT_BUS"; STORAGE="$PT_STORAGE"; SIZE="$PT_SIZE"
 [ -n "$BUS" ] || { echo "[错误] 请选择磁盘总线"; exit 1; }
 [ -n "$STORAGE" ] || { echo "[错误] 请选择目标存储"; exit 1; }
 [ -n "$SIZE" ] || { echo "[错误] 请填写磁盘容量"; exit 1; }
 SIZE=$(echo "$SIZE" | tr 'a-z' 'A-Z')
 echo "$SIZE" | grep -qE '^[0-9]+[GTKM]?$' || { echo "[错误] 容量格式不正确（数字+可选单位 G/T/M/K，如 32G）"; exit 1; }
 pvesm status 2>/dev/null | awk '{print $1}' | grep -qx "$STORAGE" || { echo "[错误] 存储 $STORAGE 不存在"; exit 1; }
 MAX=30
 case "$BUS" in
   scsi) MAX=30;; virtio) MAX=15;; sata) MAX=5;; ide) MAX=3;;
   *) echo "[错误] 未知总线: $BUS"; exit 1;;
 esac
 EXIST=$(qm config "$VMID" 2>/dev/null | grep -E "^${BUS}[0-9]+:" | sed -E "s/^${BUS}([0-9]+):.*/\1/" | sort -n)
 NEXT=""
 for i in $(seq 0 $MAX); do
   echo "$EXIST" | grep -qx "$i" || { NEXT=$i; break; }
 done
 [ -n "$NEXT" ] || { echo "[错误] $BUS 总线槽位已占满（0-$MAX）"; exit 1; }
 SLOT="${BUS}${NEXT}"
 echo "[提示] 将在 $VMID 的 $SLOT 新增 ${SIZE} 磁盘（存储 $STORAGE）"
 backup_file "/etc/pve/qemu-server/${VMID}.conf"
run "新增磁盘失败" qm set "$VMID" --"$SLOT" "$STORAGE:$SIZE"
 verify_qm_change "$VMID" "$SLOT" "$STORAGE"
 echo "[完成] 已为 $VMID 新增磁盘 $SLOT（${SIZE}，$STORAGE）"
 VM_STATE=$(qm status "$VMID" 2>/dev/null | awk '{print $2}')
 [ "$VM_STATE" = "running" ] && echo "[提示] 虚机运行中：若未识别新盘，请确认已开磁盘热插拔（VM 选项→Hotplug），或系统内 rescan/重启"
 exit 0
fi

if [ "$PT_ACTION" = "remove" ]; then
 DISK="$PT_DISK"
 [ -n "$DISK" ] || { echo "[错误] 请选择要移除的磁盘"; exit 1; }
 qm config "$VMID" 2>/dev/null | grep -qE "^${DISK}:" || { echo "[错误] $VMID 上不存在磁盘 $DISK（可能已被移除）"; exit 1; }
 VM_STATE=$(qm status "$VMID" 2>/dev/null | awk '{print $2}')
 [ "$VM_STATE" = "running" ] && { echo "[错误] 移除磁盘需先关闭虚机（$VMID 运行中），请关机后重试"; exit 1; }
 if [ "$PT_KEEP_DISK" = "1" ]; then
   echo "[提示] 仅卸载 $DISK，保留磁盘（可重新挂载）"
   backup_file "/etc/pve/qemu-server/${VMID}.conf"
run "卸载失败" qm unlink "$VMID" --idlist "$DISK"
   verify_qm_change "$VMID" "$DISK" "^${DISK}:" neg
   echo "[完成] 已卸载 $VMID 的 $DISK（磁盘保留）"
 else
   echo "[警告] 将彻底删除 $DISK 及其数据，不可恢复"
   backup_file "/etc/pve/qemu-server/${VMID}.conf"
run "删除磁盘失败" qm set "$VMID" --delete "$DISK"
   verify_qm_change "$VMID" "$DISK" "^${DISK}:" neg
   echo "[完成] 已彻底删除 $VMID 的 $DISK（磁盘一并删除，数据不可恢复）"
 fi
 exit 0
fi
echo "[错误] 未知操作: $PT_ACTION"; exit 1
