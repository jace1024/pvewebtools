
ACTION="${PT_ACTION:-set}"
# 只读监控分支：实时轮询每核真实频率（与下方「设置」分支互不干扰）
if [ "$ACTION" = "view" ]; then
  SECS="${PT_SECS:-15}"
  case "$SECS" in ''|*[!0-9]*) SECS=15;; esac
  [ "$SECS" -gt 120 ] && SECS=120
  LOOP=$SECS
  [ "$SECS" -eq 0 ] && LOOP=1
  CPUFREQ=$(ls -d /sys/devices/system/cpu/cpu*/cpufreq 2>/dev/null | head -1)
  [ -z "$CPUFREQ" ] && CPUFREQ=$(ls -d /sys/devices/system/cpu/cpufreq/policy* 2>/dev/null | head -1)
  DRV=$(cat "$CPUFREQ/scaling_driver" 2>/dev/null || true)
  GOV=$(cat "$CPUFREQ/scaling_governor" 2>/dev/null || true)
  NT=$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null || true)
  [ -z "$NT" ] && NT="-(非 intel_pstate)"
  BF=$(cat "$CPUFREQ/cpuinfo_min_freq" 2>/dev/null || true)
  MF=$(cat "$CPUFREQ/cpuinfo_max_freq" 2>/dev/null || true)
  [ -n "$BF" ] && BF=$((BF/1000))
  [ -n "$MF" ] && MF=$((MF/1000))
  NOMINAL=$(awk -F'@' '/model name/ {split($2,a,"GHz"); gsub(/[^0-9.]/,"",a[1]); printf "%.0f", a[1]*1000; exit}' /proc/cpuinfo 2>/dev/null)
  [ -z "$NOMINAL" ] && NOMINAL=$BF
  read_freqs() {
    local out=""
    for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq; do
      [ -f "$f" ] || continue
      local khz; khz=$(cat "$f" 2>/dev/null)
      case "$khz" in ''|*[!0-9]*) continue;; esac
      out="$out $((khz/1000))"
    done
    if [ -z "$out" ]; then
      out=$(awk -F: '/cpu MHz/ {gsub(/[[:space:]]/,"",$2); printf " %.0f", $2}' /proc/cpuinfo 2>/dev/null)
    fi
    printf '%s' "$out"
  }
  echo "===== CPU 实时频率监控（每核，单位 MHz）====="
  echo "驱动=$DRV 模式=$GOV 睿频开关=$NT 基准=$NOMINAL 最低硬件=$BF 最高=$MF"
  echo "（在 KVM 虚机里 Windows 读不到真实频率属正常；本工具读的是宿主机物理核真实值）"
  echo
  ITER=0
  while [ "$ITER" -lt "$LOOP" ]; do
    ITER=$((ITER+1))
    FREQS=$(read_freqs)
    TS=$(date '+%H:%M:%S')
    MIN=999999; MAX=0; SUM=0; CNT=0
    for v in $FREQS; do
      [ "$v" -lt "$MIN" ] && MIN=$v
      [ "$v" -gt "$MAX" ] && MAX=$v
      SUM=$((SUM+v)); CNT=$((CNT+1))
    done
    AVG=0; [ "$CNT" -gt 0 ] && AVG=$((SUM/CNT))
    TURBO=""
    if [ -n "$NOMINAL" ] && [ "$CNT" -gt 0 ] && [ "$MAX" -gt $((NOMINAL+50)) ] 2>/dev/null; then TURBO="  ▲部分核心睿频中"; fi
    if [ "$CNT" -eq 0 ]; then
      echo "[$TS] [警告] 无法读取任何核心频率（本机可能无 cpufreq 接口，或精简内核未暴露 /sys 调频节点）"
    else
      echo "[$TS] 核心数=$CNT"
      echo "  每核MHz:$(echo "$FREQS" | fold -s -w 96 | sed 's/^/    /')"
      echo "  最低/平均/最高 = $MIN / $AVG / $MAX  (基准 $NOMINAL / 最高 $MF)$TURBO"
    fi
    echo
    [ "$SECS" -ne 0 ] && sleep 1
  done
  echo "[结束] $([ "$SECS" -eq 0 ] && echo 单次快照 || echo 监控 ${SECS}s 已结束)"
  exit 0
fi
GOV="${PT_GOVERNOR:-performance}"
LOCKFREQ="$PT_LOCK_FREQ"
ARCH=$(uname -m)
# 用第一个存在的 cpufreq 目录：部分 ARM 机器核心 0 可能不暴露调频接口或处于离线
CPUFREQ=$(ls -d /sys/devices/system/cpu/cpu*/cpufreq 2>/dev/null | head -1)
[ -n "$CPUFREQ" ] || { echo "[错误] 本机不支持 cpufreq 调频（虚拟机/固定频率 ARM/精简内核常见）"; exit 1; }
DRV=$(cat "$CPUFREQ/scaling_driver" 2>/dev/null || true)
AVAILABLE=$(cat "$CPUFREQ/scaling_available_governors" 2>/dev/null || true)
# 满载锁频 = performance + HWP 性能偏好 + 每核最低频率抬到最高（仅对性能优先模式生效）
AGGRESSIVE=0
if [ "$LOCKFREQ" = "1" ]; then
 if [ "$GOV" = "performance" ]; then
   AGGRESSIVE=1
 else
   echo "[提示] 满载锁频仅对「性能优先」模式生效，本次已忽略"
 fi
fi
echo "---- 当前调频状态 ----"
echo "架构     : $ARCH"
echo "驱动     : ${DRV:-未知}"
echo "可用模式 : ${AVAILABLE:-未知}"
echo "当前模式 : $(cat "$CPUFREQ/scaling_governor" 2>/dev/null || echo 未知)"
# 所选模式不在驱动可用列表时，按语义做等价映射：intel_pstate 常只有 performance/powersave，
# 其 powersave 即硬件智能调频（HWP 自动按负载调频），语义上正好对应均衡/省电
if ! echo "$AVAILABLE" | grep -qw "$GOV"; then
 case "$GOV" in
   schedutil|conservative|powersave)
     if echo "$AVAILABLE" | grep -qw "powersave"; then
       echo "[提示] 驱动 $DRV 不支持 $GOV，已映射为语义等价的 powersave（$DRV 的 powersave 即硬件智能调频，效果等同均衡/省电）"
       GOV=powersave
     fi
     ;;
 esac
fi
# 无等价映射项时（如驱动连 powersave 都没有）才报错
if ! echo "$AVAILABLE" | grep -qw "$GOV"; then
 echo "[错误] 当前驱动（$DRV）不支持 $GOV 模式，可选：$AVAILABLE"; exit 1
fi
echo; echo "[1/3] 立即应用 $GOV 到全部 CPU ..."
FAILED=0
for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
 echo "$GOV" > "$g" 2>/dev/null || { echo "[警告] $g 写入失败"; FAILED=1; }
done
[ "$FAILED" = 0 ] || echo "[提示] 部分核心切换失败，不影响其余核心"
# 非锁频模式：先解除历史满载锁频残留（每核最低频率、HWP/EPP 偏好、intel_pstate 性能范围）。
# 否则之前锁频抬高的 scaling_min_freq 会把省电/均衡模式的频率顶住，频率降不下来
if [ "$AGGRESSIVE" != 1 ]; then
 RESTORED=0
 for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_min_freq; do
   [ -f "$f" ] || continue
   d=$(dirname "$f")
   MIN=$(cat "$f" 2>/dev/null || true)
   HMIN=$(cat "$d/cpuinfo_min_freq" 2>/dev/null || true)
   case "$MIN" in ''|*[!0-9]*) continue;; esac
   case "$HMIN" in ''|*[!0-9]*) HMIN=0;; esac
   # 当前最低频率高于硬件最低（被锁频抬高过）才需要恢复，写 0 由内核钳制到硬件最低
   if [ "$MIN" -gt "$HMIN" ] 2>/dev/null; then
     echo 0 > "$f" 2>/dev/null && RESTORED=1
   fi
 done
 [ "$RESTORED" = 1 ] && echo "[设置] 已解除满载锁频（每核最低频率恢复硬件默认）"
 EPP_DONE=0
 for f in /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference; do
   [ -f "$f" ] || continue
   CUR=$(cat "$f" 2>/dev/null || true)
   if [ -n "$CUR" ] && [ "$CUR" != "balance_power" ]; then
     echo balance_power > "$f" 2>/dev/null && EPP_DONE=1
   fi
 done
 [ "$EPP_DONE" = 1 ] && echo "[设置] HWP/EPP 性能偏好 → balance_power（默认）"
 if [ -f /sys/devices/system/cpu/intel_pstate/min_perf_pct ]; then
   MINP=$(cat /sys/devices/system/cpu/intel_pstate/min_perf_pct 2>/dev/null || true)
   if [ -n "$MINP" ] && [ "$MINP" != "0" ]; then
     echo 0 > /sys/devices/system/cpu/intel_pstate/min_perf_pct 2>/dev/null && echo "[设置] intel_pstate 最低性能 → 0（默认）"
   fi
 fi
 [ -f /sys/devices/system/cpu/intel_pstate/max_perf_pct ] && echo 100 > /sys/devices/system/cpu/intel_pstate/max_perf_pct 2>/dev/null || true
fi
if [ "$AGGRESSIVE" = 1 ]; then
 # HWP 性能偏好：Intel HWP / AMD pstate 均暴露 energy_performance_preference
 # EPP_OK 供后面的锁频诊断引用：区分「接口不存在」与「存在但被 BIOS 锁定拒绝写入」
 EPP_OK=0
 for f in /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference; do
   echo performance > "$f" 2>/dev/null && EPP_OK=1
 done
 if [ "$EPP_OK" = 1 ]; then
   echo "[设置] HWP/EPP 性能偏好 → performance"
 elif [ -f "$CPUFREQ/energy_performance_preference" ]; then
   echo "[警告] HWP/EPP 性能偏好接口存在但写入被拒（可能被 BIOS/固件锁定）"
 else
   echo "[提示] 本机无 HWP/EPP 性能偏好接口（未启用 HWP 或驱动不支持），跳过"
 fi
 # 通用满载锁频：把每核最低频率抬到其最大频率（scaling_min_freq=scaling_max_freq），
 # Intel HWP/非 HWP、AMD pstate、acpi-cpufreq 均支持，比 intel_pstate.min_perf_pct 更可靠
 # 先重写一次 max 再写 min：部分内核（HWP）要求先设置上限再设置下限
 LOCKED=0
 ERRLOG=/tmp/pvewebtools-freq-lock.err
 for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_min_freq; do
   d=$(dirname "$f")
   maxf=$(cat "$d/scaling_max_freq" 2>/dev/null) || continue
   case "$maxf" in ''|*[!0-9]*) continue;; esac
   echo "$maxf" > "$d/scaling_max_freq" 2>/dev/null || true
   if echo "$maxf" > "$f" 2>"$ERRLOG"; then
     LOCKED=1
   else
     echo "[警告] 写 $f 失败: $(cat "$ERRLOG" 2>/dev/null)"
   fi
 done
 rm -f "$ERRLOG"
 [ "$LOCKED" = 1 ] && echo "[设置] 每核最低频率 → 最大频率（满载锁频）" || echo "[警告] 写入 scaling_min_freq 全部失败，满载锁频未生效"
 # intel_pstate 双保险：锁性能范围 100%（amd-pstate 无此接口则自动跳过）
 for f in /sys/devices/system/cpu/intel_pstate/min_perf_pct /sys/devices/system/cpu/intel_pstate/max_perf_pct; do
   [ -f "$f" ] && echo 100 > "$f" 2>/dev/null && echo "[设置] $f → 100"
 done
fi
echo "[2/3] 持久化配置（重启后仍生效）..."
# Debian 13 已移除 cpufrequtils 包，不再走 apt；用自管理的 systemd 单元开机自动应用
UNIT=/etc/systemd/system/pvewebtools-cpufreq.service
backup_file "$UNIT"
if [ "$AGGRESSIVE" = 1 ]; then
 UNIT_CMD="/bin/sh -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo $GOV > \$g; done; for f in /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference; do echo performance > \$f 2>/dev/null || true; done; for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_min_freq; do d=\${f%/scaling_min_freq}; m=\$(cat \"\$d/scaling_max_freq\" 2>/dev/null) || continue; case \"\$m\" in \"\"|*[!0-9]*) continue;; esac; echo \"\$m\" > \"\$f\" 2>/dev/null || true; done; [ -f /sys/devices/system/cpu/intel_pstate/min_perf_pct ] && echo 100 > /sys/devices/system/cpu/intel_pstate/min_perf_pct; [ -f /sys/devices/system/cpu/intel_pstate/max_perf_pct ] && echo 100 > /sys/devices/system/cpu/intel_pstate/max_perf_pct'"
 UNIT_DESC="Set CPU frequency governor to $GOV with aggressive performance (HWP + max perf, managed by pvewebtools)"
else
 UNIT_CMD="/bin/sh -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo $GOV > \$g; done'"
 UNIT_DESC="Set CPU frequency governor to $GOV (managed by pvewebtools)"
fi
cat > "$UNIT" <<EOF
[Unit]
Description=$UNIT_DESC
After=multi-user.target

[Service]
Type=oneshot
ExecStart=$UNIT_CMD

[Install]
WantedBy=multi-user.target
EOF
# 清理早期版本写入的 cpufrequtils 配置块（若有），避免两处维护
if [ -f /etc/default/cpufrequtils ]; then
 backup_file /etc/default/cpufrequtils
 remove_block /etc/default/cpufrequtils CPUFREQ_GOV
fi
run "systemd 重载失败" systemctl daemon-reload
run "开机自启设置失败" systemctl enable pvewebtools-cpufreq.service
# 写后验证：核心 0 的模式确实切过来了
echo; echo "---- 应用后状态 ----"
NOW=$(cat "$CPUFREQ/scaling_governor" 2>/dev/null || true)
echo "当前模式 : $NOW"
if [ "$NOW" != "$GOV" ]; then
 echo "[错误] 模式切换未生效（期望 $GOV，实际 $NOW）"; exit 1
fi
if [ "$AGGRESSIVE" = 1 ]; then
 EPP=$(cat "$CPUFREQ/energy_performance_preference" 2>/dev/null || true)
 [ -n "$EPP" ] && echo "HWP 偏好 : $EPP"
 MINP=$(cat /sys/devices/system/cpu/intel_pstate/min_perf_pct 2>/dev/null || true)
 [ -n "$MINP" ] && echo "最低性能: ${MINP}%"
 # 验证满载锁频：每核最低频率应等于其最大频率
 MINOK=1
 for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_min_freq; do
   d=$(dirname "$f")
   mn=$(cat "$f" 2>/dev/null || true)
   mx=$(cat "$d/scaling_max_freq" 2>/dev/null || true)
   if [ -n "$mn" ] && [ "$mn" = "$mx" ]; then
     :
   else
     MINOK=0
     echo "[警告] $f 未锁定: min=$mn, max=$mx"
   fi
 done
 if [ "$MINOK" = 1 ]; then
   echo "频率锁定: 每核已锁至最大频率（min=max）"
 else
   echo; echo "---- 锁频未生效，自动诊断 ----"
   DRV2=$(cat "$CPUFREQ/scaling_driver" 2>/dev/null || true)
   echo "驱动    : ${DRV2:-未知}"
   if [ -f "$CPUFREQ/energy_performance_preference" ]; then
     if [ "$EPP_OK" = 1 ]; then
       echo "HWP 接口: 存在且可写（写入 performance 成功）"
     else
       echo "HWP 接口: 存在但写入被拒 → 很可能被 BIOS/固件锁定"
     fi
   else
     echo "HWP 接口: 不存在（未启用 HWP，或驱动不支持）"
   fi
   case "$DRV2" in
     *intel_pstate*)
       PSTATUS=$(cat /sys/devices/system/cpu/intel_pstate/status 2>/dev/null || true)
       echo "intel_pstate 状态: ${PSTATUS:-未知}"
       if [ "$PSTATUS" = "passive" ]; then
         echo "→ 原因: intel_pstate 为 passive 模式，性能范围接口受限"
         echo "→ 建议: 内核参数加 intel_pstate=active（或 intel_pstate=disable 退回 acpi-cpufreq）后重启再试"
       else
         echo "→ 原因: 写 scaling_min_freq 被拒绝，大概率 BIOS 接管了 HWP/P-States 控制"
         echo "→ 建议: 进 BIOS 关闭 HWP / 将 CPU 节能项设为最大性能；或内核参数 intel_pstate=disable 后用 acpi-cpufreq 锁频"
       fi
       ;;
     *amd*pstate*)
       echo "→ 原因: 写 scaling_min_freq 被拒绝，可能 BIOS 锁定 CPPC 或内核限制"
       echo "→ 建议: BIOS 开启 CPPC / 禁用 Cool'n'Quiet，并确认内核参数 amd_pstate=active 已生效"
       ;;
     *acpi*cpufreq*)
       echo "→ 原因: acpi-cpufreq 下锁频被拒，多为 BIOS 固件限制"
       echo "→ 建议: AMD 机可先在内核参数加 amd_pstate=active 启用硬件调频；通用做法是进 BIOS 调整节能项"
       ;;
     *)
       case "$ARCH" in
         aarch64|arm64|armv7l|armv6l)
           echo "→ 原因: ARM cpufreq 驱动（$DRV2）下写 scaling_min_freq 被拒，多为固件/DTB 或驱动限制"
           echo "→ 建议: 查看 dmesg 中 cpufreq 相关报错；部分 SoC 需调整 DTB 频率表或禁用省电策略后重试"
           ;;
         *)
           echo "→ 建议: 进 BIOS 把 CPU 节能项（SpeedStep/EIST/HWP/C-States/P-States）设为最大性能后重试"
           ;;
       esac
       ;;
   esac
 fi
fi
CURF=$(cat "$CPUFREQ/scaling_cur_freq" 2>/dev/null || true)
if [ -n "$CURF" ] && [ "$CURF" -gt 0 ] 2>/dev/null; then
 echo "当前频率 : $((CURF / 1000)) MHz"
else
 awk -F: '/MHz/{printf "当前频率 : %s MHz\n", $2; exit}' /proc/cpuinfo 2>/dev/null || true
fi
if [ "$AGGRESSIVE" = 1 ]; then
 echo "[完成] 已启用满载锁频：governor=performance + 每核锁定至最高频率（HWP/EPP、intel_pstate 按平台自动适配），开机自启单元已生效（耗电与发热会明显上升）"
else
 echo "[完成] CPU 调频已切换为 $GOV，开机自启单元 $UNIT 已生效"
fi
