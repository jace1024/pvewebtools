
if [ ! -d /sys/firmware/efi ]; then
 echo "[提示] 当前系统为 Legacy BIOS 引导，无需维护 EFI 分区"
 exit 0
fi
command -v proxmox-boot-tool >/dev/null 2>&1 || { echo "[错误] 未找到 proxmox-boot-tool"; exit 1; }
if [ "$PT_ACTION" = "sync" ]; then
 echo "[1/2] 刷新 EFI 分区内核引导项 ..."
 run "EFI 引导项刷新失败" proxmox-boot-tool refresh
 echo "[2/2] 当前 EFI 分区内核引导项:"
 proxmox-boot-tool kernel list
 echo "[完成] EFI 引导项已同步，重启后可引导新内核"
elif [ "$PT_ACTION" = "fix" ]; then
 REG=/etc/kernel/proxmox-boot-uuids
 if [ -s "$REG" ]; then
   echo "[提示] ESP 注册正常（$REG 已有登记），无需修复："
   cat "$REG"
   exit 0
 fi
 # 探测 vfat EFI 分区：blkid -o device 拿设备名，再逐个取 UUID
 UUIDS=""
 for dev in $(blkid -t TYPE=vfat -o device 2>/dev/null); do
   U=$(blkid -s UUID -o value "$dev" || true)
   if [ -n "$U" ]; then UUIDS="${UUIDS}$U"$'\n'; echo "[发现] EFI 分区 $dev -> $U"; fi
 done
 [ -n "$UUIDS" ] || { echo "[错误] 未找到 vfat EFI 分区，请确认 EFI 分区完好后再修复"; exit 1; }
 backup_file "$REG"   # 文件不存在时自动跳过
 printf '%s' "$UUIDS" > "$REG"
 run "登记写入失败" grep -qE '[0-9A-Fa-f-]' "$REG"
 echo "[1/3] 已写入 ESP 登记:"
 cat "$REG"
 echo "[2/3] 执行 refresh 验证 ..."
 run "refresh 失败，请确认 EFI 分区完好" proxmox-boot-tool refresh
 echo "[3/3] 当前 EFI 分区内核引导项:"
 proxmox-boot-tool kernel list
 echo "[完成] ESP 注册已修复，以后安装新内核会自动同步到 EFI 分区"
else
 echo "---- 启动模式: EFI ----"
 echo; echo "---- EFI 分区状态 ----"
 proxmox-boot-tool status || true
 echo; echo "---- EFI 分区内核引导项 ----"
 proxmox-boot-tool kernel list
fi
