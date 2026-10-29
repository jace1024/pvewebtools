
DEVICES=$(printf '%s' "${PT_DEVICES:-}" | tr -d ' ')
MODE="${PT_ACTION:-list}"
OK=0
FAIL=0
BOUND_DRVS=""
echo "====== 回收直通设备 ======"
echo "[说明] 概览里的「直通占用」只算虚机配置里 hostpci 引用的设备；没人用、却还被 vfio 占着的显示为「未使用（可释放）」，属本次可释放范围"

# 仅解除驱动屏蔽（配置层）：与「释放并恢复驱动」里的持久化清理一致，但无需指定设备，重启后驱动自动加载
if [ "$MODE" = "unblacklist" ]; then
  for f in /etc/modprobe.d/pve-blacklist.conf /etc/modprobe.d/blacklist.conf; do
    [ -f "$f" ] || continue
    backup_file "$f"
    sed -i '/blacklist i915/d; /blacklist snd_hda_intel/d; /blacklist snd_hda_codec_hdmi/d; /blacklist nouveau/d; /blacklist nvidia/d; /blacklist nvidiafb/d; /blacklist amdgpu/d; /blacklist radeon/d' "$f"
  done
  remove_block /etc/modprobe.d/pvewebtools-blacklist.conf IGPU_BLACKLIST
  remove_block /etc/modprobe.d/pvewebtools-blacklist.conf DGPU_BLACKLIST
  run "update-initramfs 执行失败" update-initramfs -u -k all
  echo "[完成] 驱动屏蔽已解除，重启后生效"
  exit 0
fi


# 读取设备当前绑定的驱动名：未绑定时必须输出空。
# 注意不能用 readlink -f —— 链接不存在时它会把路径原样回显，basename 得到 "driver"，
# 会被误判成"已还给宿主驱动：driver"（实际是无驱动）
pci_driver() {
  local d="/sys/bus/pci/devices/$1/driver" t=""
  if [ -e "$d" ]; then t=$(readlink "$d" 2>/dev/null || true); fi
  if [ -n "$t" ]; then basename "$t"; fi
}

# 设备可用的原驱动候选（输出顺序即尝试顺序）：
# lspci 认出的模块去掉 vfio-pci，并把 framebuffer/legacy 这类多数场景不想用的排到最后
# （NVIDIA 卡常列出 "nvidiafb, nouveau, nvidia"，而 nvidiafb 绑不上独显 → 必须逐个试）
_LOW_PRIO="nvidiafb nouveau efifb vesafb simpledrm vga16fb uvesafb"
orig_candidates() {
  local mods hi lo m
  mods=$(lspci -Dknn -s "$1" 2>/dev/null | sed -n 's/.*Kernel modules: //p' | tr ',' '\n' \
    | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | grep -v '^vfio-pci$' | grep -v '^$' || true)
  hi=""; lo=""
  for m in $mods; do
    case " $_LOW_PRIO " in *" $m "*) lo="$lo $m" ;; *) hi="$hi $m" ;; esac
  done
  printf '%s %s' "$hi" "$lo" | sed 's/^ *//; s/ *$//'
}

# 候选扫描：驱动是 vfio-pci，且没有任何 VM 的 hostpci 引用（正在直通的绝不列入）
if [ "$MODE" = "list" ] || [ -z "$DEVICES" ]; then
  CAND=""
  for p in $(lspci -D 2>/dev/null | awk '{print $1}'); do
    drv=$(pci_driver "$p")
    # 两类候选：还被 vfio 占着的；或已解绑但没装回驱动（上次释放没成功）
    if [ "$drv" != "vfio-pci" ] && [ -n "$drv" ]; then continue; fi
    cands_p=$(orig_candidates "$p")
    if [ -z "$drv" ] && [ -z "$cands_p" ]; then continue; fi
    bd=$(printf '%s' "$p" | sed 's/\.[0-9]*$//; s/^0000://')
    own=$(grep -lE "^hostpci[0-9]+:[[:space:]]*(0000:)?$bd([,. ]|$)" /etc/pve/qemu-server/*.conf 2>/dev/null | head -1 || true)
    if [ -n "$own" ]; then continue; fi
    CAND="$CAND $p"
  done
  echo
  echo "候选设备（没人用、宿主也用不了）："
  if [ -z "$CAND" ]; then
    echo "   （没有：设备要么正被 VM 直通，要么已经还给宿主了）"
  else
    for p in $CAND; do
      DESCP=$(lspci -nn -s "$p" 2>/dev/null | sed 's/^[0-9a-fA-F:.]* //' || true)
      CURP=$(pci_driver "$p")
      ORIGP=$(orig_candidates "$p" | awk '{print $1}')
      printf '   %s\t当前驱动：%s\t可装回：%s\t%s\n' "$p" "${CURP:-无}" "${ORIGP:-未知}" "$DESCP"
    done
  fi
fi

if [ "$MODE" = "list" ]; then
  echo
  echo "[完成] 只查看，未做任何改动"
  exit 0
fi

if [ -z "$DEVICES" ]; then
  echo
  echo "[错误] 未勾选要释放的设备：请在上方「要释放的设备」里勾选（可点「全选」）"
  exit 1
fi

for BDF in $(printf '%s' "$DEVICES" | tr ',' ' '); do
  echo
  echo "== $BDF =="
  if [ ! -e "/sys/bus/pci/devices/$BDF" ]; then
    echo "   [跳过] 设备不存在（BDF 可能已变化）"
    FAIL=$((FAIL+1)); continue
  fi
  DRV=$(pci_driver "$BDF")
  NEED_UNBIND=0
  if [ "$DRV" = "vfio-pci" ]; then
    NEED_UNBIND=1
  elif [ -z "$DRV" ]; then
    echo "   [状态] 当前没有驱动（上次解绑后没装回），本次尝试装回原驱动"
  else
    echo "   [跳过] 宿主驱动 $DRV 已正常持有，无需处理"
    FAIL=$((FAIL+1)); continue
  fi
  BD=$(printf '%s' "$BDF" | sed 's/\.[0-9]*$//; s/^0000://')
  # 安全闸门：被任何 VM 的 hostpci 引用 → 拒绝（否则会把正在直通的设备抢走）
  OWNER=$(grep -lE "^hostpci[0-9]+:[[:space:]]*(0000:)?$BD([,. ]|$)" /etc/pve/qemu-server/*.conf 2>/dev/null | head -1 || true)
  if [ -n "$OWNER" ]; then
    echo "   [拒绝] 正被 VM $(basename "$OWNER" .conf) 的 hostpci 引用；请先用「移除 VM 全部直通」移除后再释放"
    FAIL=$((FAIL+1)); continue
  fi
  # 原驱动候选（按优先顺序逐个试，见 orig_candidates 注释）
  CANDS=$(orig_candidates "$BDF")
  # PVE 直通时写过 driver_override=vfio-pci：不清掉，原驱动绑不回来。
  # 内核在"未设置"时可能读回 "(null)"，要当空处理，否则每台设备都会误报"已清空"
  OV="/sys/bus/pci/devices/$BDF/driver_override"
  OLD_OV=$(cat "$OV" 2>/dev/null || true)
  if [ "$OLD_OV" = "(null)" ]; then OLD_OV=""; fi
  if [ -n "$OLD_OV" ]; then
    if [ "${PT_CLEAR_OVERRIDE:-}" = "1" ]; then
      if (printf '' > "$OV") 2>/dev/null; then
        echo "   [处理] 已清空 driver_override（原值：$OLD_OV）"
      else
        echo "   [警告] 清空 driver_override 失败（可重启宿主解决）"
      fi
    else
      echo "   [提示] driver_override=$OLD_OV 未清理（按你的选择），原驱动可能绑不上"
    fi
  fi
  if [ "$NEED_UNBIND" = "1" ]; then
    if printf '%s' "$BDF" > /sys/bus/pci/drivers/vfio-pci/unbind 2>/dev/null; then
      echo "   [解绑] 已从 vfio-pci 解绑"
    else
      echo "   [警告] 解绑指令未成功（可能已被解绑）"
    fi
  fi
  # 绑回原驱动：按候选顺序逐个尝试，第一个绑上的即为结果
  # （只看第一个模块会误判：NVIDIA 卡常是 "nvidiafb, nouveau, nvidia"，nvidiafb 绑不上独显）
  BOUND=""
  if [ -n "$CANDS" ]; then
    for m in $CANDS; do
      if grep -rqsE "^[[:space:]]*blacklist[[:space:]]+$m" /etc/modprobe.d/ 2>/dev/null; then
        echo "   [提示] 驱动 $m 在 modprobe.d 里被屏蔽：屏蔽只挡开机自动加载，这里手动加载一次"
      fi
      modprobe "$m" 2>/dev/null || true
      # 写 bind 是同步接口：驱动要先 probe 完硬件才返回（AHCI/NVMe 会逐端口复位+识别，
      # 慢时 1~2 分钟，链路异常/掉盘过更久）。先给进度提示，再用 timeout 兜底，别让它看着像卡死。
      echo "   [绑定] 正在把 $BDF 交给 $m（驱动需重新探测硬件，慢时 1~2 分钟，请稍候）..."
      if [ -d "/sys/bus/pci/drivers/$m" ] \
         && timeout 180 sh -c "printf '%s' '$BDF' > '/sys/bus/pci/drivers/$m/bind'" 2>/dev/null; then
        BOUND="$m"
        BOUND_DRVS="$BOUND_DRVS $m"
        echo "   [绑定] 已绑回 $m"
        break
      fi
      # 超时兜底：探测还在后台跑，但设备实际已经绑上 → 仍算成功，避免误判
      if [ "$(pci_driver "$BDF")" = "$m" ]; then
        BOUND="$m"
        BOUND_DRVS="$BOUND_DRVS $m"
        echo "   [绑定] 已绑回 $m（探测仍在后台继续，可稍后用 lspci -k 确认）"
        break
      fi
      if [ -d "/sys/bus/pci/drivers/$m" ]; then
        echo "   [超时] 交给 $m 后 180 秒未返回：多为盘/链路异常导致的探测卡住，建议查 dmesg 的 ata 日志并重启宿主机"
      fi
    done
    if [ -z "$BOUND" ]; then
      echo "   [警告] 候选驱动（$CANDS）都绑不上，可手动逐个试："
      for m in $CANDS; do echo "            echo $BDF > /sys/bus/pci/drivers/$m/bind"; done
    fi
  else
    echo "   [警告] lspci 认不出可用驱动（未安装或已被屏蔽），设备维持「无驱动」状态"
  fi
  NOW=$(pci_driver "$BDF")
  case "$NOW" in
    vfio-pci) echo "   [结果] 仍被 vfio-pci 占用（多为 driver_override 未清或设备被占用，可重启宿主）"; FAIL=$((FAIL+1)) ;;
    "")       echo "   [结果] 当前没有驱动（宿主可以用了；要让它干活需装上对应驱动）"; OK=$((OK+1)) ;;
    *)        echo "   [结果] 已还给宿主驱动：$NOW"; OK=$((OK+1)) ;;
  esac
done

# 持久化：把本次成功装回的原驱动从 modprobe.d 黑名单移除并刷新 initramfs，
# 让重启后驱动也能自动加载（不再需要再单独跑「仅解除驱动屏蔽」）
NEED_INIT=0
for m in $BOUND_DRVS; do
  for f in /etc/modprobe.d/*.conf; do
    [ -f "$f" ] || continue
    if grep -qsE "^[[:space:]]*blacklist[[:space:]]+$m([[:space:]]|$)" "$f"; then
      backup_file "$f"
      sed -i "/^[[:space:]]*blacklist[[:space:]]*$m([[:space:]]|\$)/d" "$f"
      NEED_INIT=1
    fi
  done
done
if [ "$NEED_INIT" = "1" ]; then
  echo
  echo "[持久化] 已移除相关驱动的黑名单并刷新 initramfs，重启后驱动自动加载（无需再单独解除屏蔽）"
  run "update-initramfs 执行失败" update-initramfs -u -k all
fi

echo
echo "[结果] 成功 $OK 个，失败/跳过 $FAIL 个"
echo "[说明] 释放后概览里这些设备会从「未使用（可释放）」变成「宿主使用」；网卡还给宿主后可到「网络管理」纳入 vmbr"
echo "[持久化] 本次装回的驱动若此前被黑名单屏蔽，已一并移除并刷新 initramfs，重启后自动加载；"
echo "         若显示「无驱动」仅因没装驱动包，需先 apt 安装对应驱动。"
if [ "$FAIL" != "0" ]; then
  echo "[提示] 有失败/跳过项：按上方每条提示处理（正在直通的设备必须先移除 hostpci）"
fi
if [ "$OK" = "0" ] && [ "$FAIL" != "0" ]; then exit 1; fi
