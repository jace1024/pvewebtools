
DIR=/etc/pvewebtools/backup/grub
GRUB=/etc/default/grub
mkdir -p "$DIR"
case "$PT_ACTION" in
view)
 echo "---- 当前启动参数（GRUB_CMDLINE_LINUX_DEFAULT）----"
 LINE=$(grep '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB" || true)
 echo "${LINE:-未设置 GRUB_CMDLINE_LINUX_DEFAULT}"
 ;;
list)
 echo "---- 现有 GRUB 备份 ----"
 if [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then ls -lh "$DIR" | tail -n +2; else echo "（无备份）"; fi
 ;;
backup)
 NOTE="${PT_NOTE:-手动备份}"
 F="$DIR/$(date +%Y%m%d_%H%M%S)_${NOTE}.bak"
 cp "$GRUB" "$F"
 echo "[完成] 已备份到 $F"
 echo; echo "---- 现有备份 ----"; ls -lh "$DIR" | tail -n +2
 ;;
add|remove)
 P="$PT_PARAM"
 [ -n "$P" ] || { echo "[错误] 请填写启动参数（如 quiet 或 mitigations=off）"; exit 1; }
 KEY="${P%%=*}"
 LINE=$(grep '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB" || true)
 [ -n "$LINE" ] || { echo "[错误] $GRUB 中没有 GRUB_CMDLINE_LINUX_DEFAULT 行"; exit 1; }
 # 取出引号内的现有参数串，逐词处理避免 sed 转义坑
 CUR=${LINE#GRUB_CMDLINE_LINUX_DEFAULT=}
 CUR=${CUR%\"}; CUR=${CUR#\"}
 if [ "$PT_ACTION" = "add" ]; then
   for w in $CUR; do
     case "$w" in "$KEY"|"$KEY="*) echo "[错误] 参数 $KEY 已存在（$w），请先移除旧值"; exit 1;; esac
   done
   NEWPARAMS="${CUR:+$CUR }$P"
   echo "[添加] $P"
 else
   FOUND=0; NEWPARAMS=""
   for w in $CUR; do
     case "$w" in
       "$KEY"|"$KEY="*) echo "[移除] $w"; FOUND=1;;
       *) NEWPARAMS="${NEWPARAMS:+$NEWPARAMS }$w";;
     esac
   done
   [ "$FOUND" = 1 ] || { echo "[错误] 未找到参数 $KEY：$CUR"; exit 1; }
 fi
 backup_file "$GRUB"
 NEWLINE="GRUB_CMDLINE_LINUX_DEFAULT=\"$NEWPARAMS\""
 TMP=$(pw_job_mktemp_file)
 awk -v line="$NEWLINE" '/^GRUB_CMDLINE_LINUX_DEFAULT=/{print line; next}{print}' "$GRUB" > "$TMP"
 run "参数写入失败" cp "$TMP" "$GRUB"
 rm -f "$TMP"
 run "写入未生效" grep -qF "$NEWLINE" "$GRUB"
 run "update-grub 执行失败" update-grub
 echo "[完成] 启动参数已更新，重启后生效："
 grep '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB"
 ;;
restore)
F="$DIR/$PT_BACKUP_NAME"
restore_from_backup "$GRUB" "$F"
run "update-grub 执行失败" update-grub
echo "[完成] GRUB 已从 $PT_BACKUP_NAME 恢复，重启后生效"
;;
*)
 echo "[错误] 未知操作: $PT_ACTION"; exit 1
 ;;
esac
