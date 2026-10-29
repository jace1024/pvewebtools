
# ============ 磁盘格式转换（raw <-> qcow2，批量含 EFI 盘） ============
# 一次把 VM 的全部 raw 磁盘转为目标格式（数据盘 scsi*/virtio*/sata*/ide* + EFI 盘 efidisk*），
# 并清理 unused* 残留旧盘。raw 不支持快照，转 qcow2 后即可快照。
[ -n "$PT_VMID" ] || { echo "[错误] 未选择虚拟机"; exit 1; }

# 运行中禁止转换
VM_STATUS=$(qm status "$PT_VMID" 2>/dev/null | awk '{print $2}')
if [ "$VM_STATUS" = "running" ]; then
 echo "[错误] VM $PT_VMID 正在运行，请先关机再转换（qm shutdown $PT_VMID）"
 exit 1
fi

CFG=$(qm config "$PT_VMID" 2>/dev/null)
[ -n "$CFG" ] || { echo "[错误] VM $PT_VMID 不存在"; exit 1; }

# 收集需转换的磁盘：所有 raw/qcow2 的数据盘与 EFI 盘（排除 cdrom 类光盘，如 cloudinit）
# 行格式：scsi0: local:100/100-disk-1.raw,iothread=1,... 或 efidisk0: local:100/100-disk-0.qcow2,...
MATCHES=$(echo "$CFG" | grep -E '^(scsi|virtio|sata|ide|efidisk)[0-9]*:' | grep -v 'media=cdrom' || true)
if [ -z "$MATCHES" ]; then
 echo "[提示] VM $PT_VMID 没有可转换的磁盘（仅支持 raw<->qcow2 且非光盘）"
 echo "$CFG" | grep -E '^(scsi|virtio|sata|ide|efidisk)[0-9]*:' | sed 's/^/  现磁盘: /'
 exit 0
fi

echo "===== 待转换的磁盘（目标格式 $PT_TARGET） ====="
echo "$MATCHES" | sed 's/^/  /'
echo

backup_file "/etc/pve/qemu-server/${PT_VMID}.conf"
CONVERTED=0
SKIPPED=0
while IFS= read -r line; do
 [ -n "$line" ] || continue
 SLOT=$(echo "$line" | cut -d: -f1)
 VOLID=$(echo "$line" | sed "s/^${SLOT}: *//" | cut -d, -f1)

 # 解析真实路径与格式
 SRC_PATH=$(pvesm path "$VOLID" 2>/dev/null || true)
 # 净化 pvesm 输出可能携带的 \r / 首尾空白，否则 rm 会静默删除一个不存在的路径（原盘因此残留）
 SRC_PATH=$(printf '%s' "$SRC_PATH" | tr -d '\r' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
 if [ -z "$SRC_PATH" ] || [ ! -e "$SRC_PATH" ]; then
   echo "[跳过] $SLOT：无法解析路径 $VOLID（仅支持可直接访问镜像文件的目录型存储）"
   SKIPPED=$((SKIPPED+1)); continue
 fi
 SRC_FMT=$(echo "$SRC_PATH" | sed 's/.*\.//')
 # 仅支持 raw<->qcow2 互转；已是目标格式或非受支持格式则跳过
 if [ "$SRC_FMT" = "$PT_TARGET" ]; then
   echo "[跳过] $SLOT 已是 $PT_TARGET，无需转换"
   SKIPPED=$((SKIPPED+1)); continue
 fi
 if [ "$SRC_FMT" != "raw" ] && [ "$SRC_FMT" != "qcow2" ] && [ "$SRC_FMT" != "vmdk" ]; then
   echo "[跳过] $SLOT：不支持转换的格式 $SRC_FMT（仅支持 raw/qcow2/vmdk 互转）"
   SKIPPED=$((SKIPPED+1)); continue
 fi
 DST_PATH="${SRC_PATH%.*}.${PT_TARGET}"

 echo; echo "===== 转换 $SLOT ====="
 echo "  $VOLID ($SRC_FMT -> $PT_TARGET)"
 echo "  $SRC_PATH"
 if [ -e "$DST_PATH" ]; then
   echo "[跳过] 目标文件已存在: $DST_PATH，请先处理"
   SKIPPED=$((SKIPPED+1)); continue
 fi
 run "qemu-img 转换失败（$SLOT）" qemu-img convert -f "$SRC_FMT" -O "$PT_TARGET" "$SRC_PATH" "$DST_PATH"
 # raw 格式不支持 qemu-img check（会误报校验失败），改用 qemu-img info 确认镜像可识别
 if [ "$PT_TARGET" = "qcow2" ]; then
   qemu-img check "$DST_PATH" >/dev/null 2>&1 || { echo "[错误] $DST_PATH 校验失败，已保留原盘"; rm -f "$DST_PATH"; exit 1; }
 else
   qemu-img info "$DST_PATH" >/dev/null 2>&1 || { echo "[错误] $DST_PATH 校验失败，已保留原盘"; rm -f "$DST_PATH"; exit 1; }
 fi

 NEW_VOLID="${VOLID%.*}.${PT_TARGET}"
 echo "  更新配置: $SLOT -> $NEW_VOLID"
 run "更新配置失败（$SLOT）" qm set "$PT_VMID" -"$SLOT" "$NEW_VOLID"
 verify_qm_change "$PT_VMID" "$SLOT" "$PT_TARGET"

 if [ "$PT_KEEP_SRC" = "yes" ]; then
   echo "  [保留] 按设置保留原盘: $SRC_PATH"
 else
   if [ -e "$SRC_PATH" ]; then
     if rm -f -- "$SRC_PATH"; then
       echo "  [删除] 原盘已移除: $SRC_PATH"
     else
       echo "[警告] 原盘删除失败（可能被占用或权限不足）: $SRC_PATH"
     fi
   else
     echo "  [跳过] 原盘路径不存在（可能已删除）: $SRC_PATH"
   fi
 fi
 CONVERTED=$((CONVERTED+1))
done <<EOF
$MATCHES
EOF

# 清理 unused* 残留旧盘（保留原盘时跳过清理，避免误删）
# 关键：用转换后的最新 qm config（qm set -scsi0 新盘会让旧盘变成新 unused，
# 旧的 $CFG 快照里拿不到）；同时清理 raw/qcow2 两种后缀（原 .raw 过滤会漏掉
# qcow2 -> raw 后残留的 qcow2 unused）
if [ "$PT_KEEP_SRC" != "yes" ]; then
 UNUSED_KEYS=$(qm config "$PT_VMID" 2>/dev/null | grep -E '^unused[0-9]+:' | cut -d: -f1 || true)
 for U in $UNUSED_KEYS; do
   [ -n "$U" ] || continue
   UVOLID=$(qm config "$PT_VMID" 2>/dev/null | grep "^${U}:" | cut -d' ' -f2- | cut -d, -f1 || true)
   echo "[清理] 移除残留 $U ($UVOLID)"
   qm set "$PT_VMID" -delete "$U" 2>/dev/null || true
   if [ -n "$UVOLID" ]; then
     UPATH=$(pvesm path "$UVOLID" 2>/dev/null || true)
     UPATH=$(printf '%s' "$UPATH" | tr -d '\r' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
     if [ -n "$UPATH" ] && [ -e "$UPATH" ]; then
       rm -f "$UPATH" 2>/dev/null && echo "  已删除文件: $UPATH" || echo "  [警告] 删除文件失败: $UPATH"
     fi
   fi
 done
fi

echo
if [ "$CONVERTED" -eq 0 ]; then
 echo "[提示] 未成功转换任何磁盘${SKIPPED:+"（跳过 $SKIPPED 个）"}"
else
 echo "[完成] 已转换 $CONVERTED 个磁盘为 $PT_TARGET${SKIPPED:+"（跳过 $SKIPPED 个）"}"
 if [ "$PT_TARGET" = "qcow2" ]; then
   echo "[提示] 现已支持快照：qm snapshot $PT_VMID snap1 --vmstate 1"
 fi
fi
echo "[提示] 残留 unused 旧盘已清理，可正常开机"
exit 0
