
VMID="$PT_VMID"
[ -n "$VMID" ] || { echo "[错误] 请选择要克隆的源虚机/容器"; exit 1; }

# 自动识别虚机/容器类型
GTYPE=""
qm status "$VMID" >/dev/null 2>&1 && GTYPE=qm
[ -z "$GTYPE" ] && pct status "$VMID" >/dev/null 2>&1 && GTYPE=pct
[ -n "$GTYPE" ] || { echo "[错误] 源虚机/容器 $VMID 不存在"; exit 1; }

# 新 VMID：留空自动分配（从源 VMID+1 开始找空闲）
NEWID="$PT_NEWID"
if [ -z "$NEWID" ]; then
 NEWID=$((VMID + 1))
 while qm status "$NEWID" >/dev/null 2>&1 || pct status "$NEWID" >/dev/null 2>&1; do
   NEWID=$((NEWID + 1))
 done
fi
[ -n "$NEWID" ] && [ "$NEWID" != "$VMID" ] || { echo "[错误] 新 VMID 无效（不能与源相同）"; exit 1; }
# 检查新 VMID 是否已被占用：--unique 只重置 MAC/SMBIOS/磁盘序列号（qm 与 pct 均不自动分配 VMID），
# 故无论是否勾选「唯一」，VMID 冲突都必须报错（否则 qm/pct clone 会因 newid 已存在直接失败）。
qm status "$NEWID" >/dev/null 2>&1 || pct status "$NEWID" >/dev/null 2>&1 && { echo "[错误] VMID $NEWID 已存在，请换一个"; exit 1; }

# 新名称：留空用「原名-克隆方式前缀」，让克隆体与母体名称有区别
# 完整克隆用 fullclone，链接克隆用 linkclone
NEWNAME="$PT_NEWNAME"
if [ -z "$NEWNAME" ]; then
 if [ "$GTYPE" = "qm" ]; then
   SRCNAME=$(qm config "$VMID" 2>/dev/null | awk -F': ' '/^name:/{print $2; exit}' || true)
 else
   SRCNAME=$(pct config "$VMID" 2>/dev/null | awk -F': ' '/^hostname:/{print $2; exit}' || true)
 fi
 BASE="${SRCNAME:-$VMID}"
 if [ "$PT_FULL" = "yes" ]; then
   NEWNAME="${BASE}-fullclone"
 else
   NEWNAME="${BASE}-linkclone"
 fi
fi

# ===== 克隆前置处理：自动解决 RDM 直通盘与 unused 残留，让克隆能顺利完成 =====
# RDM 盘无法克隆：临时从源 VM 摘除（数据仍在物理盘上），克隆完成后自动加回源 VM；
# 克隆体本身不含 RDM 盘（物理数据盘本就不应随克隆复制）。unused 残留自动清理。
RDM_SLOTS=""
RDM_VOLIDS=""
RDM_SAVED=""
if [ "$GTYPE" = "qm" ]; then
 # 检测 unused 残留并自动清理
 # 注意：必须用 while read 逐行读取 + 结尾 || true 兜底。若用 for $(...) 会对结果做单词拆分，
 # 且 qm config 失败时（脚本开了 set -o pipefail + ERR trap）trap 的错误信息会混入命令替换输出，
 # 被拆分成语片（如 "命令执行失败"、"行"、"168):"、"cut"）从而误打印并误删无效键。
 while IFS= read -r UKEY; do
   [ -n "$UKEY" ] || continue
   echo "[处理] 自动清理 unused 残留 $UKEY ..."
   qm set "$VMID" -delete "$UKEY" 2>/dev/null && echo "  已移除 $UKEY" || echo "  [警告] 移除 $UKEY 失败"
 done < <(qm config "$VMID" 2>/dev/null | grep -E '^unused[0-9]+:' | cut -d: -f1 || true)

 # 检测 RDM 盘并临时摘除（记录槽位+volid，克隆后加回）
 RDM_DISKS=$(qm config "$VMID" 2>/dev/null | grep -E '^(scsi|virtio|sata|ide)[0-9]*:.*(/dev/|disk/by-id)' || true)
 if [ -n "$RDM_DISKS" ]; then
   VM_ST=$(qm status "$VMID" 2>/dev/null | awk '{print $2}')
   if [ "$VM_ST" = "running" ]; then
     echo "[错误] VM $VMID 正在运行，摘除 RDM 盘需先关机（qm shutdown $VMID）"
     exit 1
   fi
   echo "[处理] 检测到 RDM 直通盘（无法克隆），临时摘除，克隆后自动加回源 VM ..."
   while IFS= read -r line; do
     [ -n "$line" ] || continue
     RSLOT=$(echo "$line" | cut -d: -f1)
     RVOLID=$(echo "$line" | sed "s/^${RSLOT}: *//" | cut -d, -f1)
     qm set "$VMID" -delete "$RSLOT" 2>/dev/null && {
       echo "  已临时摘除 $RSLOT（$RVOLID）"
       RDM_SLOTS="${RDM_SLOTS}${RDM_SLOTS:+ }$RSLOT"
       RDM_VOLIDS="${RDM_VOLIDS}${RDM_VOLIDS:+ }$RVOLID"
     } || echo "  [警告] 摘除 $RSLOT 失败"
   done <<EOF
$RDM_DISKS
EOF
   # 兜底：无论克隆成败，退出时都把摘除的 RDM 盘加回源 VM，避免源 VM 数据盘丢失
   trap 'if [ -n "$RDM_SLOTS" ]; then i=0; for rs in $RDM_SLOTS; do i=$((i+1)); rv=$(echo "$RDM_VOLIDS" | awk -v n="$i" "{print \$n}"); [ -n "$rv" ] && qm set "$VMID" "-$rs" "$rv" 2>/dev/null; done; fi' EXIT
 fi
fi

echo "===== 克隆 ====="
echo "源: $VMID -> 新: $NEWID（$NEWNAME）"
echo "方式: $([ "$PT_FULL" = "yes" ] && echo "完整克隆" || echo "链接克隆")"
[ "$PT_UNIQUE" = "1" ] && echo "选项: 唯一（将重置 MAC / SMBIOS UUID+序列号 / 磁盘序列号）"

if [ "$GTYPE" = "qm" ]; then
 # qm clone 不支持 --unique（那是 pct 的参数），VM 的「唯一」改为克隆后重新生成 MAC 地址
 CLONE_CMD=(qm clone "$VMID" "$NEWID" --name "$NEWNAME")
 [ "$PT_FULL" = "yes" ] && CLONE_CMD+=(--full 1)
 run "克隆虚拟机失败" "${CLONE_CMD[@]}"
 if [ "$PT_UNIQUE" = "1" ]; then
   # 唯一标识：克隆后统一重置硬件指纹（MAC / SMBIOS UUID+序列号 / 磁盘序列号），见 COMMON_HELPERS 中的 sanitize_identity
   sanitize_identity "$NEWID"
 fi
else
 CLONE_CMD=(pct clone "$VMID" "$NEWID" --hostname "$NEWNAME")
 [ "$PT_FULL" = "yes" ] && CLONE_CMD+=(--full 1)
 [ "$PT_UNIQUE" = "1" ] && CLONE_CMD+=(--unique 1)
 run "克隆容器失败" "${CLONE_CMD[@]}"
fi

# 写后验证：确认新虚机/容器已创建
if [ "$GTYPE" = "qm" ]; then
 qm status "$NEWID" >/dev/null 2>&1 || { echo "[错误] 克隆未生效，新 VM $NEWID 不存在"; exit 1; }
else
 pct status "$NEWID" >/dev/null 2>&1 || { echo "[错误] 克隆未生效，新容器 $NEWID 不存在"; exit 1; }
fi

# 克隆完成后：把临时摘除的 RDM 盘加回源 VM（数据始终在物理盘上，仅配置移除/加回）
if [ "$GTYPE" = "qm" ] && [ -n "$RDM_SLOTS" ]; then
 echo "[处理] 克隆完成，把 RDM 盘加回源 VM $VMID ..."
 i=0
 for RSLOT in $RDM_SLOTS; do
   i=$((i+1))
   RVOLID=$(echo "$RDM_VOLIDS" | awk -v n="$i" '{print $n}')
   if [ -n "$RVOLID" ]; then
     qm set "$VMID" "-$RSLOT" "$RVOLID" 2>/dev/null && echo "  已加回 $RSLOT（$RVOLID）" || echo "  [警告] 加回 $RSLOT 失败，可手动执行: qm set $VMID -$RSLOT $RVOLID"
   fi
 done
 echo "[提示] 源 VM $VMID 已恢复 RDM 盘；克隆体 $NEWID 不含 RDM 盘（物理数据盘不随克隆复制）"
fi
# 部署后启动（模板克隆体可直接启动；源是模板时克隆体是普通 VM）
if [ "$PT_START" = "1" ]; then
 if [ "$GTYPE" = "qm" ]; then
   run "启动失败" qm start "$NEWID"
 else
   run "启动失败" pct start "$NEWID"
 fi
 echo "[完成] $NEWID 已启动"
fi
echo "[完成] 已克隆 $VMID -> $NEWID（$NEWNAME）"
[ "$PT_FULL" = "yes" ] && echo "[提示] 完整克隆：磁盘已独立复制，可独立启动"
true
