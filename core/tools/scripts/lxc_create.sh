
TPL="$PT_TEMPLATE"
[ -n "$TPL" ] || { echo "[错误] 请选择要部署的 LXC 模板"; exit 1; }
# 集群失去仲裁时 /etc/pve 只读，pct create/set 等写操作必然失败；先探一次给出可操作指引
pw_quorum_precheck
case "$TPL" in
 *::*) TPL_STORAGE="${TPL%%::*}"; TPL_NAME="${TPL##*::}" ;;
 *)    TPL_STORAGE=""; TPL_NAME="$TPL" ;;
esac
# 确认模板文件存在（避免 pct create 报模板不存在；推荐下载到共享存储，集群各节点均可取用）
TPL_PATH=""
if [ -n "$TPL_STORAGE" ]; then
  TPL_ROOT=$(pw_storage_root "$TPL_STORAGE")
  [ -n "$TPL_ROOT" ] || { echo "[错误] 无法解析存储 $TPL_STORAGE 根路径"; exit 1; }
  TPL_PATH="$TPL_ROOT/template/cache/$TPL_NAME"
  [ -f "$TPL_PATH" ] || { echo "[错误] 模板文件不存在: $TPL_PATH（请先到「获取 LXC 模板」下载，并确认所选节点可见该存储）"; exit 1; }
  TPL_REF="$TPL_STORAGE:vztmpl/$TPL_NAME"
else
  TPL_REF="local:vztmpl/$TPL_NAME"
fi

# ===== 本机模板缓存（与虚机建机/云镜像/还原共用 pw_cache_use）=====
# 建 CT 时 pct 要把模板 tar 整包读出解包，模板在共享存储上时这一步每次建机都走一遍网络；
# 首次拉到本机盘后本节点后续建机直读本地（批量建机收益最明显）。
# 实测（PVE 9.2.20）pct create 的 ostemplate 接受本地绝对路径，与存储引用等价；
# 为防个别版本/环境不认，下面的创建步骤带「失败即销毁半成品、退回存储引用重试」的兜底。
TPL_USE="$TPL_REF"
if [ -n "$TPL_PATH" ] && [ -f "$TPL_PATH" ]; then
  _cres=$(pw_cache_use "$TPL_PATH" 模板)
  _cst=$(printf '%s\n' "$_cres" | sed -n 1p)
  _cf=$(printf '%s\n' "$_cres" | sed -n 2p)
  case "$_cst" in
    # 只有确实生成了本机副本（hit/fetched）才改成传本地路径，其余一律保持原来的存储引用形态：
    #   local   → 源本就在本机盘上（如 pve01 这种共享存储导出端），缓存不参与，没必要改形态
    #   其它    → 缓存不可用，回显的是源路径而非引用，更不该拿它去换掉一定能用的引用
    hit|fetched) if [ -n "$_cf" ] && [ -f "$_cf" ]; then TPL_USE="$_cf"; fi ;;
  esac
fi

# 首台 CTID（留空自动分配；手动指定已存在则报错）
FIRST_CTID="$PT_NEWID"
if [ -z "$FIRST_CTID" ]; then
  FIRST_CTID=100
  # CTID 与 VMID 同命名空间、集群唯一：用集群级视图判定占用
  while vmid_state_cluster "$FIRST_CTID" >/dev/null; do
    FIRST_CTID=$((FIRST_CTID + 1))
  done
else
  if [ -n "$(vmid_state_cluster "$FIRST_CTID")" ]; then
    echo "[错误] CTID $FIRST_CTID 已存在（集群内），请换一个或留空自动分配"; exit 1
  fi
fi

# ===== 批量建机参数 =====
BATCH="$PT_BATCH"
if [ -n "$BATCH" ]; then
  BATCH_COUNT="${PT_BATCH_COUNT:-1}"
  case "$BATCH_COUNT" in ''|*[!0-9]*) echo "[错误] 创建数量必须为正整数"; exit 1 ;; esac
  [ "$BATCH_COUNT" -ge 1 ] 2>/dev/null || { echo "[错误] 创建数量至少为 1"; exit 1; }
  [ "$BATCH_COUNT" -gt 50 ] && { echo "[错误] 单次批量上限 50 台，请分批执行"; exit 1; }
  echo "[提示] 批量模式已统一使用 DHCP，忽略静态 IP / 网关 / DNS（避免多台抢同一地址）"
else
  BATCH_COUNT=1
fi

# 建机封套：pct create 在部分 PVE 版本（实测 pve01 / pve-manager 9.2.20 + libpve-common-perl
# 9.2.2）上，建机命令一旦涉及 nameserver 就会在参数校验阶段 400：
#   nameserver: invalid format - value does not look like a valid IP address
# 与取值无关（带单 IP / 多 IP / 完全不带给 --nameserver 都会同样失败），且宿主 /etc/resolv.conf
# （nameserver 192.168.1.1，合法）并非元凶 —— 根因是 pct CLI 这条路径的 nameserver 处理坏了，
# 而同节点的 REST API（pvesh create）完全正常（已逐项验证）。故：pct create 若因 nameserver 被拒，
# 自动回退到 pvesh create（REST API），rootfs 大小用 pct resize 补，DNS 由建机命令写入首个地址、
# 完整列表随后由 pw_set_dns 再写一次。回退时 pct 的原始报错整段不透传（「400 Parameter verification
# failed.」+ nameserver 报错行 + 用法块），只留一行中性提示 —— 否则用户看到报错会以为建机失败，
# 而实际上紧接着那次 REST 建机是成功的（真实失败仍然原样透传，见下面 case 的兜底分支）。
# 另外：建机命令顺带带上 root 密码（--password，引号单独传参），正常版本建完即可登录；若该参数
# 也被 CLI / API 拒绝，自动去掉重试一次（不为了「顺手带密码」把建机搞挂），密码再由 deploy_one 的
# [5/5] 在容器内 chpasswd 落定，用户始终能拿到一个密码可用的容器。
pct_create_try() {
 local tpl="$1"; shift
 local out rc=0 node pvargs
 # 建机命令尽量一次把 root 密码也带上（pct create / REST API 都支持 password）：密码单独引号传参，
 # 不塞进 $ARGS 字符串（密码含空格/引号时会被分词）；个别版本若连 password 参数也拒，就去掉重试一次，
 # 交给 [5/5] 在容器内 chpasswd 落定 —— 不为了「顺手带密码」把建机搞挂。
 out=$(pct create "$NEWID" "$tpl" "$@" --nameserver "$DNS1" --password "$PW" 2>&1) || rc=$?
 if [ "$rc" -ne 0 ]; then
   case "$out" in
     # nameserver 为主因时不做密码重试：pct 的用法块里也含 --password，若先匹配 password 会误判成
     # 「密码参数被拒」而白重试一次，这里交给下面的 REST 回退处理
     *nameserver*) : ;;
     # 参数校验类失败才试「去掉密码再来一次」（校验发生在建机之前，重试不会撞「已存在」）；
     # 其它失败照原样返回，别用一次无关重试盖掉真实报错
     *password*|*unknown*|*"Parameter verification failed"*)
       echo "[提示] pct CLI 不接受建机时传密码，改为建好后单独设置 ..."
       rc=0   # 重试前必须清零：重试成功时 `|| rc=$?` 不执行，残留非零会被当成仍失败
       out=$(pct create "$NEWID" "$tpl" "$@" --nameserver "$DNS1" 2>&1) || rc=$? ;;
   esac
 fi
 if [ "$rc" -eq 0 ]; then printf '%s\n' "$out"; return 0; fi
 case "$out" in
   *nameserver*)
     # 原始报错整段吞掉（含「400 Parameter verification failed.」、nameserver 报错行与 pct 用法块）：
     # 它们只是第一次尝试留下的痕迹，紧接着就改用 REST API 建成，透传进日志只会让用户以为建机失败。
     echo "[提示] pct CLI 的 DNS 参数在 PVE 9.2 上存在已知校验问题，已自动改用 REST API（pvesh）建机 ..." ;;
   *) printf '%s\n' "$out"; return "$rc" ;;
 esac
 rc=0
 node=$(hostname -s 2>/dev/null || hostname 2>/dev/null)
 # pvesh 选项名与 pct 略有差异：--name→--hostname；rootfs 大小 API 不直接接受 storage:size，
 # 故去掉 --rootfs/--storage 后统一用 --storage 重建，大小稍后用 pct resize 补。
 # ostemplate 一律用存储引用 $TPL_REF（REST API 对本地缓存绝对路径支持不如 pct 稳）。
 pvargs=$(printf '%s ' "$@" | sed -e 's/--name /--hostname /g' -e 's/--rootfs [^ ]*//g' -e 's/--storage [^ ]*//g')
 out=$(pvesh create "/nodes/$node/lxc" --vmid "$NEWID" --ostemplate "$TPL_REF" $pvargs --storage "$STORAGE" --nameserver "$DNS1" --password "$PW" 2>&1) || rc=$?
 if [ "$rc" -ne 0 ]; then
   case "$out" in
     # 同 pct 侧：只对「参数校验类」失败做去掉密码的重试；错误里可能只报 "Parameter verification
     # failed" 而不点名 password，所以三种特征都算（重试同样发生在建机之前，不会撞「已存在」）
     *password*|*unknown*|*"Parameter verification failed"*)
       echo "[提示] REST API 不接受建机时传密码，改为建好后单独设置 ..."
       rc=0   # 同 pct 侧：重试前清零，避免重试成功却因残留 rc 被判成失败
       out=$(pvesh create "/nodes/$node/lxc" --vmid "$NEWID" --ostemplate "$TPL_REF" $pvargs --storage "$STORAGE" --nameserver "$DNS1" 2>&1) || rc=$? ;;
   esac
 fi
 printf '%s\n' "$out"
 if [ "$rc" -eq 0 ]; then
   echo "[提示] 已用 REST API 建机（DNS 同步写入: $DNS1）"
   if [ -n "$PT_ROOTFSSIZE" ]; then
     if pct resize "$NEWID" rootfs "$PT_ROOTFSSIZE" >/dev/null 2>&1; then
       echo "[提示] 已扩容 rootfs 至 $PT_ROOTFSSIZE"
     else
       echo "[警告] rootfs 扩容失败（当前为默认大小，可在 GUI 调整）"
     fi
   fi
 else
   echo "[错误] REST API 建机亦失败"
 fi
 return "$rc"
}

# 单台部署逻辑（包成函数供批量循环调用）
deploy_one() {
NEWID="$1"; NEWNAME="$2"
DEPLOY_DONE=0
EXISTED=0; pct status "$NEWID" >/dev/null 2>&1 && EXISTED=1
_cleanup_on_fail() {
  if [ "$DEPLOY_DONE" != "1" ] && [ "$EXISTED" = "0" ] && pct status "$NEWID" >/dev/null 2>&1; then
    echo "[清理] 部署未完成，自动销毁残缺 CT $NEWID ..."
    pct stop "$NEWID" >/dev/null 2>&1
    if pct destroy "$NEWID" >/dev/null 2>&1; then echo "[清理] 已销毁残缺 CT $NEWID"; else echo "[警告] 残缺 CT $NEWID 自动销毁失败，请手动: pct destroy $NEWID"; fi
  fi
}
trap _cleanup_on_fail EXIT

# 主机名：空则由模板名推导 ct-系统-编号
if [ -z "$NEWNAME" ]; then
  DIST=$(echo "$TPL_NAME" | tr '[:upper:]' '[:lower:]' | sed -E 's/[-_][0-9].*//; s/\.(tar|gz|xz|zst).*//')
  NEWNAME="ct-${DIST}-${NEWID}"
fi
STORAGE="${PT_STORAGE:-local}"
CORES="${PT_CORES:-1}"
MEM="${PT_MEMORY:-512}"
BRIDGE="${PT_BRIDGE:-vmbr0}"
# root 密码（默认 123456）：必须在建机之前就定好 —— 建机命令本身要带 password 参数（见
# pct_create_try），最后的 [5/5] 再以容器内 chpasswd 确认落定（该链在此类环境上未必生效）。
PW="${PT_PASSWORD:-123456}"

# DNS：用户未填则默认公共 DNS（避免容器跟随节点 /etc/resolv.conf 的 DNS）。
# 一律经 pw_dns_list 规范化（逗号/分号/空格 → 空格分隔，并剔掉控制字符）。
if [ -n "$PT_DNS" ]; then DNS="$PT_DNS"; else DNS="223.5.5.5,119.29.29.29"; fi
DNS=$(pw_dns_list "$DNS")
# 建机命令只带首个地址（单值最不易被参数校验拒），完整列表随后由 pw_set_dns 写入
DNS1=$(pw_dns_first "$DNS")

echo "[1/5] 创建 CT $NEWID（$NEWNAME）..."
# 网络：net0 只拼一次，避免重复 --net0 互相覆盖；IP 配置留空时默认 DHCP
# （与虚机建机一致），确保容器 eth0 一定带地址配置、能拿到 IP。否则（net0 无 ip=）
# 容器网卡处于未配置状态，eth0 不会有地址，概览页也就显示不出 IP。
NET="name=eth0,bridge=$BRIDGE"
if [ -n "$PT_IP" ]; then
  NET="$NET,ip=$PT_IP"
else
  NET="$NET,ip=dhcp"
fi
[ -n "$PT_GW" ] && NET="$NET,gw=$PT_GW"
if [ -n "$PT_ROOTFSSIZE" ]; then
  ARGS="--name $NEWNAME --memory $MEM --cores $CORES --net0 $NET --rootfs ${STORAGE}:${PT_ROOTFSSIZE}"
else
  ARGS="--name $NEWNAME --memory $MEM --cores $CORES --net0 $NET --storage $STORAGE"
fi
# 建机命令（在 pct_create_try 内部）显式带首个 DNS 地址；若 pct CLI 因 nameserver 校验拒绝，
# 会自动回退到 pvesh（REST API）建机——这是 PVE 9.2 上 pct 建机的已知问题，与取值无关
# （详见上方 pct_create_try 注释）。完整 DNS 列表在建好后由 pw_set_dns 再写一次
# （见下方 [2/5]，内含 pvesh / 配置文件两级兜底）。
if [ -n "$PT_UNPRIVILEGED" ]; then ARGS="$ARGS --unprivileged 1"; else ARGS="$ARGS --unprivileged 0"; fi
# 嵌套容器：勾选后开启 nesting + keyctl（容器内可跑 Docker / 嵌套 LXC）；--features 同时被
# pct 与回退的 pvesh（REST API）接受（pct_create_try 内部已做参数名映射，--features 原样透传）
if [ -n "$PT_FEATURES" ]; then ARGS="$ARGS --features nesting=1,keyctl=1"; fi
# IP / 网关已并入上方 NET 构建（net0 仅出现一次）；此处不再追加，避免重复 --net0。
# 优先用本机缓存模板（本地绝对路径）建机；万一该环境不认本地路径，先销毁半成品再退回
# 存储引用重试一次 —— 不能因为「想省一次网络读」反而把建机搞挂（半成品不清会撞「已存在」）
CT_CREATED=0
if [ "$TPL_USE" != "$TPL_REF" ]; then
  if pct_create_try "$TPL_USE" $ARGS; then
    CT_CREATED=1
  else
    echo "[提示] 用本机缓存模板创建失败，清理半成品后改用共享存储模板重试 ..."
    pct stop "$NEWID" >/dev/null 2>&1 || true
    pct destroy "$NEWID" >/dev/null 2>&1 || true
  fi
fi
[ "$CT_CREATED" = "1" ] || run "创建 CT 失败" pct_create_try "$TPL_REF" $ARGS

# DNS：建机命令只带了首个地址（见 pct_create_try），完整列表统一在这里写入；
# pw_set_dns 逐级兜底（pct set → pvesh API → 直写配置），全部失败也只警告
if [ -n "$DNS" ]; then
  echo "[2/5] 写入 DNS ..."
  pw_set_dns pct "$NEWID" "$DNS" || true
fi
# 数据盘（可选）：创建后挂载 mp0 到 /data（需同时给「数据盘存储」与「数据盘大小」）
if [ -n "$PT_DATASIZE" ] && [ -n "$PT_DATASTORAGE" ]; then
  echo "[3/5] 挂载数据盘 ${PT_DATASTORAGE}:${PT_DATASIZE} → /data ..."
  if pct set "$NEWID" --mp0 "${PT_DATASTORAGE}:${PT_DATASIZE},mp=/data" >/dev/null 2>&1; then
    echo "[3/5] 已挂载数据盘"
  else
    echo "[警告] 数据盘挂载失败（请确认存储 ${PT_DATASTORAGE} 支持 rootdir 内容类型，或空间不足）"
  fi
elif [ -n "$PT_DATASIZE" ] && [ -z "$PT_DATASTORAGE" ]; then
  echo "[提示] 已填数据盘大小但未选数据盘存储，跳过数据盘创建"
fi

# 部署结果摘要
echo ""
echo "===== 部署结果 ====="
CFG=$(pct config "$NEWID" 2>/dev/null || true)
F_NAME=$(echo "$CFG" | awk -F': ' '/^name:/{print $2; exit}')
F_CORES=$(echo "$CFG" | awk -F': ' '/^cores:/{print $2; exit}')
F_MEM=$(echo "$CFG" | awk -F': ' '/^memory:/{print $2; exit}')
echo "CTID: $NEWID"
echo "名称: ${F_NAME:-$NEWNAME}"
echo "CPU 核数: ${F_CORES:-$CORES} 核"
echo "内存: ${F_MEM:-$MEM} MB"
[ -n "$PT_UNPRIVILEGED" ] && echo "容器类型: 非特权容器" || echo "容器类型: 特权容器"
NETLINE=$(echo "$CFG" | grep -E '^net[0-9]+:' | head -1)
echo "网络: ${NETLINE:-（默认）}"
DATALINE=$(echo "$CFG" | grep -E '^mp[0-9]+:' | head -1)
[ -n "$DATALINE" ] && echo "数据盘: ${DATALINE}" || echo "数据盘: 无"
echo "登录密码: $PW"
DEPLOY_DONE=1
echo "[完成] 已创建 CT $NEWID（$NEWNAME）"
if [ "$PT_START" = "1" ]; then
  echo "[4/5] 启动容器 ..."
  run "启动失败" pct start "$NEWID"
  echo "[完成] CT $NEWID 已启动"
fi
# [5/5] root 密码：以容器内 chpasswd 落定（建机参数里的 --password 未必生效，见 pct_create_try 注释）。
# 勾了「部署后启动」时容器已经在跑 → 一次 exec 设密即可，不再像以前那样为设密临时启停容器；
# 没勾选启动时才退回 pw_set_password 的多级兜底（需临时拉起容器，设完再停回原状）。
echo "[5/5] 设置 root 密码 ..."
if [ "$PT_START" = "1" ]; then
  # 刚启动的容器 exec 可能还进不去：先等它就绪（最多 15s），避免设密失败
  _i=0
  while [ "$_i" -lt 15 ]; do pct exec "$NEWID" -- true >/dev/null 2>&1 && break; sleep 1; _i=$((_i + 1)); done
fi
pw_set_password "$NEWID" "$PW" || true
# 统一时区：留空=跟随宿主机（容器与宿主共享内核时钟，这里设置的只是时区）
pw_container_tz "$NEWID" "${PT_TZ:-}"
true
}  # ===== end deploy_one =====

# ===== 批量循环（子 shell 调用，单台失败不中断其余）=====
OK=0; FAIL=0; CREATED=""; PREV=""
IP_BASE="$PT_IP"
for i in $(seq 1 "$BATCH_COUNT"); do
  if [ "$i" -eq 1 ]; then CUR="$FIRST_CTID"
  else CUR=$((PREV + 1)); while vmid_state_cluster "$CUR" >/dev/null; do CUR=$((CUR + 1)); done; fi
 # 批量模式：填了静态 IP 基址则逐台递增末段（网关/DNS 共用）；未填则保持 DHCP
 if [ "$BATCH_COUNT" -gt 1 ] && [ -n "$PT_IP" ]; then
   PT_IP=$(pw_inc_ip "$IP_BASE" $((i - 1))) || exit 1
 fi
  if [ "$BATCH_COUNT" -eq 1 ]; then THIS_NAME="$PT_NEWNAME"; else THIS_NAME=""; fi
  echo ""; echo "########## 批量进度 [$i/$BATCH_COUNT] → CTID $CUR ##########"
  if ( deploy_one "$CUR" "$THIS_NAME" ); then OK=$((OK + 1)); CREATED="$CREATED $CUR"
  else FAIL=$((FAIL + 1)); echo "[警告] 第 $i 台（CTID $CUR）部署失败，继续下一台"; fi
  PREV="$CUR"
done
echo ""; echo "===== 批量建机汇总 ====="
echo "成功: $OK 台，失败: $FAIL 台"
[ -n "$CREATED" ] && echo "已创建 CTID:$CREATED"
if [ "$BATCH_COUNT" -eq 1 ] && [ "$FAIL" -gt 0 ]; then exit 1; fi
true
