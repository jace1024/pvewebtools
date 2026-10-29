
VMID="$PT_VMID"
qm status "$VMID" >/dev/null 2>&1 || { echo "[错误] VM $VMID 不存在"; exit 1; }
MODE="$PT_ACTION"
# 快捷项与自定义参数叠加（前者在前），并去重规整
ARGS=$(kargs_dedupe "${PT_PRESET:-} ${PT_KARGS:-}")
REBOOT="${PT_REBOOT:-}"
QSTATE=$(qm status "$VMID" 2>/dev/null | awk '{print $2}')

echo "===== 调整虚机内核参数 ====="
echo "[信息] VM $VMID 状态: ${QSTATE:-未知}；agent 配置: $(qm config "$VMID" | grep '^agent:' || echo '未启用')"
if [ "$MODE" != "list" ]; then
  [ -n "$ARGS" ] || { echo "[错误] 未选择/填写任何参数（快捷项与自定义至少要有一个）"; exit 1; }
  echo "[参数] $MODE: $ARGS"
fi
# QEMU 代理只在虚机运行时可用：虚机是关机状态时，可按勾选先临时开机，做完再关回原状
STARTED=0
if [ "$QSTATE" != "running" ]; then
  if [ "$PT_AUTOSTART" != "1" ]; then
    echo "[错误] VM 未运行（${QSTATE:-未知}）：QEMU 代理仅在虚机运行时可用。请先启动（qm start $VMID），或勾选「关机时先自动开机」"
    exit 1
  fi
  echo "[虚机] 虚机未运行（${QSTATE:-未知}）：先临时开机（最长等 90 秒 QEMU 代理就绪），做完关回原状"
  if qm start "$VMID" >/dev/null 2>&1; then
    STARTED=1
  else
    echo "[错误] 临时开机失败，请手动开机后重试"
    exit 1
  fi
  AGENT_OK=0
  for i in $(seq 1 45); do
    sleep 2
    if qm agent "$VMID" ping >/dev/null 2>&1; then AGENT_OK=1; break; fi
  done
  if [ "$AGENT_OK" != "1" ]; then
    echo "[提示] 开机后 90 秒内 QEMU 代理未就绪（未装 qemu-guest-agent / VM「选项」未开 agent=1）：本次无法操作，稍后关回虚机"
  else
    echo "[虚机] 已开机，QEMU 代理就绪"
  fi
  # 临时开机只为拿到代理（参数下次正常开机即生效），不需要虚机自行重启
  REBOOT=""
fi

OK=0
if guest_kargs_apply "$VMID" "$MODE" "$ARGS" "$REBOOT"; then OK=1; fi

# 临时开起来的虚机：不论成败都关回去，保持原有电源状态
if [ "$STARTED" = "1" ]; then
  echo "[虚机] 正在关回虚机（原来为关机状态，最长等 120 秒）..."
  qm shutdown "$VMID" >/dev/null 2>&1 || true
  WAIT=0
  while [ "$WAIT" -lt 120 ]; do
    sleep 2
    WAIT=$((WAIT + 2))
    if [ "$(qm status "$VMID" 2>/dev/null | awk '{print $2}')" = "stopped" ]; then break; fi
  done
  if [ "$(qm status "$VMID" 2>/dev/null | awk '{print $2}')" = "stopped" ]; then
    echo "[完成] 已关回原状（下次开机即生效）"
  else
    echo "[警告] 虚机未在 120 秒内关机，请手动关机：qm shutdown $VMID"
  fi
fi

if [ "$OK" = "1" ]; then
  echo
  case "$MODE" in
    list)   echo "[完成] 已查看内核参数（未做修改）" ;;
    add)    echo "[完成] 已追加: $ARGS（重启后生效）" ;;
    remove) echo "[完成] 已移除: $ARGS（重启后生效）" ;;
  esac
  if [ "$MODE" != "list" ]; then
    echo "[备份] 原配置保留在虚机内 /etc/default/grub.bak.<时间戳>"
    if [ "$STARTED" != "1" ] && [ "$REBOOT" != "1" ]; then echo "[重要] 需重启虚机生效：qm reboot $VMID"; fi
  fi
  echo "[复验] 重启后在虚机内执行：cat /proc/cmdline"
else
  echo
  echo "[提示] 未能自动完成（原因见上）。可在虚机内手工执行："
  echo "       cp -a /etc/default/grub /etc/default/grub.bak.\$(date +%F_%H%M%S)"
  if [ "$MODE" = "remove" ]; then
    echo "       # 在 /etc/default/grub 里删掉这些参数: $ARGS"
  else
    echo "       sed -i 's/^\(GRUB_CMDLINE_LINUX_DEFAULT=\"[^\"]*\)\"/\1 $ARGS\"/' /etc/default/grub"
  fi
  echo "       update-grub || grub-mkconfig -o /boot/grub/grub.cfg"
  echo "       若原因是虚机未装 QEMU 代理：在 VM「选项」开启 agent=1，并在虚机内安装启用 qemu-guest-agent 后即可自动完成"
  exit 1
fi
