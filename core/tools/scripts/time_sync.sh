
show_status() {
 echo "---- 当前时间状态 ----"
 timedatectl 2>/dev/null || { date; echo "(timedatectl 不可用)"; }
 if has_unit systemd-timesyncd.service; then
   ST=$(systemctl is-active systemd-timesyncd 2>/dev/null || true)
   echo "时间同步服务: systemd-timesyncd ($ST)"
 elif has_unit chrony.service; then
   ST=$(systemctl is-active chrony 2>/dev/null || true)
   echo "时间同步服务: chrony ($ST)"
 else
   echo "时间同步服务: 未找到 systemd-timesyncd / chrony"
 fi
}
# 单元存在性探测：先看单元文件落盘位置，再枚举兜底；grep -c 读完整输入避开 SIGPIPE 误判
has_unit() {  # $1=单元名
 for d in /usr/lib/systemd/system /lib/systemd/system /etc/systemd/system /run/systemd/system; do
   [ -f "$d/$1" ] && return 0
 done
 local n
 n=$(systemctl list-unit-files --no-legend 2>/dev/null | { grep -c "^$1" || true; })
 [ "${n:-0}" -ge 1 ] && return 0
 return 1
}
if [ "$PT_ACTION" = "status" ]; then
 show_status
 echo "[完成] 若 NTP 服务=否 或 同步=no，可用「开启 NTP 并立即同步」修复"
 exit 0
fi
if [ "$PT_ACTION" = "sync" ]; then
 if has_unit systemd-timesyncd.service; then
   TS_CONF=/etc/systemd/timesyncd.conf
   if [ -n "$PT_NTP_SERVER" ]; then
     backup_file "$TS_CONF"
     if grep -qE '^[#[:space:]]*NTP=' "$TS_CONF" 2>/dev/null; then
       sed -i "s|^[#[:space:]]*NTP=.*|NTP=$PT_NTP_SERVER|" "$TS_CONF"
     else
       echo "NTP=$PT_NTP_SERVER" >> "$TS_CONF"
     fi
     echo "[写入] $TS_CONF [NTP=$PT_NTP_SERVER]"
   fi
   run "NTP 同步服务启用失败" systemctl enable --now systemd-timesyncd
   systemctl restart systemd-timesyncd
   echo "[等待] 首次同步可能需要几秒到半分钟…"
   OK=0
   for i in $(seq 1 12); do
     if timedatectl show -p NTPSynchronized 2>/dev/null | grep -q '=yes'; then OK=1; break; fi
     sleep 5
   done
   if [ $OK -eq 1 ]; then
     echo "[完成] NTP 同步成功（服务已开机自启），当前状态:"
   else
     echo "[警告] 60 秒内未确认同步成功（可能网络受限），当前状态供参考:"
   fi
   show_status
   exit 0
 fi
 if has_unit chrony.service || command -v chronyd >/dev/null 2>&1; then
   echo "[信息] 本机没有 systemd-timesyncd，改用 chrony 同步"
   if [ -n "$PT_NTP_SERVER" ]; then
     # Debian chrony 支持 sources.d 机制就用它，否则备份后追加到主配置
     if [ -d /etc/chrony/sources.d ] && grep -q '^sourcedir' /etc/chrony/chrony.conf 2>/dev/null; then
       SRC=/etc/chrony/sources.d/pvewebtools.sources
       printf 'server %s iburst\n' "$PT_NTP_SERVER" > "$SRC"
       echo "[写入] $SRC [server $PT_NTP_SERVER iburst]"
     else
       backup_file /etc/chrony/chrony.conf
       echo "server $PT_NTP_SERVER iburst" >> /etc/chrony/chrony.conf
       echo "[写入] /etc/chrony/chrony.conf [server $PT_NTP_SERVER iburst]"
     fi
   fi
   run "chrony 启用失败" systemctl enable --now chrony
   systemctl restart chrony
   echo "[等待] 尝试立即强制对时（makestep）…"
   chronyc -a makestep 2>/dev/null || echo "[提示] makestep 未成功（可能还没收到源应答），chrony 会自行逐步收敛"
   sleep 2
   echo "[完成] chrony 已启用并开机自启，当前跟踪状态:"
   chronyc tracking 2>/dev/null || true
   exit 0
 fi
 echo "[错误] 未找到 systemd-timesyncd 或 chrony，请先执行: apt update && apt install systemd-timesyncd，再重新运行本工具"
 exit 1
fi
if [ "$PT_ACTION" = "rtc" ]; then
 echo "系统时间 : $(date '+%F %T %z')"
 echo "主板时钟 : $(hwclock --show 2>/dev/null || echo 读取失败)"
 run "系统时间写入主板时钟(RTC)失败" hwclock --systohc
 echo "校准后RTC: $(hwclock --show 2>/dev/null || echo 读取失败)"
 echo "[完成] 主板时钟已校准为当前系统时间（断电后开机时间不再漂移的前提）"
 echo "[提示] 若断电后时间仍跳变，是主板 CMOS 电池没电了，换颗 CR2032 即可"
 exit 0
fi
if [ "$PT_ACTION" = "tz" ]; then
 [ -n "$PT_TZ" ] || { echo "[错误] 修改时区操作需选择时区"; exit 1; }
 # awk 读完整输入再判定，不用 grep -q：匹配到即退出会让上游 printf 收 SIGPIPE，pipefail 下误报无效时区
 TZLIST=$(timedatectl list-timezones 2>/dev/null || true)
 TZHIT=$(printf '%s\n' "$TZLIST" | awk -v tz="$PT_TZ" '$0==tz {found=1} END {print found+0}')
 [ "$TZHIT" = "1" ] || { echo "[错误] 无效时区: $PT_TZ"; exit 1; }
 run "时区设置失败" timedatectl set-timezone "$PT_TZ"
 # 写后验证：时区确实切过去了
 run "时区设置未生效" sh -c "timedatectl show -p Timezone | grep -q \"=$PT_TZ\""
 echo "[完成] 时区已设为 $PT_TZ，当前时间: $(date '+%F %T %z')"
 echo "[提示] 定时任务的触发时刻按新时区计算，如有定时开关机任务请留意重新核对"
 exit 0
fi
echo "[错误] 未知操作: $PT_ACTION"; exit 1
