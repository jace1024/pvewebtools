
CRON=/etc/cron.d/pvewebtools-schedule
SLOG=/var/log/pvewebtools-schedule.log   # 定时任务执行日志：cron 输出默认进邮件会静默丢失，落盘便于排查
touch "$CRON"
if [ "$PT_ACTION" = "del" ]; then
 remove_block "$CRON" "SCHEDULE_${PT_VMID}"
 systemctl restart cron
 echo "[完成] VMID $PT_VMID 的定时任务已删除，剩余内容:"
 cat "$CRON" 2>/dev/null || echo "(文件为空)"
 exit 0
fi
# 暂停/启用：注释/恢复块内 cron 行（到点不执行但仍保留配置）
if [ "$PT_ACTION" = "pause" ]; then
 sed -i "/# PVEWEBTOOLS BEGIN SCHEDULE_${PT_VMID}/,/# PVEWEBTOOLS END SCHEDULE_${PT_VMID}/ { /^#/! s/^/#/ }" "$CRON"
 systemctl restart cron
 echo "[完成] VMID $PT_VMID 的定时任务已暂停（到点不执行）"
 exit 0
fi
if [ "$PT_ACTION" = "resume" ]; then
 sed -i "/# PVEWEBTOOLS BEGIN SCHEDULE_${PT_VMID}/,/# PVEWEBTOOLS END SCHEDULE_${PT_VMID}/ { /^# PVEWEBTOOLS/! s/^#// }" "$CRON"
 systemctl restart cron
 echo "[完成] VMID $PT_VMID 的定时任务已启用"
 exit 0
fi
TOOL=$(command -v "$PT_GTYPE" || echo "/usr/sbin/$PT_GTYPE")
# 类型一致性提醒：所选虚机/容器类型与操作类型不符时给出警告（不阻断，跨节点列表可能探测不到）
# 用 pvesh 默认表格输出 + awk 取第 1/2 列（vmid/type），避开多行 JSON 解析；尾部 || true 防 ERR trap
GT=$(pvesh get /cluster/resources --type vm 2>/dev/null | awk -v id="$PT_VMID" '$1==id {print $2; exit}' || true)
if [ -n "$GT" ]; then
 EXPECT=$([ "$PT_GTYPE" = "qm" ] && echo qemu || echo lxc)
 [ "$GT" = "$EXPECT" ] || echo "[警告] 虚机/容器 $PT_VMID 实际类型为 $GT，与所选类型不符，请确认操作类型选对"
fi
# 时间格式守卫：前端是 time 控件（HH:MM）不会错，服务端仍拦截非法值
chk_time() {  # $1=名称 $2=HH:MM
 [ -z "$2" ] && return 0
 echo "$2" | grep -qE '^([01][0-9]|2[0-3]):[0-5][0-9]$' || { echo "[错误] $1时间不正确: $2"; exit 1; }
}
chk_time "开机" "$PT_START_TIME"
chk_time "关机" "$PT_STOP_TIME"
# 星期字段守卫：允许 * / 1-5 / 0,6（前端下拉限制，服务端兜底）
chk_days() {  # $1=名称 $2=星期
 [ -z "$2" ] && return 0
 echo "$2" | grep -qE '^(\*|1-5|0,6)$' || { echo "[错误] $1星期不正确: $2"; exit 1; }
}
chk_days "开机" "$PT_START_DAYS"
chk_days "关机" "$PT_STOP_DAYS"
# 环境自检：工具存在 + VMID 在本节点可查——cron 里执行失败是静默的，这里先把典型错误拦在写入前
run "未找到命令 $TOOL，请确认在正确节点执行" test -x "$TOOL"
if ! "$TOOL" status "$PT_VMID" >/dev/null 2>&1; then
 echo "[警告] $TOOL status $PT_VMID 在本节点查不到该虚机/容器：若虚机/容器在其他节点，定时任务需写在那个节点上"
fi
# 模板拦截：模板无法开机/关机，到点执行必失败（/etc/pve 是集群文件系统，任意节点可见全部配置）
CONF=/etc/pve/qemu-server/$PT_VMID.conf
if [ "$PT_GTYPE" = "pct" ]; then CONF=/etc/pve/lxc/$PT_VMID.conf; fi
if [ -f "$CONF" ] && grep -qE '^template:[[:space:]]*1' "$CONF"; then
 echo "[错误] 虚机/容器 $PT_VMID 是模板，模板无法开机/关机，定时任务写了也不会生效"
 echo "[提示] 请从该模板克隆出正式虚机/容器（WebUI 右键→克隆，或 qm clone），再为克隆出的 VMID 配置定时开关机"
 exit 1
fi
# cron 行尾部带日志重定向：执行时间 + 退出码落盘，到点没执行/执行报错都有迹可循
# \$ 让 date/$? 到 cron 运行时才求值；\% 防 crontab 把 % 当换行符
LOGCMD=">> $SLOG 2>&1; echo \"[$TOOL $PT_VMID] \$(date '+\%F \%T') rc=\$?\" >> $SLOG"
# 自愈守卫：到点执行时若虚机/容器已不存在（本工具外删除/配置丢失），自动移除本任务块并退出，
# 避免幽灵任务反复在日志里报错；start/stop 两条 cron 行共用同一块，删一次即整体移除，不会重复触发。
# 说明：PVE 集群中 /etc/pve 配置全局可见，虚机/容器仅迁走时 status 仍可查（exit 0），不会误删；配置真正消失才会触发。
GUARD="if ! $TOOL status $PT_VMID >/dev/null 2>&1; then sed -i \"/# PVEWEBTOOLS BEGIN SCHEDULE_${PT_VMID}/,/# PVEWEBTOOLS END SCHEDULE_${PT_VMID}/d\" $CRON; systemctl restart cron; echo \"[自愈] 虚机/容器 $PT_VMID 已不存在，自动移除定时任务 SCHEDULE_${PT_VMID}\" >> $SLOG; exit 0; fi; "
START_DAYS="${PT_START_DAYS:-*}"
STOP_DAYS="${PT_STOP_DAYS:-*}"
LINES=""
if [ -n "$PT_START_TIME" ]; then
 # cron 行：分 时 * * 星期 root 命令（第 5 字段为星期）
 LINES="${PT_START_TIME#*:} ${PT_START_TIME%%:*} * * $START_DAYS root $GUARD$TOOL start $PT_VMID $LOGCMD"
fi
if [ -n "$PT_STOP_TIME" ]; then
 ACTION=stop
 if [ "$PT_GTYPE" = "qm" ]; then ACTION=shutdown; fi
 if [ -n "$LINES" ]; then LINES="$LINES\n"; fi
 LINES="${LINES}${PT_STOP_TIME#*:} ${PT_STOP_TIME%%:*} * * $STOP_DAYS root $GUARD$TOOL $ACTION $PT_VMID $LOGCMD"
fi
[ -n "$LINES" ] || { echo "[错误] 开机/关机时间至少填一个"; exit 1; }
apply_block "$CRON" "SCHEDULE_${PT_VMID}" "$(printf '%b' "$LINES")"
systemctl restart cron
# 写后验证：任务块确实写入且 cron 服务在跑
run "定时任务写入未生效" grep -q "# PVEWEBTOOLS BEGIN SCHEDULE_${PT_VMID}" "$CRON"
run "cron 服务重启后未处于运行状态" systemctl is-active --quiet cron
echo "[完成] 定时任务已写入，当前内容:"
cat "$CRON"
echo "[诊断] 文件权限: $(ls -l "$CRON" | awk '{print $1, $3, $4}')（需 root 属主且非 777，否则 cron 会忽略该文件）"
echo "[诊断] cron 服务: 运行中，开机自启=$(systemctl is-enabled cron 2>/dev/null || echo 未知)"
echo "[提示] 到点执行记录（含退出码）会落在 $SLOG；若到点后该文件无新增行，用 journalctl -u cron 看调度器日志"
