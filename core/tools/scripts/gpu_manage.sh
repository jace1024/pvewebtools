
if [ "$PT_ACTION" = "add" ]; then
 VMID="$PT_VMID"
 qm status "$VMID" >/dev/null 2>&1 || { echo "[错误] VM $VMID 不存在"; exit 1; }
 # PCIe 直通前置条件：IOMMU 已开启（iommu_groups 非空）
 ls /sys/kernel/iommu_groups 2>/dev/null | grep -q . || { echo "[错误] 未开启 IOMMU，请先执行「开启硬件直通」开启并重启宿主机"; exit 1; }
 # 宿主显示驱动占用拦截：显卡仍被宿主显示驱动（amdgpu/radeon/nouveau/nvidia/i915/xe）绑定时，
 # vfio 拿不到设备，VM 启动后必失败，故直通前拦截并引导先屏蔽驱动。
 DRV=""
 [ -e "/sys/bus/pci/devices/$PT_DEVICE/driver" ] && DRV=$(basename "$(readlink -f "/sys/bus/pci/devices/$PT_DEVICE/driver")")
 case "$DRV" in
   amdgpu|radeon|nouveau|nvidia|i915|xe)
     echo "[错误] 显卡 $PT_DEVICE 仍被宿主显示驱动 $DRV 占用，直通后 VM 无法正常使用"
     echo "[原因] 显卡必须先从宿主驱动解绑（屏蔽后重启，让 vfio-pci 接管）才能直通给 VM"
     echo "[引导] 请执行「开启硬件直通」："
     echo "   - 直通独显 → 勾选「屏蔽独显驱动（nouveau/nvidia/amdgpu/radeon）」= 是"
     echo "   - 直通核显 → 勾选「屏蔽核显/声卡驱动」= 是（Intel 核显走 i915、AMD APU 核显走 amdgpu，两种都已覆盖）"
     echo "   然后重启宿主机，再回来添加显卡直通"
     exit 1
     ;;
 esac
 # 已在配置里？复用原槽位按本次参数覆盖更新（幂等：便于调整 x-vga / legacy-igd / romfile 等），
 # 否则分配一个空闲槽位。匹配用 bus:device（去 function / 去 0000: 前缀），兼容整组多设备的写法。
 DUP_BD=$(echo "${PT_DEVICE%.*}" | sed 's/^0000://')
 SLOT=$(qm config "$VMID" | grep -E "^hostpci[0-9]+:" | grep -E "(^|[: ])(0000:)?${DUP_BD}([,;.]|$)" | head -1 | cut -d: -f1 || true)
 if [ -n "$SLOT" ]; then
   echo "[提示] 该显卡已在 $SLOT 中，按本次参数覆盖更新（无需先移除）"
 else
   for i in $(seq 0 9); do
     qm config "$VMID" | grep -q "^hostpci${i}:" || { SLOT="hostpci${i}"; break; }
   done
   [ -n "$SLOT" ] || { echo "[错误] 没有空闲 hostpci 槽位"; exit 1; }
 fi
 # 主设备（显卡本身）带专属选项，同组附带设备随后以分号追加（选项不能作用到附带设备）
 # 缩短语法 bus:device（如 01:00）已自动涵盖同 device 的全部 function（显卡 + HDMI 音频），
 # 组内若还有其它 bus:device（非桥接），再以分号追加整组一起过
 OPTS=""
 GTXT="独显"
 if [ "$PT_GTYPE" = "igpu" ]; then
   GTXT="核显"
 fi
 # 主设备地址：缩短为 bus:device（去掉 .function），PVE 用它可直通该 device 全部 function
 MAIN_BD="${PT_DEVICE%.*}"
 OPTS="$MAIN_BD"
 if [ "$PT_GTYPE" = "igpu" ]; then
   # 核显两种用法（默认仅计算 = 纯直通，最常用且不触碰 VM 的 vga 配置）：
   #  - 仅计算：hostpci 不带任何子项；VM 保留模拟显卡，Web VNC 控制台照常可用；
   #  - 需要显示输出：必须先把 VM 显示设为 none，再写 legacy-igd=1
   #    （PVE 校验：legacy IGD assignment requires VGA mode to be 'none'）。
   # 注：旧版 PVE 用 x-igd-opregion=1，新 schema 已不接受，会直接报 property is not defined。
   VENDOR=$(cat "/sys/bus/pci/devices/$PT_DEVICE/vendor" 2>/dev/null || true)
   # 代次提示：仅作路线指引（legacy-igd 只对老代次有效；Xe 及更新代次整卡直通成功率也有限）。
   # Intel 按设备 ID 代次分组（分组与 server.py 的 INTEL_IGPU_GENERATIONS 保持一致，改动需两边同步）；
   # AMD APU 核显单独一条（无 legacy-igd 路线），未收录的 Intel 新平台走兜底。
   IGPU_ID=$(printf '%s' "$(cat "/sys/bus/pci/devices/$PT_DEVICE/device" 2>/dev/null || true)" | sed 's/^0x//' | tr 'A-F' 'a-f')
   if [ "$VENDOR" = "0x8086" ]; then
     case "$IGPU_ID" in
       # Gen 2–5（i810 ~ G45/Pineview 早期平台 + Sandy Bridge ~ Broadwell，含 Ironlake / Bay Trail / Cherry Trail）：机型较老，legacy-igd 同样可用
       004*|010*|011*|012*|015*|016*|040*|041*|042*|0a0*|0a1*|0a2*|0a3*|0a4*|0a5*|0a6*|0a7*|0be*|0c0*|0c1*|0c2*|0d0*|0d1*|0d2*|0f3*|113*|160*|161*|162*|163*|22b*|256*|257*|258*|259*|277*|27a*|29a*|29b*|29c*|29d*|2a0*|2a1*|2a4*|2e0*|2e1*|2e2*|2e3*|2e4*|2e9*|357*|358*|712*|a00*)
         echo "[核显] 设备 ID $IGPU_ID：Gen 2–5（Sandy Bridge ~ Broadwell）"
         echo "       代次较老，legacy-igd 路线通常可用；若显示输出异常，改用「仅计算」纯直通" ;;
       # Gen 6–9.5（Skylake ~ Comet Lake，含 Apollo / Gemini / Cannon Lake）：legacy-igd 的典型可用代次
       190*|191*|192*|193*|590*|591*|592*|593*|3e9*|3ea*|87c*|9b2*|9b4*|9ba*|9bc*|9bd*|9be*|9bf*|0a8*|1a8*|318*|5a4*|5a5*|5a8*)
         echo "[核显] 设备 ID $IGPU_ID：Gen 6–9.5（Skylake ~ Comet Lake），legacy-igd 路线的典型可用代次" ;;
       # Gen 11–12（Ice Lake ~ Meteor Lake）：legacy-igd 通常无效
       8a5*|8a6*|8a7*|9a4*|9a6*|9a7*|9ac*|9ad*|9af*|454*|455*|457*|4c8*|4c9*|4e5*|4e6*|4e7*|462*|463*|468*|469*|46a*|46b*|46c*|46d*|984*|a72*|a78*|a7a*|7d4*|7d5*|7d6*|7dd*)
         echo "[提示] 设备 ID $IGPU_ID：Gen 11–12（Ice Lake ~ Alder/Raptor/Meteor Lake）"
         echo "       该代次 legacy-igd 通常无效，PVE 原生整卡直通成功率也偏低；建议先用「仅计算」纯直通试，"
         echo "       要分片共享需平台支持 SR-IOV（BIOS 开关 + 较新驱动）" ;;
       # Xe2（Lunar Lake 及更新平台的 Arc 架构核显）：legacy-igd 完全不适用
       64a*|64b*)
         echo "[提示] 设备 ID $IGPU_ID：Xe2 代（Lunar Lake 及更新平台，Arc 架构核显）"
         echo "       legacy-igd 不适用；整卡直通仅在部分平台可行，优先「仅计算」纯直通，分片需 SR-IOV" ;;
       *)
         echo "[提示] 设备 ID $IGPU_ID：未收录在已知代次表内（可能为更新的 Intel 平台）"
         echo "       已按通用「纯直通」处理；若启动失败，改用「仅计算」并确认宿主核显驱动已屏蔽（见「开启硬件直通」）" ;;
     esac
   else
     echo "[核显] 设备 ID $IGPU_ID：AMD APU 核显（vendor $VENDOR）"
     echo "       AMD 核显不支持 legacy-igd，PVE 下只能「整卡纯直通」，无原生分片方案；"
     echo "       Linux 虚机建议加 amdgpu.runpm=0 防掉卡（上方「为虚机适配内核参数」已按厂商自动写入）"
   fi
   if [ "$PT_IGPU_USE" = "display" ]; then
     if [ "$VENDOR" = "0x8086" ]; then
       backup_file "/etc/pve/qemu-server/${VMID}.conf"
       run "设置 VM 显示为 none 失败（legacy IGD 要求无模拟显卡）" qm set "$VMID" --vga none
       OPTS="${OPTS},legacy-igd=1"
       echo "[核显] 用途=显示输出：已设 VM 显示为 none（VNC 看不到画面），并附加 legacy-igd=1"
     else
       echo "[提示] 非 Intel 核显（AMD APU）不支持 legacy-igd，已按纯直通处理，保留模拟显卡与 VNC 控制台"
       echo "       AMD 核显没有「vga=none + legacy-igd」这类直出路线，画面需在虚机内把该核显当普通多显示设备使用"
     fi
   else
     echo "[核显] 用途=仅计算：纯直通（不带子项，保留模拟显卡与 VNC 控制台）"
     # 若此前为 legacy IGD 设过 vga=none，这里恢复默认显示，避免一直看不到画面
     if qm config "$VMID" 2>/dev/null | grep -qE '^vga: *none'; then
       echo "[提示] 检测到 VM 显示为 none（此前 legacy IGD 残留），已恢复为默认 std"
       qm set "$VMID" --vga std || true
     fi
   fi
 else
   # 独显：可选设为主显卡，输出接管宿主控制台
   [ "$PT_XVGA" = "yes" ] && OPTS="${OPTS},x-vga=1"
   # 已知直通易报 Code 43 的显卡型号：自动附加推荐参数（如 pcie=1），并给出提示
   GPU_VD=$(lspci -nn -s "$PT_DEVICE" 2>/dev/null | grep -oE '\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]' | tail -1 | tr -d '[]' | tr 'A-F' 'a-f')
   case "$GPU_VD" in
     1002:67e3)  # AMD Radeon Pro WX 4100 (Polaris)：不加 pcie=1 易报 Code 43
       case ",$OPTS," in
         *,pcie=*) ;;
         *) OPTS="${OPTS},pcie=1" ;;
       esac
       echo "[推荐] 检测到 AMD Radeon Pro WX 4100，已自动加 pcie=1 防 Code 43"
       echo "[推荐] 建议：设为主显卡(x-vga=1)，并给 VM CPU 加 hidden=1（qm set $VMID -cpu host,hidden=1）"
       ;;
   esac
   # 独显可选挂 vBIOS ROM：部分显卡直通后黑屏/无法初始化时需挂载转储的 vBIOS；
   # 白名单校验后拼 romfile=，文件不存在提前报错（避免 VM 开机才失败）
   if [ -n "$PT_ROMFILE" ]; then
     case "$PT_ROMFILE" in
       *[\\\"\']*|*/*|..*) echo "[错误] ROM 文件名含非法字符"; exit 1;;
     esac
     [ -f "/usr/share/kvm/$PT_ROMFILE" ] || { echo "[错误] /usr/share/kvm/$PT_ROMFILE 不存在，请先将 vBIOS ROM 文件放到宿主该目录"; exit 1; }
     OPTS="${OPTS},romfile=$PT_ROMFILE"
     echo "[vBIOS] 挂载 ROM: /usr/share/kvm/$PT_ROMFILE"
   fi
 fi
 # 隔离组整组闭环：同组内非桥接设备整组直通；主设备已含，其余 bus:device 以分号追加（不带选项）
 GROUP_DEVS=$(iommu_group_devices "$PT_DEVICE" || true)
 if [ -n "$GROUP_DEVS" ]; then
   # 直通安全闸门（在关机/写 hostpci 之前就把不安全的组合挡掉）：组内若含存储控制器
   # （AMD APU 平台上核显与 FCH SATA 常同组），整组直通会把它交给虚机 → 宿主机掉盘；
   # 虚机自己的 qcow2 若也在该控制器上，qm start 会直接报
   # "Could not read qcow2 header: Input/output error"（每次开机都会再掉一次盘）。
   # 组内共享 IOMMU 上下文，PVE/vfio 要求整组一起过（否则 "Group N is not viable"），
   # 不能只跳过存储控制器 —— 所以判定有风险（承载宿主在用盘 / 正被 vfio 占用无法确认）就拒绝执行。
   for d in $GROUP_DEVS; do
     pw_guard_storage_ctrl "$d" "整组直通（$PT_DEVICE 所在隔离组）" || exit 1
   done
   EXTRA=""
   for d in $GROUP_DEVS; do
     [ "$d" = "$MAIN_BD" ] && continue
     EXTRA="${EXTRA}${EXTRA:+;}$d"
   done
   if [ -n "$EXTRA" ]; then
     OPTS="${OPTS};${EXTRA}"
     echo "[隔离组] $PT_DEVICE 所在组含多个可直通设备，已自动整组直通：$MAIN_BD + $EXTRA"
     echo "       整组直通的代价：这些设备在宿主机上会一并消失（含同组 USB/音频等），VM 关机后自动归还"
   fi
 fi
 # 虚机侧内核参数（可选）：核显/显卡直通在 Linux 虚机里常因显示初始化卡死，这里在「写入 hostpci 之前」
 # 先通过 QEMU 代理把参数改好，使一次「关机→开机」两边同时生效（复用「调整虚机内核参数」工具的实现）。
 # 两个动作的电源状态要求相反 —— 写参数要「运行中」（代理只在跑着的虚机里），写 hostpci 要「已关机」——
 # 所以这里自行切换状态，两种情况都能一次跑完：
 #   虚机运行中 → 写参数 → 自动关机 → 写 hostpci → 末尾自动开机（保持原状，一次开关机）
 #   虚机已关机 → 临时开机 → 等代理就绪 → 写参数 → 关回去 → 写 hostpci（保持原状，下次开机生效）
 WAS_RUNNING=0   # 开始时是否运行中（结束时开回去）
 STARTED=0       # 是否为了拿到代理而主动开过机（需要关回去）
 AGENT_OK=0
 if [ "$PT_GUEST_FIX" = "1" ]; then
   # 取值 = 按显卡类型的默认值 + 常用参数（快捷）+ 自定义参数，去重后合并（三者是叠加关系）
   if [ "$PT_GTYPE" = "igpu" ]; then
     # 核显按厂商给默认值：Intel 关 GUC 防 i915 显示初始化卡死（QSV 仍可用）；AMD APU 关运行时电源管理防掉卡
     if [ "${VENDOR:-}" = "0x1002" ]; then
       GK="nomodeset amdgpu.runpm=0"
     else
       GK="nomodeset i915.enable_guc=0"
     fi
   else
     GK="nomodeset"
   fi
   GK=$(kargs_dedupe "$GK ${PT_GUEST_PRESET:-} ${PT_GUEST_KARGS:-}")
   QST=$(qm status "$VMID" 2>/dev/null | awk '{print $2}')
   if [ "$QST" = "running" ]; then WAS_RUNNING=1; fi
   echo
   echo "[虚机] 为 VM $VMID 追加内核参数: «$GK»（当前状态: ${QST:-未知}）"
   if [ "$WAS_RUNNING" = "1" ]; then
     AGENT_OK=1
   else
     echo "[虚机] 内核参数须由 QEMU 代理写入，虚机未运行：先临时开机，写入后关回去"
     if qm start "$VMID" >/dev/null 2>&1; then
       STARTED=1
       # 等代理就绪（开机到 agent 起来通常 10~60 秒）
       for i in $(seq 1 45); do
         sleep 2
         if qm agent "$VMID" ping >/dev/null 2>&1; then AGENT_OK=1; break; fi
       done
       if [ "$AGENT_OK" != "1" ]; then
         echo "[提示] 开机后 90 秒内 QEMU 代理未就绪（未装 qemu-guest-agent / VM「选项」未开 agent=1）：本次不写入参数"
       fi
     else
       STARTED=0
       echo "[提示] 临时开机失败：本次不写入参数（可手动开机后用「虚机/容器运维 → 调整虚机内核参数」处理）"
     fi
   fi
   if [ "$AGENT_OK" = "1" ]; then
     if guest_kargs_apply "$VMID" add "$GK"; then
       echo "[虚机] 参数已写入（下次启动生效）"
     else
       echo "[提示] 参数未写入；如虚机随后卡死/黑屏，请用「虚机/容器运维 → 调整虚机内核参数」工具，或到虚机内手工处理"
     fi
   fi
   # 写 hostpci 需要关机状态：这里统一关到位，避免「参数写好了、直通却写不进去」还得跑第二遍
   if [ "$WAS_RUNNING" = "1" ] || [ "$STARTED" = "1" ]; then
     echo "[虚机] 直通配置需关机状态，正在关机 VM $VMID（最长等 120 秒）..."
     qm shutdown "$VMID" >/dev/null 2>&1 || true
     WAIT=0
     while [ "$WAIT" -lt 120 ]; do
       sleep 2
       WAIT=$((WAIT + 2))
       if [ "$(qm status "$VMID" 2>/dev/null | awk '{print $2}')" = "stopped" ]; then break; fi
     done
     if [ "$(qm status "$VMID" 2>/dev/null | awk '{print $2}')" != "stopped" ]; then
       echo "[错误] VM $VMID 未在 120 秒内关机（虚机可能未响应 ACPI 关机）。请手动关机后重新执行本工具。"
       exit 1
     fi
     echo "[虚机] 已关机，继续写入直通配置"
   fi
 fi
 backup_file "/etc/pve/qemu-server/${VMID}.conf"
 # 纯直通值（丢掉主设备的子项，保留隔离组附带设备）：子项被当前 PVE 版本拒绝时兜底重试
 case "$OPTS" in
   *";"*) PLAIN_OPTS="$MAIN_BD;${OPTS#*;}" ;;
   *)     PLAIN_OPTS="$MAIN_BD" ;;
 esac
 # PCIe 设备不支持热插，运行中 VM 会在此报错，需先关机
 # 注：hostpci 的子项随 PVE 版本变化（核显 legacy-igd、旧的 x-igd-opregion、romfile、x-vga 等），
 # 版本不接受时报 "property is not defined in schema"；此处自动退回「只直通设备」再试一次
 if qm set "$VMID" -"$SLOT" "$OPTS" 2>&1; then
   :
 else
   echo "[提示] 当前 PVE 版本不接受附加子项（$OPTS），改为只直通设备本身重试：$PLAIN_OPTS"
   run "直通失败（VM 运行中请先关机）" qm set "$VMID" -"$SLOT" "$PLAIN_OPTS"
   OPTS="$PLAIN_OPTS"
   # 退回纯直通后 legacy-igd 没写进去，就不再需要 vga=none：恢复默认显示，保住 VNC 控制台
   if qm config "$VMID" 2>/dev/null | grep -qE '^vga: *none'; then
     echo "[提示] 已退回纯直通，恢复 VM 显示为默认 std（VNC 控制台可用）"
     qm set "$VMID" --vga std || true
   fi
 fi
 verify_qm_change "$VMID" "$SLOT" "$MAIN_BD"
 # 兜底提示：显示驱动已在前置拦截；此处仅对 vfio-pci 之外的其他驱动给弱提醒
 DRV=""
 [ -e "/sys/bus/pci/devices/$PT_DEVICE/driver" ] && DRV=$(basename "$(readlink -f "/sys/bus/pci/devices/$PT_DEVICE/driver")")
 if [ -n "$DRV" ] && [ "$DRV" != "vfio-pci" ]; then
   echo "[提示] 宿主当前由 $DRV 驱动占用该显卡，若 VM 启动后无法使用，需屏蔽该驱动后重启宿主"
 fi
 echo "[完成] PCIe $GTXT $OPTS 已直通到 VM $VMID 的 $SLOT"
 echo "[重要] PCIe 直通不支持热插，需先「关机」再「开机」才真正生效：qm shutdown $VMID && qm start $VMID"
 # 核显「仅计算」的常见坑：Linux 虚机开机卡在显示初始化（Intel i915 的 DC state mismatch、
 # AMD 直通后掉卡）。虚机不做显示初始化即可绕开，渲染节点 renderD128 仍在（QSV/VAAPI 可用）。
 if [ "$PT_GTYPE" = "igpu" ] && [ "$PT_IGPU_USE" != "display" ]; then
   echo
   if [ "${VENDOR:-}" = "0x1002" ]; then
     echo "[提示] AMD 核显：Linux 虚机建议内核参数 nomodeset（必要时 amdgpu.runpm=0 防掉卡），"
     echo "       虚机侧不做显示初始化，避开直通后掉卡/黑屏。"
     echo "       本次已按上方「为虚机适配内核参数」的设置自动写入（未勾选或代理不可用时需手工处理）。"
     echo "       手工调整：虚机/容器运维 → 调整虚机内核参数；验证：ls -l /dev/dri/ 应能看到渲染节点"
   else
     echo "[提示] Linux 虚机需在内核参数加 nomodeset：i915 不再做显示初始化，避开 DC state mismatch 卡死，"
     echo "       而 /dev/dri/renderD128 仍在，QSV 照常可用。"
     echo "       本次已按上方「为虚机适配内核参数」的设置自动写入（未勾选或代理不可用时需手工处理）。"
     echo "       手工调整：虚机/容器运维 → 调整虚机内核参数；验证：ls -l /dev/dri/ 应有 renderD128；vainfo | head"
   fi
   fi
   # 本轮为写内核参数关过机（开始时是运行状态）：自动开回来，一次开关机完成「参数 + 直通」
   if [ "$WAS_RUNNING" = "1" ]; then
   echo
   echo "[虚机] 正在开机 VM $VMID（内核参数与直通配置同时生效）..."
   run "开机失败，请手动开机" qm start "$VMID"
   fi
   exit 0
   fi

if [ "$PT_ACTION" = "del" ]; then
 VMID="$PT_VMID"
 qm status "$VMID" >/dev/null 2>&1 || { echo "[错误] VM $VMID 不存在"; exit 1; }
 # 直通时是整组一起写入同一槽位的，移除时按槽位整组删除（附带设备一并移除，不留孤儿）
 # 匹配用 bus:device（去 .function 后缀），与 add 写入的缩短语法一致
 MAIN_BD="${PT_DEVICE%.*}"
 SLOT=$(qm config "$VMID" | grep "^hostpci[0-9]*:.*${MAIN_BD}" | head -1 | cut -d: -f1 || true)
 [ -n "$SLOT" ] || { echo "[错误] 显卡 $PT_DEVICE 未直通给 VM $VMID"; exit 1; }
 HDVAL=$(qm config "$VMID" | grep "^${SLOT}:" | head -1 || true)
 backup_file "/etc/pve/qemu-server/${VMID}.conf"
 run "移除失败（VM 运行中请先关机）" qm set "$VMID" -delete "$SLOT"
 verify_qm_change "$VMID" "$SLOT" "$MAIN_BD" neg
 echo "[完成] 已从 VM $VMID 移除显卡直通（原槽位 $SLOT，含同组附带设备）"
 # 该直通若是 legacy IGD（添加时把 vga 设成了 none），移除后必须恢复默认显示，否则 VNC 一直黑屏
 case "$HDVAL" in
   *legacy-igd*)
     if qm config "$VMID" 2>/dev/null | grep -qE '^vga: *none'; then
       echo "[提示] 该直通使用过 legacy-igd，已将 VM 显示恢复为默认 std（VNC 控制台可用）"
       qm set "$VMID" --vga std || true
     fi ;;
 esac
 exit 0
fi

echo "[错误] 未知操作: $PT_ACTION"; exit 1
