
if [ "$PT_ACTION" = "deep" ]; then
 echo "---- 系统可见磁盘 SMART ----"
 for d in $(lsblk -dn -o NAME,TYPE | awk '$2=="disk" && $1 !~ /^(nbd|loop|ram|zram|dm-)/ {print $1}'); do
   echo "== /dev/$d =="
   HH=$(smartctl -H /dev/$d 2>/dev/null | { grep -iE 'result|assessment' || true; } | head -1)
   if [ -n "$HH" ]; then
     echo "  $HH"
     smartctl -A /dev/$d 2>/dev/null | { grep -iE 'Reallocated_Sector_Ct|Current_Pending_Sector|Offline_Uncorrectable|Power_On_Hours|Wear_Leveling|Percent_Life|Media_Wearout|Media_Unit' || true; } | sed 's/^/  /' || true
   else
     echo "  SMART 无法直读（RAID 卡后的盘见下方 RAID 卡段落）"
   fi
 done
 if command -v zpool >/dev/null 2>&1; then
   echo; echo "---- ZFS 池状态 ----"
   POOLS=$(zpool list -H -o name 2>/dev/null || true)
   if [ -n "$POOLS" ]; then zpool status 2>/dev/null || true; else echo "(无 ZFS 池)"; fi
 fi
 echo; echo "---- RAID 卡后物理盘 SMART（megaraid 枚举）----"
 if [ -d /sys/module/megaraid_sas ]; then
   CTRL=$(lsblk -dn -o NAME,TYPE | awk '$2=="disk" && $1 ~ /^sd/ {print "/dev/"$1; exit}')
   HIT=0; MISS=0; i=0
   while [ -n "$CTRL" ] && [ $i -lt 16 ]; do
     OUT=$(smartctl -d "megaraid,$i" -H "$CTRL" 2>/dev/null || true)
     H=$(printf '%s\n' "$OUT" | { grep -iE 'overall-health|Health Status' || true; } | head -1)
     if [ -n "$H" ]; then
       MISS=0; HIT=$((HIT+1)); echo "盘位 $i: $H"
       smartctl -d "megaraid,$i" -A "$CTRL" 2>/dev/null | { grep -iE 'Reallocated|Pending|Power_On|defect|Temperature' || true; } | sed 's/^/  /' || true
     else
       MISS=$((MISS+1))
       if [ $MISS -ge 4 ]; then break; fi  # 连续 4 个空盘位即停，避免无效探测
     fi
     i=$((i+1))
   done
   if [ $HIT -eq 0 ]; then echo "  (未探测到 SMART 可读盘位)"; fi
 else
   echo "  (未检测到 MegaRAID 卡)"
 fi
 echo; echo "---- 文件系统水位（≥80% 预警）----"
 df -h --output=target,pcent,size,used -x tmpfs -x devtmpfs 2>/dev/null || df -h | head -15
 df --output=target,pcent -x tmpfs -x devtmpfs 2>/dev/null | awk 'NR>1{p=$2; gsub(/%/,"",p); if (p+0>=80) print "  ⚠ 水位偏高: "$1" ("$2")"}' || true
 exit 0
fi
echo "---- PVE 存储池 ----"; pvesm status 2>/dev/null || df -h | head -10
echo; echo "---- LVM 逻辑卷 ----"; lvs 2>/dev/null || echo "(无 LVM)"
