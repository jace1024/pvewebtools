
DIR=/etc/pvewebtools/backup/network
IFACE=/etc/network/interfaces
mkdir -p "$DIR"
case "$PT_ACTION" in
view)
 echo "---- $IFACE ----"
 cat "$IFACE"
 ;;
list)
 echo "---- 现有网络配置备份 ----"
 if [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then ls -lh "$DIR" | tail -n +2; else echo "（无备份）"; fi
 ;;
backup)
 NOTE="${PT_NOTE:-手动备份}"
 F="$DIR/$(date +%Y%m%d_%H%M%S)_${NOTE}.bak"
 cp "$IFACE" "$F"
 echo "[完成] 已备份到 $F"
 echo; echo "---- 现有备份 ----"; ls -lh "$DIR" | tail -n +2
 ;;
restore)
F="$DIR/$PT_BACKUP_NAME"
restore_from_backup "$IFACE" "$F"
echo "[完成] 网络配置已从 $PT_BACKUP_NAME 恢复（尚未生效），请执行'应用配置'使其生效"
;;
apply)
 # ifreload -a 增量应用配置差异（ifupdown2）；无此命令时回退重启 networking 服务
 if command -v ifreload >/dev/null 2>&1; then
   run "ifreload 应用失败（若已断网请到物理控制台恢复或用'从备份恢复'）" ifreload -a
   echo "[完成] 网络配置已应用（ifreload -a）"
 else
   run "重启网络服务失败" systemctl restart networking
   echo "[完成] 已重启 networking 服务，配置已应用"
 fi
 echo; ip -br addr
 ;;
*)
 echo "[错误] 未知操作: $PT_ACTION"; exit 1
 ;;
esac
