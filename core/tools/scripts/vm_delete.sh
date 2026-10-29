
VMIDS="$PT_VMID"   # 逗号分隔，如 100,101,102
[ -n "$VMIDS" ] || { echo "[错误] 请选择要删除的虚拟机/容器"; exit 1; }

# 删除单个虚机/容器；成功返回 0，失败返回 1（错误原因已打印）
delete_guest() {
 local VMID="$1"
 local GTYPE="" NAME="" STATUS="" TYPE=""
 # 自动识别虚机/容器类型
 if qm status "$VMID" >/dev/null 2>&1; then GTYPE=qm; fi
 if [ -z "$GTYPE" ] && pct status "$VMID" >/dev/null 2>&1; then GTYPE=pct; fi
 if [ -z "$GTYPE" ]; then
   echo "  [跳过] 虚机/容器 $VMID 不存在（可能已被删除）"
   return 0
 fi
 # 显示待删除虚机/容器信息，便于二次核对
 if [ "$GTYPE" = "qm" ]; then
   NAME=$(qm config "$VMID" 2>/dev/null | awk -F': ' '/^name:/{print $2; exit}' || true)
   STATUS=$(qm status "$VMID" 2>/dev/null | awk '{print $2}' || true)
   TYPE="虚拟机 (VM)"
 else
   NAME=$(pct config "$VMID" 2>/dev/null | awk -F': ' '/^hostname:/{print $2; exit}' || true)
   STATUS=$(pct status "$VMID" 2>/dev/null | awk '{print $2}' || true)
   TYPE="容器 (LXC)"
 fi
 echo "类型: $TYPE | 名称: ${NAME:-（未命名）} | 状态: ${STATUS:-未知}"
 # 运行中的虚机/容器需先停止（qm stop / pct stop），否则删除会失败
 if [ "$STATUS" = "running" ]; then
   echo "[提示] 虚机/容器正在运行中，先停止..."
   if [ "$GTYPE" = "qm" ]; then
     qm stop "$VMID" || { echo "[错误] 停止虚拟机 $VMID 失败"; return 1; }
   else
     pct stop "$VMID" || { echo "[错误] 停止容器 $VMID 失败"; return 1; }
   fi
   echo "✓ 已停止"
 fi
 # 执行删除
 local DESTROY_CMD=("$GTYPE" destroy "$VMID")
 [ "$PT_PURGE" = "yes" ] && DESTROY_CMD+=(--purge)
 [ "$PT_DESTROY_UNREFERENCED" = "yes" ] && DESTROY_CMD+=(--destroy-unreferenced-disks 1)
 if ! "${DESTROY_CMD[@]}"; then
   echo "[错误] 删除 $TYPE $VMID 失败"; return 1
 fi
 # 写后验证：确认虚机/容器已不存在
 if $GTYPE status "$VMID" >/dev/null 2>&1; then
   echo "[错误] 删除未生效，虚机/容器 $VMID 仍存在"; return 1
 fi
 # 连带清理该虚机/容器的定时开关机任务：虚机/容器已不存在，残留块到点执行必然失败
 # （qm/pct start 找不到虚机/容器，只在日志里留一行报错），前端还会一直显示这条幽灵任务。
 # 与 vm_schedule 的 del 分支共用同一套标记块；本来就没任务时 remove_block 直接跳过。
 local CRON=/etc/cron.d/pvewebtools-schedule
 if [ -f "$CRON" ] && grep -q "# PVEWEBTOOLS BEGIN SCHEDULE_${VMID}" "$CRON"; then
   remove_block "$CRON" "SCHEDULE_${VMID}"
   systemctl restart cron || true
   echo "[清理] 已移除 $VMID 的定时开关机任务（虚机/容器已删除，留着到点只会报错）"
 fi
 # 连带清理本工具为该虚机生成的 Cloud-Init 片段：它是存储上的普通文件，qm destroy/--purge 不管它。
 # 留着不会影响本次删除，但同 VMID 下次被别的途径复用时会把上一台的 hostname/SSH 配置带过去。
 # 容器（pct）不涉及片段，pw_snippet_rm 找不到文件时静默跳过。
 pw_snippet_rm "$VMID"
 echo "[完成] $TYPE $VMID${NAME:+（$NAME）} 已删除"
 return 0
}

# 汇总待删数量后逐个执行
TOTAL=0
for VMID in $(echo "$VMIDS" | tr ',' ' '); do
 [ -n "$VMID" ] && TOTAL=$((TOTAL+1))
done
echo "===== 批量删除：共 $TOTAL 个虚机/容器 ====="
DONE=0; FAILED=0; IDX=0
for VMID in $(echo "$VMIDS" | tr ',' ' '); do
 [ -n "$VMID" ] || continue
 IDX=$((IDX+1))
 echo ""
 echo "──── [ $IDX/$TOTAL ] 删除 VMID $VMID ... ────"
 if delete_guest "$VMID"; then
   DONE=$((DONE+1))
 else
   FAILED=$((FAILED+1))
 fi
done

echo ""
echo "===== 批量删除汇总：成功 $DONE，失败 $FAILED ====="
[ "$PT_PURGE" = "yes" ] && echo "[提示] 勾选了删除磁盘的虚机/容器已连同删除（--purge）"
[ "$FAILED" -gt 0 ] && { echo "[错误] 有 $FAILED 个虚机/容器删除失败，请逐个检查"; exit 1; }
true
