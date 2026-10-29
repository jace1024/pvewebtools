
# ============ 磁盘导入（上传镜像 -> qm importdisk -> 自动挂载到总线） ============
[ -n "$PT_VMID" ] || { echo "[错误] 未选择虚拟机"; exit 1; }
[ -n "$PT_DISKFILE" ] || { echo "[错误] 未选择镜像文件"; exit 1; }
[ -n "$PT_STORAGE" ] || { echo "[错误] 未选择导入存储"; exit 1; }
# 源镜像位置：value 形如 <storage>::<name>，默认导入目录 storage 为 __import__
SRC_ITEM="$PT_DISKFILE"
case "$SRC_ITEM" in
 *::*) S_SRC_STORAGE="${SRC_ITEM%%::*}"; S_SRC_NAME="${SRC_ITEM##*::}" ;;
 *)    S_SRC_STORAGE="__import__"; S_SRC_NAME="$SRC_ITEM" ;;
esac
if [ "$S_SRC_STORAGE" = "__import__" ]; then
 SRC_ROOT=$(pw_storage_root pvewebtools)
 [ -n "$SRC_ROOT" ] && SRC="$SRC_ROOT/disk-images/$S_SRC_NAME" || SRC="/var/lib/vz/disk-images/$S_SRC_NAME"
else
 S_ROOT=$(pw_storage_root "$S_SRC_STORAGE")
 [ -n "$S_ROOT" ] || { echo "[错误] 无法解析存储 $S_SRC_STORAGE 的根路径"; exit 1; }
 # 磁盘镜像（disk_upload 数据源）在非 pvewebtools 存储下落在 import/ 目录（PVE import 内容类型），
 # 与 __import__ 分支（pvewebtools 的 disk-images/）语义一致，均指向「普通磁盘镜像」。
 SRC="$S_ROOT/import/$S_SRC_NAME"
fi
[ -f "$SRC" ] || { echo "[错误] 导入文件不存在: $SRC"; exit 1; }

# 选项 B：压缩包（.xz/.gz/.zst）原样保留在导入目录，此处现场解压后再导入
DEC=""
_clean_dec() { [ -n "$DEC" ] && rm -f "$DEC" 2>/dev/null; }
trap _clean_dec EXIT
case "$SRC" in
 *.xz)  DEC="${SRC%.*}.dec.tmp"; echo "[解压] xz -dc $SRC"; xz -dc "$SRC" > "$DEC" ;;
 *.gz)  DEC="${SRC%.*}.dec.tmp"; echo "[解压] gzip -dc $SRC"; gzip -dc "$SRC" > "$DEC" ;;
 *.zst) DEC="${SRC%.*}.dec.tmp"; echo "[解压] zstd -dc $SRC"; zstd -dc "$SRC" > "$DEC" ;;
esac
if [ -n "$DEC" ]; then
 [ -s "$DEC" ] || { echo "[错误] 解压失败或文件为空: $SRC"; exit 1; }
 echo "[解压] 已解压为临时文件: $DEC"
 SRC="$DEC"
fi

backup_file "/etc/pve/qemu-server/${PT_VMID}.conf"
echo "===== 导入 $PT_DISKFILE 到 VM $PT_VMID（存储 $PT_STORAGE）====="
OUT=$(qm importdisk "$PT_VMID" "$SRC" "$PT_STORAGE" 2>&1)
echo "$OUT"
# 解析 importdisk 输出的 volid（形如 unused0:local:100/vm-100-disk-0.raw）
VOLID_LINE=$(echo "$OUT" | grep -oE "imported disk as '[^']+'" | head -1 | sed "s/imported disk as '//; s/'//")
[ -n "$VOLID_LINE" ] || { echo "[错误] 未能从导入输出解析磁盘 volid"; exit 1; }
DISK_VOLID=${VOLID_LINE#*:}   # 去掉 unusedN: 前缀，剩 local:100/...

if [ "$PT_BUS" = "auto" ]; then
 SLOT=""
 for base in scsi sata virtio ide; do
   for i in 0 1 2 3 4 5 6 7; do
     s="${base}${i}"
     if ! qm config "$PT_VMID" 2>/dev/null | grep -qE "^${s}:"; then SLOT="$s"; break 2; fi
   done
 done
 [ -n "$SLOT" ] || { echo "[错误] 未找到空闲总线槽位"; exit 1; }
else
 SLOT="$PT_BUS"
fi

echo "===== 挂载到 $SLOT ====="
run "挂载磁盘失败" qm set "$PT_VMID" -"$SLOT" "$DISK_VOLID"
echo "[完成] 已导入并挂载为 $SLOT: $DISK_VOLID"
echo "[提示] 若源镜像来自其他虚拟化平台（VMware/Hyper-V/VirtualBox），开机前请确认已装 virtio 驱动/Cloud-init"
exit 0
