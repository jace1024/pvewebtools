
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
  ACTION="$PT_ACTION"

# 销毁残留的 VM/容器（清理还原失败留下的半成品），$1=VMID，带 --purge 一并删除其磁盘
cleanup_guest() {
 local id="$1"
 # 先清除可能残留的 immutable 属性（chattr -i），避免 qm destroy --purge 删磁盘失败
 find "/var/lib/vz/images/$id" -type f -name 'base-*' -exec chattr -i {} \; 2>/dev/null || true
 if qm status "$id" >/dev/null 2>&1; then
   qm destroy "$id" --purge 2>/dev/null && echo "[清理] 已销毁残留 VM $id" || {
     echo "[警告] qm destroy $id --purge 失败，尝试跳过磁盘销毁只删配置 ..."
     qm destroy "$id" --skip 2>/dev/null && echo "[清理] 已删除 VM $id 配置（磁盘残留可稍后手动清理）" || echo "[警告] 销毁残留 VM $id 失败"
   }
 fi
 if pct status "$id" >/dev/null 2>&1; then
   pct destroy "$id" --purge 2>/dev/null && echo "[清理] 已销毁残留容器 $id" || echo "[警告] 销毁残留容器 $id 失败"
 fi
}

if [ "$ACTION" = "backup" ]; then
 # ---- 备份 VM / 容器 / 模板 ----
 [ -n "$PT_VMID" ] || { echo "[错误] 请选择要备份的虚机/容器"; exit 1; }
 [ -n "$PT_STORAGE" ] || { echo "[错误] 请选择备份存储"; exit 1; }
 # 默认值兜底：压缩 zstd，模式停机（前端 select 未选时也有合理值）
 PT_COMPRESS="${PT_COMPRESS:-zstd}"
 PT_MODE="${PT_MODE:-stop}"
 # 自动判断虚机/容器类型（qm=VM，pct=容器）
 if qm status "$PT_VMID" >/dev/null 2>&1; then
   TOOL=qm; TYPE=VM
 elif pct status "$PT_VMID" >/dev/null 2>&1; then
   TOOL=pct; TYPE=容器
 else
   echo "[错误] 虚机/容器 $PT_VMID 不存在"; exit 1
 fi
 # 模板检测：模板是停止状态，快照/挂起模式不可用，自动改用停机模式
 IS_TEMPLATE=0
 if qm config "$PT_VMID" 2>/dev/null | grep -qE '^template:[[:space:]]*1'; then
   IS_TEMPLATE=1; TYPE="VM 模板"
 elif pct config "$PT_VMID" 2>/dev/null | grep -qE '^template:[[:space:]]*1'; then
   IS_TEMPLATE=1; TYPE="容器模板"
 fi
 if [ "$IS_TEMPLATE" = "1" ] && [ "$PT_MODE" != "stop" ]; then
   echo "[提示] 检测到目标为模板（停止状态），备份模式自动改为停机模式"
   PT_MODE="stop"
 fi
 echo "===== 备份 $TYPE $PT_VMID ====="
 echo "模式: $PT_MODE，压缩: $PT_COMPRESS，存储: $PT_STORAGE"
 # 备份前提示目标 VM 若在运行，停机模式会先关闭
 if [ "$PT_MODE" = "stop" ]; then
   echo "[提示] 停机模式会先关闭虚机/容器，备份完成后自动重启"
 fi
 # 备份实际落在所选存储的 dump 目录（vzdump --storage 决定），而非固定 local 的 /var/lib/vz/dump：
 # 用 pw_storage_root 解析所选存储根再拼 dump/（与 PVE backup 内容目录约定一致），解析失败回退 local 默认目录。
 DUMP_DIR="/var/lib/vz/dump"
 SROOT=$(pw_storage_root "$PT_STORAGE" 2>/dev/null || true)
 [ -n "$SROOT" ] && DUMP_DIR="$SROOT/dump"
 BEFORE=$(ls -1 "$DUMP_DIR" 2>/dev/null || true)
 # vzdump 参数：--mode 快照/挂起/停机，--compress 压缩，--storage 存储，--node 本机
 VZ_CMD=(vzdump "$PT_VMID" --mode "$PT_MODE" --compress "$PT_COMPRESS" --storage "$PT_STORAGE" --quiet 0)
 echo "执行: ${VZ_CMD[*]}"
 run "vzdump 备份失败，请查看上方报错" "${VZ_CMD[@]}"
 # 找出新生成的备份文件（vzdump 输出的是 .vma.* 或 .tar.*，排除 .log/.notes）
 NEW_BACKUP=$(ls -1t "$DUMP_DIR" 2>/dev/null | while read -r f; do
   echo "$BEFORE" | grep -qxF "$f" && continue
   case "$f" in *.log|*.notes) continue;; esac
   echo "$f"; break
 done)
 if [ -n "$PT_BACKUP_NAME" ] && [ -n "$NEW_BACKUP" ]; then
   # 自定义文件名：保留原扩展名（.vma.zst/.tar.gz 等），只替换主名；
   # 强制加 vzdump-qemu-/vzdump-lxc- 前缀（qmrestore/pct restore 靠文件名识别类型与压缩格式，
   # 自定义名缺前缀会报 "couldn't determine archive info"）
   EXT="${NEW_BACKUP##*.}"
   # 多级扩展名（.vma.zst / .tar.zst）需要取完整后缀
   case "$NEW_BACKUP" in
     *.vma.*) EXT="${NEW_BACKUP#*.}";;   # vzdump-qemu-100-xxx.vma.zst -> vma.zst
     *.tar.*) EXT="${NEW_BACKUP#*.}";;   # vzdump-lxc-100-xxx.tar.zst -> tar.zst
   esac
   PREFIX="vzdump-qemu-"
   [ "$TOOL" = "pct" ] && PREFIX="vzdump-lxc-"
   NAME="$PT_BACKUP_NAME"
   # 避免重复前缀
   case "$NAME" in
     vzdump-qemu-*|vzdump-lxc-*) ;;
     *) NAME="${PREFIX}${NAME}" ;;
   esac
   TARGET="$DUMP_DIR/$NAME.$EXT"
   if [ "$NEW_BACKUP" != "$(basename "$TARGET")" ]; then
     mv "$DUMP_DIR/$NEW_BACKUP" "$TARGET" 2>/dev/null && \
       echo "[完成] 备份已生成并重命名为: $(basename "$TARGET")" || \
       echo "[提示] 重命名失败，备份保留默认名: $NEW_BACKUP"
   fi
 else
   echo; echo "[完成] 备份已生成: $NEW_BACKUP"
 fi
 echo "存储位置: $DUMP_DIR"

elif [ "$ACTION" = "restore" ]; then
 # ---- 还原备份 ----
 [ -n "$PT_BACKUP_FILE" ] || { echo "[错误] 请选择要还原的备份文件"; exit 1; }
 # 还原目标存储默认 local（未选时兜底）
 PT_RESTORE_STORAGE="${PT_RESTORE_STORAGE:-local}"
 # 备份文件绝对路径：优先在 /var/lib/vz/dump 下找，找不到则用 backup_file 本身（可能是完整路径）
 BACKUP_PATH=""
 if [ -f "/var/lib/vz/dump/$PT_BACKUP_FILE" ]; then
   BACKUP_PATH="/var/lib/vz/dump/$PT_BACKUP_FILE"
 elif [ -f "$PT_BACKUP_FILE" ]; then
   BACKUP_PATH="$PT_BACKUP_FILE"
 else
   echo "[错误] 找不到备份文件: $PT_BACKUP_FILE"; exit 1
   fi

   # ===== 本机备份缓存 =====
   # 与建机共用同一套本机缓存（pw_cache_use 里含提示与「不可用才诊断」的口径）：
   # 备份常在共享存储上，首次还原时拉取到本机，之后本节点所有还原直读本地。
   _cres=$(pw_cache_use "$BACKUP_PATH" 备份)
   _cf=$(printf '%s\n' "$_cres" | sed -n 2p)
   [ -n "$_cf" ] && BACKUP_PATH="$_cf"

   # 从备份文件扩展名推断类型：.vma* = VM，.tar* = 容器（兼容自定义命名）
 BASE=$(basename "$BACKUP_PATH")
 RESTORE_TOOL=""
 case "$BASE" in
   *.vma|*.vma.*) RESTORE_TOOL=qmrestore; TYPE=VM ;;
   *.tar|*.tar.*) RESTORE_TOOL=pct; TYPE=容器 ;;
   *) echo "[错误] 无法识别的备份文件类型（应为 .vma 或 .tar 备份文件）: $BASE"; exit 1 ;;
 esac
 # 探测文件头魔数，识别是否被整体压缩（zstd/gzip/lzo）。
 # 背景：qmrestore/pct restore 靠文件名识别压缩格式，自定义命名的备份（缺少 vzdump-qemu-/vzdump-lxc- 前缀）
 # 会报 "couldn't determine archive info"。这里统一先解压成未压缩的标准归档，绕过文件名依赖。
 MAGIC=$(head -c 4 "$BACKUP_PATH" 2>/dev/null | od -An -tx1 | tr -d ' \n')
 RESTORE_SRC="$BACKUP_PATH"
 RESTORE_TMPDIR=""
 DECOMP=""
 case "$MAGIC" in
   28b52ffd*) DECOMP="zstd -d -c" ;;
   1f8b*)     DECOMP="gzip -dc" ;;
   894c5a4f*) DECOMP="lzop -dc" ;;
 esac
 if [ -n "$DECOMP" ]; then
   RESTORE_TMPDIR=$(pw_job_mktemp_dir)
   # 解压后的临时文件必须带规范扩展名（qmrestore/pct restore 靠文件名识别格式）：
   # VM → vzdump-qemu-restore.vma，容器 → vzdump-lxc-restore.tar
   if [ "$RESTORE_TOOL" = "qmrestore" ]; then
     RESTORE_SRC="$RESTORE_TMPDIR/vzdump-qemu-restore.vma"
   else
     RESTORE_SRC="$RESTORE_TMPDIR/vzdump-lxc-restore.tar"
   fi
   echo "[解压] 备份被整体压缩（$DECOMP），先解压成标准归档再还原 ..."
   $DECOMP "$BACKUP_PATH" > "$RESTORE_SRC" 2>/dev/null || { echo "[错误] 解压备份失败"; rm -rf "$RESTORE_TMPDIR"; exit 1; }
 fi
 # 目标 VMID 优先级：用户在虚机/容器框中填了 VMID → 用该值；否则从 vzdump 标准命名解析原 VMID；
 # 解析不到时自动寻找空闲 VMID。
 # 注意：自定义命名（如 vzdump-qemu-iStoreOS-24.10.8）中的数字不是 VMID，不能靠 grep 数字提取。
 TARGET_VMID="$PT_VMID"
 if [ -z "$TARGET_VMID" ]; then
   if echo "$BASE" | grep -qE '^vzdump-(qemu|lxc)-[0-9]+'; then
     TARGET_VMID=$(echo "$BASE" | sed -E 's/^vzdump-(qemu|lxc)-([0-9]+).*$/\2/')
   fi
 fi
 # 未取到 VMID：自动寻找空闲 ID（100-999 范围内第一个未被占用的）
 if [ -z "$TARGET_VMID" ]; then
   TARGET_VMID=100
   while qm status "$TARGET_VMID" >/dev/null 2>&1 || pct status "$TARGET_VMID" >/dev/null 2>&1; do
     TARGET_VMID=$((TARGET_VMID + 1))
     [ "$TARGET_VMID" -gt 999 ] && { echo "[错误] 100-999 范围内无空闲 VMID，请手动指定"; exit 1; }
   done
   echo "[提示] 未从备份文件名解析到原 VMID，自动分配空闲 ID: $TARGET_VMID"
 fi
 # 还原前预检：目标 VMID 是否已被占用
 if qm status "$TARGET_VMID" >/dev/null 2>&1 || pct status "$TARGET_VMID" >/dev/null 2>&1; then
   echo "[错误] 目标 VMID $TARGET_VMID 已被占用，无法还原"
   echo "[提示] 1. 在「虚机/容器」下拉中指定一个空闲 VMID"
   echo "       2. 或先删除/迁移现有的 VMID $TARGET_VMID"
   [ -n "$RESTORE_TMPDIR" ] && rm -rf "$RESTORE_TMPDIR"
   exit 1
 fi
 # 还原额外参数：「还原后启动」在还原成功后手动执行（可优雅跳过模板）
 # 「唯一」分两条路径：容器(pct) 走原生 --unique 重置 MAC（LXC 无 SMBIOS/磁盘序列号）；
 # 虚拟机(qm) 不在 qmrestore 阶段处理，改为还原后统一调用 sanitize_identity 重置 4 维度
 RESTORE_EXTRA=()
 [ "$PT_UNIQUE" = "1" ] && [ "$RESTORE_TOOL" = "pct" ] && RESTORE_EXTRA+=(--unique 1)
 echo "===== 还原 $TYPE 备份 ====="
 echo "备份文件: $BACKUP_PATH"
 echo "目标 VMID: $TARGET_VMID，存储: $PT_RESTORE_STORAGE"
 [ "$PT_UNIQUE" = "1" ] && echo "选项: 唯一（重置硬件指纹，避免与源重复）"
 [ "$PT_START" = "1" ] && echo "选项: 还原后启动"
 if [ "$RESTORE_TOOL" = "qmrestore" ]; then
   if qmrestore "$RESTORE_SRC" "$TARGET_VMID" --storage "$PT_RESTORE_STORAGE" "${RESTORE_EXTRA[@]}"; then
     :
   else
     echo "[清理] 还原失败，自动清理残留的半成品 VM $TARGET_VMID ..."
     cleanup_guest "$TARGET_VMID"
     [ -n "$RESTORE_TMPDIR" ] && rm -rf "$RESTORE_TMPDIR"
     echo "[错误] qmrestore 还原失败"
     exit 1
   fi
   [ -n "$RESTORE_TMPDIR" ] && rm -rf "$RESTORE_TMPDIR"
   # 备份什么还原什么：模板还原后仍是模板（template: 1），虚机/容器还原后仍是虚机/容器，不强制改标记
   IS_TEMPLATE=0
   qm config "$TARGET_VMID" 2>/dev/null | grep -qE '^template:[[:space:]]*1' && IS_TEMPLATE=1
   if [ "$IS_TEMPLATE" = "1" ]; then
     echo "[提示] 还原结果为模板（备份源即模板），保持模板状态不变"
   else
     # 非模板虚机/容器：若磁盘是只读 base（链接克隆残留），转换为独立可写磁盘
     BASE_DISKS=$(qm config "$TARGET_VMID" 2>/dev/null | grep -oE 'local:[0-9]+/base-[^,]+' || true)
     if [ -n "$BASE_DISKS" ]; then
       echo "[提示] 检测到只读 base 磁盘（链接克隆残留），转换为独立可写磁盘 ..."
       for vol in $BASE_DISKS; do
         STORE=$(echo "$vol" | cut -d: -f1)
         BASE_NAME=$(echo "$vol" | cut -d/ -f2)   # base-103-disk-0.qcow2
         NEW_NAME="${BASE_NAME#base-}"            # 103-disk-0.qcow2 → 去掉 base- 前缀
         SRC="/var/lib/vz/images/$TARGET_VMID/$BASE_NAME"
         DST="/var/lib/vz/images/$TARGET_VMID/$NEW_NAME"
         if [ -f "$SRC" ] && [ ! -f "$DST" ]; then
           echo "  转换磁盘: $BASE_NAME -> $NEW_NAME"
           run "磁盘转换失败" qemu-img convert -p -O qcow2 "$SRC" "$DST"
           chmod 644 "$DST"
           # 更新配置指向新磁盘
           run "更新磁盘配置失败" qm set "$TARGET_VMID" --scsi0 "$STORE:$TARGET_VMID/$NEW_NAME"
           # 清除 immutable 属性再删除原只读 base 磁盘（链接克隆常设 chattr +i 防误删）
           chattr -i "$SRC" 2>/dev/null
           if [ $? -ne 0 ]; then
             echo "[提示] 清除 immutable 属性失败（文件系统可能不支持），跳过删除，残留文件: $BASE_NAME"
           elif rm -f "$SRC" 2>/dev/null; then
             echo "  已替换为独立磁盘: $NEW_NAME"
           else
             echo "[提示] 旧 base 磁盘删除失败（残留文件: $BASE_NAME），不影响 VM 使用，可稍后手动清理"
           fi
         fi
       done
     fi
   fi
   # 还原「唯一」：虚拟机在还原后统一重置硬件指纹（MAC/SMBIOS/磁盘序列号 4 维度）
   [ "$PT_UNIQUE" = "1" ] && sanitize_identity "$TARGET_VMID"
 else
   if pct restore "$TARGET_VMID" "$RESTORE_SRC" --storage "$PT_RESTORE_STORAGE" "${RESTORE_EXTRA[@]}"; then
     :
   else
     echo "[清理] 还原失败，自动清理残留的半成品容器 $TARGET_VMID ..."
     cleanup_guest "$TARGET_VMID"
     [ -n "$RESTORE_TMPDIR" ] && rm -rf "$RESTORE_TMPDIR"
     echo "[错误] pct restore 还原失败"
     exit 1
   fi
   [ -n "$RESTORE_TMPDIR" ] && rm -rf "$RESTORE_TMPDIR"
 fi
 # VM 还原后重新生成 cloud-init 驱动，让 guest 首次启动时重新走 cloud-init（DHCP 重新获取 IP）
 if [ "$RESTORE_TOOL" = "qmrestore" ] && qm config "$TARGET_VMID" 2>/dev/null | grep -q 'ide2:.*cloudinit\|scsi1:.*cloudinit'; then
   echo "[提示] 检测到 VM 使用 cloud-init，重新生成 cloud-init 驱动以重新获取 IP ..."
   qm cloudinit update "$TARGET_VMID" 2>/dev/null && \
     echo "[完成] cloud-init 已重新生成，VM 启动后将重新初始化网络（DHCP 重新获取 IP）" || \
     echo "[提示] cloud-init 重新生成失败，可手动执行: qm cloudinit update $TARGET_VMID"
 fi
 # 清理备份残留的 ISO 光驱，避免 "volume 'xxx.iso' does not exist" 导致启动失败
 if [ "$RESTORE_TOOL" = "qmrestore" ]; then
   CDROM_LINES=$(qm config "$TARGET_VMID" 2>/dev/null | grep -E '^(ide|sata)[0-9]*:.*,media=cdrom' || true)
   if [ -n "$CDROM_LINES" ]; then
     echo "$CDROM_LINES" | while IFS= read -r line; do
       CDROM_KEY=$(echo "$line" | awk -F: '{print $1}')
       CDROM_VOL=$(echo "$line" | sed 's/^[^:]*:[[:space:]]*//' | cut -d, -f1)
       case "$CDROM_VOL" in
         *iso*|*:iso/*)
           echo "[清理] 移除残留光驱 $CDROM_KEY（$CDROM_VOL）..."
           qm set "$TARGET_VMID" --delete "$CDROM_KEY" 2>/dev/null && echo "  已移除 $CDROM_KEY" || true
           ;;
       esac
     done
   fi
 fi
 # 还原后启动（模板无法启动，自动跳过）
 if [ "$PT_START" = "1" ]; then
   if [ "$RESTORE_TOOL" = "qmrestore" ] && qm config "$TARGET_VMID" 2>/dev/null | grep -qE '^template:[[:space:]]*1'; then
     echo "[提示] 还原结果是模板，无法启动（模板为只读母本），已跳过启动"
   elif [ "$RESTORE_TOOL" = "qmrestore" ]; then
     run "启动失败" qm start "$TARGET_VMID"
     echo "[完成] VM $TARGET_VMID 已启动"
   else
     run "启动失败" pct start "$TARGET_VMID"
     echo "[完成] 容器 $TARGET_VMID 已启动"
   fi
 fi
 echo; echo "[完成] 还原成功，VMID: $TARGET_VMID"
 echo "[提示] 还原后请检查网络/磁盘配置，必要时手动调整"

else
 echo "[错误] 未知操作: $ACTION"; exit 1
fi
  )
done
echo "===== 全部 $N 台虚机处理完成 ====="
