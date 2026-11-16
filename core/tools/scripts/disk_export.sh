
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
  # ============ 磁盘导出（转换格式 + 落盘，供跨云迁移下载） ============
[ -n "$PT_VMID" ] || { echo "[错误] 未选择虚拟机"; exit 1; }
[ -n "$PT_TARGET" ] || { echo "[错误] 未选择目标格式"; exit 1; }
[ -n "$PT_STORAGE" ] || { echo "[错误] 未选择导出存储"; exit 1; }

# 运行中禁止导出（需停机保证一致性）
VM_STATUS=$(qm status "$PT_VMID" 2>/dev/null | awk '{print $2}')
if [ "$VM_STATUS" = "running" ]; then
 echo "[错误] VM $PT_VMID 正在运行，请先关机再导出（qm shutdown $PT_VMID）"
 exit 1
fi

CFG=$(qm config "$PT_VMID" 2>/dev/null)
[ -n "$CFG" ] || { echo "[错误] VM $PT_VMID 不存在"; exit 1; }

# 解析导出目录（dir/nfs/cifs 等带 path 的存储根目录；不依赖 pvesm 易变的输出格式）
EXPORT_BASE=$(awk -v s="$PT_STORAGE" '
 /^dir:|^nfs:|^cifs:|^glusterfs:/ { cur=$2; type=1; next }
 /^[^[:space:]]/ { type=0; cur="" }
 type==1 && $1=="path" { if(cur==s){print $2; exit} }
' /etc/pve/storage.cfg 2>/dev/null)
# 兜底：尝试 pvesm path（部分特殊存储类型或无 storage.cfg 时）
if [ -z "$EXPORT_BASE" ]; then
 EXPORT_BASE=$(pvesm path "$PT_STORAGE:" 2>/dev/null | sed 's#/$##')
fi
[ -n "$EXPORT_BASE" ] || { echo "[错误] 无法解析导出存储目录: $PT_STORAGE"; exit 1; }
# 导出专用子目录：与 PVE 自身目录（images/iso/dump 等）隔离，避免污染存储根、也不会误列
EXPORT_DIR="$EXPORT_BASE/.pvewebtools/export"
mkdir -p "$EXPORT_DIR" 2>/dev/null || { echo "[错误] 无法创建导出目录: $EXPORT_DIR"; exit 1; }

# 收集磁盘：指定 disk 则只导出该槽位；否则全部数据盘 + EFI 盘
# 排除 cdrom 光驱与 cloud-init 配置盘（后者为 PVE 自动生成的初始化盘，还原时由 qm cloudinit update 重新生成，导出无意义）
if [ -n "$PT_DISK" ]; then
 MATCHES=$(echo "$CFG" | grep -E "^${PT_DISK}:" | grep -v 'media=cdrom' | grep -v ',cloudinit' || true)
else
 MATCHES=$(echo "$CFG" | grep -E '^(scsi|virtio|sata|ide|efidisk)[0-9]*:' | grep -v 'media=cdrom' | grep -v ',cloudinit' || true)
fi
if [ -z "$MATCHES" ]; then
 echo "[提示] 没有可导出的磁盘"
 exit 0
fi

EXPORTED_COUNT=0
while IFS= read -r line; do
 [ -n "$line" ] || continue
 SLOT=$(echo "$line" | cut -d: -f1)
 VOLID=$(echo "$line" | sed "s/^${SLOT}: *//" | cut -d, -f1)
 SRC_PATH=$(pvesm path "$VOLID" 2>/dev/null | tr -d '\r' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
 [ -n "$SRC_PATH" ] || { echo "[跳过] $SLOT 无法解析路径 $VOLID"; continue; }
 DST_NAME="${PT_VMID}-${SLOT}.${PT_TARGET}"
 DST_PATH="$EXPORT_DIR/$DST_NAME"
 if [ -e "$DST_PATH" ]; then echo "[跳过] 目标已存在: $DST_PATH"; continue; fi
 echo "===== 导出 $SLOT -> $DST_NAME ====="
 run "导出失败（$SLOT）" qemu-img convert -p -O "$PT_TARGET" "$SRC_PATH" "$DST_PATH"
 echo "[EXPORTED] $PT_STORAGE $DST_NAME"
 EXPORTED_COUNT=$((EXPORTED_COUNT+1))
done <<EOF
$MATCHES
EOF

[ "$EXPORTED_COUNT" -gt 0 ] && echo "[完成] 已导出 $EXPORTED_COUNT 个磁盘到 $PT_STORAGE" || echo "[提示] 未导出任何磁盘"
exit 0
  )
done
echo "===== 全部 $N 台虚机处理完成 ====="
