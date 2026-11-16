
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
  VMID="$PT_VMID"
# 自动识别虚机/容器类型：qm（虚拟机）优先，其次 pct（容器），两者的快照命令同构
GTYPE=""
qm status "$VMID" >/dev/null 2>&1 && GTYPE=qm
[ -z "$GTYPE" ] && pct status "$VMID" >/dev/null 2>&1 && GTYPE=pct
[ -n "$GTYPE" ] || { echo "[错误] 虚机/容器 $VMID 不存在"; exit 1; }
# 模板拦截：模板是只读的，无法拍快照/回滚/删除（前端已置灰，这里兜底）
if [ "$GTYPE" = "qm" ]; then
 CONF="/etc/pve/qemu-server/$VMID.conf"
else
 CONF="/etc/pve/lxc/$VMID.conf"
fi
if [ -f "$CONF" ] && grep -qE '^template:[[:space:]]*1' "$CONF"; then
 echo "[错误] 虚机/容器 $VMID 是模板，模板无法创建快照，请从模板克隆出正式虚机/容器后操作"
 exit 1
fi
# 集群失去仲裁时 /etc/pve 只读，创建/回滚/删除快照的写配置动作必然失败；先探一次给出可操作指引
pw_quorum_precheck

# 快照清单：PVE 9 的 listsnapshot 为树形格式（快照名前带 `-> 分支标记），
# 旧版本为空格/管道分隔表格。逐词扫描，跳过 "|" 与分支标记，取首个真实词，
# 再排除表头 NAME 与 current 伪快照行
snap_names() {
 $GTYPE listsnapshot "$VMID" 2>/dev/null | \
   awk '{ for (i=1; i<=NF; i++) { t=$i; if (t=="|" || t ~ /^[`|]-?>$/) continue; print t; break } }' | \
   grep -vx -e NAME -e current
}
SNAP_LIST=$(snap_names || true)
has_snap() { echo "$SNAP_LIST" | grep -qx "$1"; }

if [ "$PT_ACTION" = "rollback" ]; then
 [ -n "$PT_SNAPNAME" ] || { echo "[错误] 请从快照列表选择要回滚的快照"; exit 1; }
 has_snap "$PT_SNAPNAME" || { echo "[错误] 快照 $PT_SNAPNAME 不存在（可能已被删除）"; exit 1; }
 echo "[提示] 回滚会丢弃当前状态恢复到 $PT_SNAPNAME，运行中的虚机/容器将被重启"
 run "回滚失败" $GTYPE rollback "$VMID" "$PT_SNAPNAME"
 echo "[完成] $VMID 已回滚到快照 $PT_SNAPNAME"
 exit 0
fi

if [ "$PT_ACTION" = "del" ]; then
 [ -n "$PT_SNAPNAME" ] || { echo "[错误] 请从快照列表选择要删除的快照"; exit 1; }
 has_snap "$PT_SNAPNAME" || { echo "[错误] 快照 $PT_SNAPNAME 不存在（可能已被删除）"; exit 1; }
 run "删除快照失败" $GTYPE delsnapshot "$VMID" "$PT_SNAPNAME"
 if snap_names | grep -qx "$PT_SNAPNAME"; then
   echo "[错误] 删除未生效，快照仍存在"; exit 1
 fi
 echo "[完成] 已删除 $VMID 的快照 $PT_SNAPNAME"
 exit 0
fi

# 默认动作：创建快照（take）
# 快照名：留空时自动生成；PVE 配置 ID 必须以字母开头，故加 snap- 前缀
SNAPNAME="$PT_SNAPNAME"
if [ -z "$SNAPNAME" ]; then
 NAME=$($GTYPE config "$VMID" 2>/dev/null | awk -F': ' '/^(name|hostname):/{print $2; exit}' || true)
 # VM 名截断到 15 字符，避免长名称导致快照名超长
 NAME=$(echo "$NAME" | cut -c1-15)
 TS=$(date +%Y%m%d-%H%M%S)
 SNAPNAME="snap-${VMID}-${NAME}-${TS}"
 # 快照名（PVE 配置 ID）只允许字母数字下划线连字符，点号等特殊字符一律转为连字符
 SNAPNAME=$(echo "$SNAPNAME" | tr -cs 'A-Za-z0-9_-' '-' | sed 's/^-*//;s/-*$//')
 # 最终截断到 40 字符（PVE 快照名上限），并清理尾部连字符
 SNAPNAME=$(echo "$SNAPNAME" | cut -c1-40 | sed 's/-*$//')
fi
# 守卫：快照名必须以字母开头（PVE 配置 ID 规范）
case "$SNAPNAME" in
 [A-Za-z]*) ;;
 *) echo "[错误] 快照名必须以字母开头（PVE 配置 ID 规范），当前: $SNAPNAME"; exit 1 ;;
esac
echo "[提示] 快照名: $SNAPNAME"
has_snap "$SNAPNAME" && { echo "[错误] 快照 $SNAPNAME 已存在，请换个名字"; exit 1; }
# ===== 创建快照前置检测：提前给出失败原因，而不是等 PVE 报生硬错误 =====
if [ "$GTYPE" = "qm" ]; then
 # 检测 1：hostpci（PCIe 直通 / vGPU）——两者都无法快照，但原因不同，措辞分开避免误导
 CFG_HP=$(qm config "$VMID" 2>/dev/null | grep -E '^hostpci[0-9]+:' || true)
 if [ -n "$CFG_HP" ]; then
   case "$CFG_HP" in
     *mdev=*) HP_KIND="vGPU（hostpci 的 mdev=）"
              HP_WHY="QEMU 无法保存 mdev 设备的内存状态";;
     *)       HP_KIND="PCIe 直通设备（hostpci）"
              HP_WHY="QEMU 无法保存 PCIe 直通设备（如显卡/USB 控制器）的内存状态";;
   esac
   echo "[错误] VM $VMID 存在 $HP_KIND，带直通/vGPU 的虚拟机无法创建快照"
   echo "[原因] $HP_WHY，这是 PVE 的固有限制"
   echo "[建议] 要快照需先移除直通/vGPU（qm set $VMID -delete hostpci0 ...），或用 vzdump 离线备份代替快照"
   exit 1
 fi
 # 检测 2：raw 磁盘——raw 格式不支持快照（目录存储下），需转 qcow2
 RAW_DISKS=$(qm config "$VMID" 2>/dev/null | grep -E '^(scsi|virtio|sata|ide|efidisk)[0-9]*:.*\.raw' || true)
 if [ -n "$RAW_DISKS" ]; then
   echo "[错误] VM $VMID 存在 raw 格式磁盘，raw 磁盘不支持快照"
   echo "$RAW_DISKS" | sed 's/^/    磁盘: /'
   echo "[原因] raw 是裸格式，PVE 无法对它做增量快照（目录存储下）；qcow2 才支持快照"
   echo "[建议] 用「存储与磁盘 → 虚机磁盘格式转换」把 raw 转成 qcow2 后再快照"
   exit 1
 fi
 run "创建快照失败" $GTYPE snapshot "$VMID" "$SNAPNAME" --vmstate 1
else
 run "创建快照失败" $GTYPE snapshot "$VMID" "$SNAPNAME"
fi
snap_names | grep -qx "$SNAPNAME" || { echo "[错误] 快照创建未生效"; exit 1; }
# 写后验证：含内存的快照会生成 vm-<vmid>-state-<snapname>.raw 文件，据此判断内存是否真的保存
if [ "$GTYPE" = "qm" ]; then
 if ls /var/lib/vz/images/"$VMID"/vm-"$VMID"-state-"$SNAPNAME".raw >/dev/null 2>&1; then
   echo "[完成] 已为 $VMID 创建快照 $SNAPNAME（已包含内存状态）"
 else
   echo "[完成] 已为 $VMID 创建快照 $SNAPNAME"
   echo "[提示] 快照未包含内存状态：VM 若在运行中请确认 QEMU 支持保存内存（嵌套虚拟化/VMware 环境可能不支持）"
 fi
else
 echo "[完成] 已为 $VMID 创建快照 $SNAPNAME（容器快照默认包含运行状态）"
fi
  )
done
echo "===== 全部 $N 台虚机处理完成 ====="
