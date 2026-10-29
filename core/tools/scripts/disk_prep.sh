
# 本工具操作不可逆（wipefs 抹除 / 重新分区格式化）。
# 默认 ERR trap 只打印不中断：一旦前面的保护检查因命令异常而失效，脚本会继续往下跑把盘抹掉。
# 因此这里覆盖为「立即中止」，任何未预期的命令失败都直接停止，宁可不执行也不能误抹盘。
trap 'echo "[错误] 命令执行失败 (退出码 $?, 行 $LINENO): $BASH_COMMAND"; echo "[中止] 该操作不可逆，已停止执行以确保安全"; exit 1' ERR

DEV="/dev/$PT_DISK"
[ -b "$DEV" ] || { echo "[错误] 磁盘不存在: $DEV"; exit 1; }
# ===== 系统盘保护：宿主占用的盘（有挂载点 / LVM / ZFS / RAID 成员）一律禁止分区格式化 =====
# 1. 该盘及其任一分区是否挂载中（覆盖 /、/boot、/var/lib/vz、/mnt 等所有挂载点）
if grep -qE "^${DEV}[0-9p]*[[:space:]]" /proc/mounts; then
 echo "[错误] 该磁盘（或其分区）正在挂载中，请先卸载"; exit 1
fi
# 2. LVM 物理卷成员：仅当属于「PVE 在用卷组」时才禁止。
#    其他系统（如飞牛）的存储池在 PVE 眼里同样是 LVM 成员，但 PVE 并未使用它，不该拦截
# 注意：脚本开了 pipefail + ERR trap，查询类命令「无匹配/不存在」也返回非零并触发 trap，
# 所以所有查询都必须 || true 兜底，不能依赖 trap 兜底（它只打印不中断）
DEV_VG=$(pvs --noheadings -o pv_name,vg_name 2>/dev/null | awk -v d="$DEV" '$1==d {print $2; exit}' || true)
if [ -n "$DEV_VG" ]; then
 # PVE 在用卷组 = 默认卷组 pve + 含有已挂载逻辑卷的卷组
 PVE_VGS=" pve"
 MOUNTED_VGS=$(lvs --noheadings -o vg_name,lv_path 2>/dev/null | while read -r vg lvpath; do
   [ -n "$lvpath" ] || continue
   # 用 if 而非 &&：grep 无匹配时 && 短路会让整行返回非零并触发 ERR trap
   if grep -q "^$lvpath " /proc/mounts 2>/dev/null; then echo " $vg"; fi
 done || true)
 PVE_VGS="$PVE_VGS$MOUNTED_VGS "
 case "$PVE_VGS" in
   *" $DEV_VG "*) echo "[错误] 该磁盘属于 PVE 在用卷组 $DEV_VG，禁止操作"; exit 1;;
 esac
fi
# 3. ZFS 成员盘（可能是 rpool 等系统池底层盘；zpool 默认输出短设备名，用 PT_DISK 匹配）
if command -v zpool >/dev/null 2>&1 && zpool status 2>/dev/null | grep -qE "(^|[[:space:]/])${PT_DISK}([0-9p]*[[:space:]]|$)"; then
 echo "[错误] 该磁盘是 ZFS 池成员（可能是系统盘），禁止操作"; exit 1
fi
# 4. 根分区所在盘（findmnt 解析根挂载源，覆盖 LVM/md/直挂设备）
ROOTSRC=$(findmnt -n -o SOURCE / 2>/dev/null | sed 's/\[.*\]//' || true)
case "$ROOTSRC" in "$DEV"*) echo "[错误] 该磁盘承载根分区，禁止操作"; exit 1;; esac
# 4b. /boot 与 /boot/efi 所在盘：多盘安装时 EFI 分区可能与根盘不在同一块，
#     格式化它会导致宿主机无法启动；LVM/ZFS 形态无法按设备名比对，由上面 2、3 项兜底
for MP in /boot /boot/efi; do
 SRC=$(findmnt -n -o SOURCE "$MP" 2>/dev/null | sed 's/\[.*\]//' || true)
 [ -n "$SRC" ] || continue
 case "$SRC" in "$DEV"*) echo "[错误] 该磁盘承载 $MP（$SRC），禁止操作"; exit 1;; esac
done
# 5. VM 直通/使用中
if grep -qs "$DEV" /etc/pve/qemu-server/*.conf; then
 echo "[错误] 疑似有虚拟机直通/使用该磁盘，请先移除"; exit 1
fi
if [ "$PT_ACTION" = "wipe" ]; then
 WIPE_MODE="${PT_WIPE_MODE:-quick}"
 case "$WIPE_MODE" in
   zero)
     # 彻底覆写：把整盘每个字节写成 0，数据不可恢复；大容量盘耗时较长（1TB 约十几分钟起）
     # status=progress 用 \r 原地刷新进度，不会刷出海量日志行
     echo "[擦除] 彻底覆写：整盘写入 0（不可恢复），耗时较长，请勿中断 ..."
     run "dd 覆写失败" dd if=/dev/zero of="$DEV" bs=1M status=progress
     echo; echo "[完成] $DEV 已全盘覆写为 0，数据不可恢复"
     ;;
   discard)
     # SSD 安全擦除：先清签名再对整盘下发 discard/TRIM，秒级完成且不可恢复
     # 机械盘或部分阵列卡不支持 discard，失败时提示改用覆写
     echo "[擦除] SSD 安全擦除：先清签名，再对整盘下发 TRIM ..."
     run "wipefs 失败" wipefs -a "$DEV"
     run "blkdiscard 失败（该盘可能不支持 discard，如机械盘/阵列卡，请改用「彻底覆写」）" blkdiscard "$DEV"
     echo "[完成] $DEV 已安全擦除（全盘 TRIM），数据不可恢复"
     ;;
   *)
     run "wipefs 失败" wipefs -a "$DEV"
     echo "[完成] $DEV 上的分区表与文件系统签名已全部抹除，现为空盘"
     echo "[提示] 快速清除只擦签名，数据区未被覆写，理论上仍可用工具找回；要彻底清除请选「彻底覆写」"
     ;;
 esac
 exit 0
fi
if [ "$PT_ACTION" = "format" ]; then
 [ -n "$PT_FSTYPE" ] || { echo "[错误] 请选择文件系统"; exit 1; }
 [ -n "$PT_PARTS" ] || PT_PARTS=1
 # 先确认 mkfs 工具可用再动盘：PVE 默认只带 mkfs.ext4，其余需额外安装。
 # 刻意放在抹除签名之前——否则会出现「盘已经清了，才发现工具没装」的进退两难。
 # 缺失时直接自动安装；仅当装不上（无网络等）才报错并给出手动命令
 MKFS_BIN="mkfs.$PT_FSTYPE"
 if ! command -v "$MKFS_BIN" >/dev/null 2>&1; then
   case "$PT_FSTYPE" in
     xfs)   PKG="xfsprogs";;
     btrfs) PKG="btrfs-progs";;
     exfat) PKG="exfatprogs";;
     *)     PKG="";;
   esac
   [ -n "$PKG" ] || { echo "[错误] 系统缺少 $MKFS_BIN，且无对应可自动安装的软件包"; exit 1; }
   echo "[安装] 未检测到 $MKFS_BIN，正在自动安装 $PKG ..."
   if ! apt install -y "$PKG"; then
     # 软件索引过期是最常见的失败原因，更新索引后重试一次
     echo "[提示] 首次安装失败，先更新软件索引再重试 ..."
     run "apt update 失败（无网络？可手动执行: apt install -y $PKG）" apt update
     run "$PKG 安装失败（可手动执行: apt install -y $PKG）" apt install -y "$PKG"
   fi
   command -v "$MKFS_BIN" >/dev/null 2>&1 || { echo "[错误] 安装后仍未找到 $MKFS_BIN"; exit 1; }
   echo "[完成] $PKG 安装完成，继续格式化"
 fi
 echo "[1/3] 抹除旧签名"; run "wipefs 失败" wipefs -a "$DEV"
 echo "[2/3] 建立 GPT 分区表（$PT_PARTS 个等分分区）"
 TOTAL_MB=$(( $(blockdev --getsize64 "$DEV") / 1048576 ))
 PER_MB=$(( TOTAL_MB / PT_PARTS ))
 run "parted 建分区表失败" parted -s "$DEV" mklabel gpt
 i=1; START=1
 while [ $i -le "$PT_PARTS" ]; do
   if [ $i -eq "$PT_PARTS" ]; then END_MB=$((TOTAL_MB - 1)); else END_MB=$((START + PER_MB - 1)); fi
   run "parted 分区失败（第 $i 分区）" parted -s "$DEV" mkpart primary "${START}MiB" "${END_MB}MiB"
   START=$((END_MB + 1)); i=$((i + 1))
 done
 partprobe "$DEV" 2>/dev/null || true
 sleep 1
 echo "[3/3] 格式化全部分区为 $PT_FSTYPE"
 FOUND=0
 for PART in $(lsblk -ln -o NAME,TYPE "$DEV" | awk '$2=="part"{print "/dev/"$1}'); do
   FOUND=1
   case "$PT_FSTYPE" in
     xfs)   run "mkfs.xfs 失败（$PART）" mkfs.xfs -f "$PART";;
     btrfs) run "mkfs.btrfs 失败（$PART）" mkfs.btrfs -f "$PART";;
     exfat) run "mkfs.exfat 失败（$PART）" mkfs.exfat "$PART";;
     *)     run "mkfs.ext4 失败（$PART）" mkfs.ext4 -F "$PART";;
   esac
   echo "  ✓ $PART 已格式化"
 done
 [ $FOUND -eq 1 ] || { echo "[错误] 分区创建后未找到分区设备"; exit 1; }
 echo "[完成] $DEV 分区格式化完毕（$PT_PARTS 个分区，$PT_FSTYPE）"
 echo "[提示] 如需给 PVE 用：先挂载分区（如 /mnt/data 并写入 fstab），再到 WebUI: 数据中心-存储-添加-目录"
 exit 0
fi
echo "[错误] 未知操作: $PT_ACTION"; exit 1
