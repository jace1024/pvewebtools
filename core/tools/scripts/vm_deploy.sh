
# 别名表未命中时的兜底标签：由名称（模板文件名 / VM 名 / 云镜像名）清洗出合法主机名前缀。
# 原先一律回落 vm/linux，未收录的系统（iKuai8、rhel、deepin 等）会被命名成 temp-vm-编号，
# 看起来就像「系统没识别出来」。这里改为取名称里第一个有信息量的词：跳过纯数字段
# （时间戳、位宽）、架构 / 引导方式 / 无意义词，全都不合用时才回落 vm。
deploy_label_fallback() {
 local s=$(echo "$1" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-')
 local seg
 for seg in $(echo "$s" | tr '-' ' '); do
   case "$seg" in ''|*[!a-z0-9]*) continue ;; esac   # 空段 / 含非字母数字
   case "$seg" in *[a-z]*) ;; *) continue ;; esac    # 纯数字段（时间戳、位宽）
   [ ${#seg} -ge 2 ] || continue                     # 单字符段没有信息量
   case "$seg" in
     x86|i386|i686|amd64|aarch64|arm64|arm|uefi|bios|efi|legacy|vm|img|image|template|disk|tmp|test) continue ;;
   esac
   # 主机名总长上限 63（temp- 前缀 + 版本 + -编号 还要占位），前缀限 40 字符
   echo "${seg:0:40}"; return 0
 done
 echo "vm"; return 0
}

TPL="$PT_TEMPLATE"
[ -n "$TPL" ] || { echo "[错误] 请选择要部署的模板"; exit 1; }
# 集群失去仲裁时 /etc/pve 只读，qmrestore/qm set 等写操作必然失败；先探一次给出可操作指引
pw_quorum_precheck

# 销毁残留的 VM（清理上次失败留下的半成品），$1=VMID
cleanup_guest() {
 local id="$1"
 if qm status "$id" >/dev/null 2>&1; then
   if qm destroy "$id" --purge 2>/dev/null; then
     echo "[清理] 已销毁残留 VM $id"
   else
     # 只说「销毁失败」会让人以为磁盘还在。两种成因说清，并给出手动清理入口：
     # 1) 恢复中途失败时磁盘已被 PVE 回收（日志里那句 disk image '...' does not exist），
     #    配置却留在 /etc/pve —— 集群可写后再执行一次即可；
     # 2) /etc/pve 只读（集群失去仲裁），销毁同样被拒，恢复仲裁后自动可清。
     echo "[警告] 销毁残留 VM $id 失败（磁盘可能已由 PVE 回收，仅剩配置）"
     echo "       集群恢复可写后执行：qm destroy $id --purge"
   fi
 fi
 # 半成品虚机连同它的 Cloud-Init 片段一起清掉（片段是普通文件，qm destroy 不管；见 pw_snippet_rm）
 pw_snippet_rm "$id"
}

# 解析模板来源：vmid:<数字> = PVE 模板 VM；file:<路径> = 上传的模板文件（.vma）
case "$TPL" in
 vmid:*) SRC="${TPL#vmid:}"; SRC_KIND=vmid ;;
 file:*) SRC_FILE="${TPL#file:}"; SRC_KIND=file ;;
 *) echo "[错误] 无法识别的模板来源: $TPL"; exit 1 ;;
esac

# 由模板名/文件名推导发行版标签（小写），用于生成标准主机名 系统-VMID
deploy_distro_label() {
 local n=$(echo "$1" | tr '[:upper:]' '[:lower:]')
 set -- centos:centos rocky:rocky alma:alma fedora:fedora \
        debian:debian ubuntu:ubuntu opensuse:opensuse suse:opensuse \
        alpine:alpine arch:arch gentoo:gentoo oracle:oracle \
        freebsd:freebsd openbsd:openbsd netbsd:netbsd \
        windows:win win:win openwrt:openwrt ikuai:ikuai \
        istoreos:istoreos fnos:fnos unraid:unraid truenas:truenas \
        pfsense:pfsense vyos:vyos mikrotik:routeros routeros:routeros bionic:ubuntu focal:ubuntu jammy:ubuntu noble:ubuntu xenial:ubuntu trusty:ubuntu oracular:ubuntu plucky:ubuntu precise:ubuntu bookworm:debian bullseye:debian buster:debian stretch:debian jessie:debian trixie:debian forky:debian openeuler:openeuler anolis:anolis
 local spec k v
 for spec in "$@"; do
   k=${spec%%:*}; v=${spec##*:}
   case "$n" in *"$k"*) echo "$v"; return 0;; esac
 done
 # 别名表未命中：由模板名清洗出前缀，别一律回落 vm（见 DEPLOY_LABEL_HELPERS）
 deploy_label_fallback "$n"; return 0
}
deploy_distro_version() {
  # 由镜像/模板名推导版本号(纯数字,去点): 优先显式版本(ubuntu-22.04/debian-12), 其次 Ubuntu/Debian 代号映射
  local n=$(echo "$1" | tr "[:upper:]" "[:lower:]")
  local v
  v=$(echo "$n" | grep -oE '(ubuntu|debian|centos|rhel|rocky|almalinux|alma|fedora|opensuse|suse|oracle|openeuler|anolis|ikuai)[-_]?[0-9]+([.][0-9]+)?' | head -1 | grep -oE '[0-9]+([.][0-9]+)?$')
  if [ -n "$v" ]; then echo "$v" | tr -d "."; return 0; fi
  case "$n" in
    *precise*) echo 1204;; *trusty*) echo 1404;; *xenial*) echo 1604;;
    *bionic*) echo 1804;; *focal*) echo 2004;; *jammy*) echo 2204;;
    *noble*) echo 2404;; *oracular*) echo 2410;; *plucky*) echo 2504;;
    *jessie*) echo 8;; *stretch*) echo 9;; *buster*) echo 10;;
    *bullseye*) echo 11;; *bookworm*) echo 12;; *trixie*) echo 13;; *forky*) echo 14;;
    *) echo "";;
  esac
}

# 首台 VMID（沿用原严格逻辑：手动指定已存在→stopped 清理重建；running 报错；留空→自动顺次）
FIRST_VMID="$PT_NEWID"
if [ -z "$FIRST_VMID" ]; then
 if [ "$SRC_KIND" = "vmid" ]; then
   FIRST_VMID=$((SRC + 1))
 else
   FIRST_VMID=100
 fi
 # VMID 集群唯一：用集群级视图判定占用
 while vmid_state_cluster "$FIRST_VMID" >/dev/null; do
   FIRST_VMID=$((FIRST_VMID + 1))
 done
else
 # 集群级已占用判定（VMID 集群唯一，须看全局而非本机 qm status）
 ST=$(vmid_state_cluster "$FIRST_VMID")
 if [ -n "$ST" ]; then
   # 本地能查到状态才做 stopped→销毁重建；跨节点占用（本机查不到）直接报错让用户先释放
   if qm status "$FIRST_VMID" >/dev/null 2>&1 || pct status "$FIRST_VMID" >/dev/null 2>&1; then
     if [ "$ST" = "running" ]; then
       echo "[错误] VMID $FIRST_VMID 已存在且运行中，请先停止/删除后再试"; exit 1
     else
       echo "[清理] 目标 VMID $FIRST_VMID 已存在（stopped），自动销毁后重新部署 ..."
       cleanup_guest "$FIRST_VMID"
     fi
   else
     echo "[错误] VMID $FIRST_VMID 在集群内已被占用（位于其它节点），请用其它编号或先释放"; exit 1
   fi
 fi
fi

# ===== 批量建机参数 =====
# 以「批量创建」复选框决定是否批量：勾选后用 batch_count（留空=1，上限 50）；未勾选则单台
BATCH="$PT_BATCH"
if [ -n "$BATCH" ]; then
 BATCH_COUNT="${PT_BATCH_COUNT:-1}"
 case "$BATCH_COUNT" in
   ''|*[!0-9]*) echo "[错误] 创建数量必须为正整数"; exit 1 ;;
 esac
 [ "$BATCH_COUNT" -ge 1 ] 2>/dev/null || { echo "[错误] 创建数量至少为 1"; exit 1; }
 if [ "$BATCH_COUNT" -gt 50 ]; then echo "[错误] 单次批量上限 50 台，请分批执行"; exit 1; fi
 echo "[提示] 批量模式：若填了静态 IP，将以末段为基址逐台递增分配（网关/DNS 共用）；未填静态 IP 则走 DHCP（无 DHCP 环境请填静态 IP 基址，如 192.168.1.10/24）"
else
 BATCH_COUNT=1
fi

# 单台部署逻辑（包成函数供批量循环调用）；参数：本次 VMID、名称（空则由函数内按模板生成）
deploy_one() {
NEWID="$1"
NEWNAME="$2"

# 分步计时：定位模板建机慢在哪一阶段（本阶段耗时 / 累计耗时）
_t_start=$(date +%s); _t_phase=$_t_start
_ph() {
  local _now=$(date +%s)
  local _d=$(( _now - _t_phase )); local _tot=$(( _now - _t_start ))
  echo "[计时] $1：本阶段 ${_d} 秒，累计 ${_tot} 秒（$((_tot/60)) 分 $((_tot%60)) 秒）"
  _t_phase=$_now
}

# 失败自动清理：本函数异常退出（含 run 的 exit 1）且 VM 是本次新建/克隆的，则销毁残缺 VM，
# 避免留下半成品。创建成功只设 DEPLOY_DONE=1，trap 不再销毁。
DEPLOY_DONE=0
VM_EXISTED_BEFORE=0; qm status "$NEWID" >/dev/null 2>&1 && VM_EXISTED_BEFORE=1
_cleanup_on_fail() {
  [ "$DEPLOY_DONE" != "1" ] || return 0
  # 失败即归因：恢复/写配置这一步失败最常见的原因就是集群失去仲裁（/etc/pve 只读），
  # 而日志里只会留下 PVE 的原始报错（unable to open file '...103.conf.tmp.18546' - Permission denied），
  # 极容易被误判成模板/存储问题。这里探一次当前可写性，命中就打与前置检测一致的指引。
  pw_quorum_hint_if_readonly || true
  if [ "$VM_EXISTED_BEFORE" = "0" ] && qm status "$NEWID" >/dev/null 2>&1; then
    echo "[清理] 部署未完成，自动销毁残缺 VM $NEWID ..."
    cleanup_guest "$NEWID"
  fi
  return 0
}
trap _cleanup_on_fail EXIT

echo "===== 模板快速建机 ====="

# 目标存储默认 local（未选时兜底）
PT_STORAGE="${PT_STORAGE:-local}"

if [ "$SRC_KIND" = "vmid" ]; then
 # ---- 来源一：PVE 模板 VM，直接 qm clone --full 完整克隆 ----
 qm config "$SRC" >/dev/null 2>&1 || { echo "[错误] 模板 VM $SRC 不存在"; exit 1; }
 qm config "$SRC" | grep -qE '^template:[[:space:]]*1' || { echo "[错误] VM $SRC 不是模板（缺少 template: 1）"; exit 1; }
 SRCNAME=$(qm config "$SRC" 2>/dev/null | awk -F': ' '/^name:/{print $2; exit}' || true)
 if [ -z "$NEWNAME" ]; then
   NEWNAME="temp-$(deploy_distro_label "$SRCNAME")$(deploy_distro_version "$SRCNAME")-${NEWID}"
 fi
 echo "模板: VM $SRC（$SRCNAME）"
 echo "目标: VM $NEWID（$NEWNAME）"
 echo "方式: 完整克隆（qm clone --full，磁盘独立）"
 CLONE_CMD=(qm clone "$SRC" "$NEWID" --name "$NEWNAME" --full 1)
 [ -n "$PT_STORAGE" ] && [ "$PT_STORAGE" != "local" ] && CLONE_CMD+=(--storage "$PT_STORAGE")
 if "${CLONE_CMD[@]}"; then
   _ph "完整克隆（qm clone --full）"
 else
   echo "[清理] 克隆失败，自动清理残留的半成品 VM $NEWID ..."
   cleanup_guest "$NEWID"
   echo "[错误] 克隆模板失败"
   exit 1
 fi

else
 # ---- 来源二：上传的模板文件（.vma），qmrestore 完整恢复 ----
 [ -f "$SRC_FILE" ] || { echo "[错误] 模板文件不存在: $SRC_FILE"; exit 1; }
 BASE=$(basename "$SRC_FILE")
 SRCNAME="$BASE"
 TMPDIR=""
 # 流式导入（取消「先整块拷到本地」这一步）：
 # 模板源可能在 NFS 上，原逻辑会先把整个压缩包 cp 到本机再解压/导入，等于把 NFS 读取与
 # 本地解压/提取串行执行，且多写 2 份大文件（zst + 32G VMA），徒增 270s 级耗时，还要求
 # 本机 /var/tmp 有「源 3 倍 + 2G」空闲。
 # 改为：压缩模板经管道（stdin）流式解压直接喂给 qmrestore（- 读 stdin），让「NFS 读取 / 解压 / 提取 / 写 qcow2」
 # 在一条管道里并发，且不落盘写 32G VMA；未压缩模板则直接让 qmrestore 读源（NFS 上也只需
 # 顺序读，无需整块预拷）。不再有本地空间门槛，也不再有串行阻塞的拷贝阶段。
 # 注：早期用命名管道 FIFO，但 qmrestore 仅接受普通文件或“-”（对 FIFO 报 "can't find file"），故改走 stdin 管道。
 MAGIC=$(head -c 4 "$SRC_FILE" 2>/dev/null | od -An -tx1 | tr -d ' \n')
 DECOMP=""
 case "$MAGIC" in
   28b52ffd*) DECOMP="zstd -d -c" ;;
   1f8b*)     DECOMP="gzip -dc" ;;
   894c5a4f*) DECOMP="lzop -dc" ;;
 esac
 if [ -z "$NEWNAME" ]; then
   _base=$(echo "$BASE" | sed -E 's/\.(vma|tar)(\.[^.]+)?$//')
   # 去掉 vzdump 备份文件自带的 vzdump-qemu-/vzdump-lxc- 前缀，避免默认名带多余字符
   _base=$(echo "$_base" | sed -E 's/^vzdump-[a-z]+-//')
   NEWNAME="temp-$(deploy_distro_label "$_base")$(deploy_distro_version "$_base")-${NEWID}"
 fi
 echo "模板文件: $BASE"
 echo "目标: VM $NEWID（$NEWNAME）"
 echo "方式: 完整恢复（qmrestore，磁盘独立）"
 if [ -n "$DECOMP" ]; then
   TMPDIR=$(pw_job_mktemp_dir)
   echo "[解压] 模板被整体压缩，通过管道流式解压并直接导入（不落盘、不预先整块拷贝）..."
   # 直接经管道喂给 qmrestore（- 读 stdin），避免 FIFO 不被 qmrestore 支持而报 "can't find file"；
   # 解压错误留存 decomp.err 便于定位，pipefail 保证解压失败也算整体失败（不残留半成品）
   if $DECOMP "$SRC_FILE" 2>"$TMPDIR/decomp.err" | qmrestore - "$NEWID" --storage "$PT_STORAGE"; then
     _ph "解压+导入（流式管道）"
   else
     _drc=${PIPESTATUS[0]}; _qrc=${PIPESTATUS[1]}
     echo "[清理] 恢复失败，自动清理残留的半成品 VM $NEWID ..."
     cleanup_guest "$NEWID"
     echo "[错误] 恢复模板失败（解压退出码 $_drc / qmrestore 退出码 $_qrc）: $(cat "$TMPDIR/decomp.err" 2>/dev/null)"
     rm -rf "$TMPDIR"
     exit 1
   fi
   rm -rf "$TMPDIR"
 else
   if qmrestore "$SRC_FILE" "$NEWID" --storage "$PT_STORAGE"; then
     _ph "导入（qmrestore）"
   else
     echo "[清理] 恢复失败，自动清理残留的半成品 VM $NEWID ..."
     cleanup_guest "$NEWID"
     echo "[错误] 恢复模板失败"
     exit 1
   fi
 fi
 [ -n "$TMPDIR" ] && rm -rf "$TMPDIR"
 # 恢复后若带模板标记（备份源是模板），清除之使其成为可启动的普通 VM
 if qm config "$NEWID" 2>/dev/null | grep -qE '^template:[[:space:]]*1'; then
   echo "[提示] 检测到模板标记，清除之使其成为可启动的普通 VM ..."
   qm set "$NEWID" --template 0 2>/dev/null || true
 fi
 # 修复只读 base 磁盘：模板备份的磁盘文件名带 base- 前缀且权限只读（immutable），
 # 清模板标记后普通 VM 无法以读写方式打开（Operation not permitted），须转换为独立可写磁盘
 # 注意：需从 qm config 中解析所有磁盘条目（scsi0/virtio0/sata0/ide0/efidisk0 等），找到 base- 前缀的磁盘
 BASE_DISK_LINES=$(qm config "$NEWID" 2>/dev/null | grep -E '^(scsi|virtio|sata|ide|efidisk)[0-9]*:.*base-' || true)
 if [ -n "$BASE_DISK_LINES" ]; then
   echo "[提示] 检测到只读 base 磁盘（模板残留），转换为独立可写磁盘 ..."
   # 用 here-string 读入而非管道：管道会把循环体放进子 shell，循环里的 exit 1 只能结束子 shell，
   # 主流程仍会继续改名/重置唯一标识，最后才在启动时报错——正是「根因看不见」的成因。
   while IFS= read -r line; do
     # 解析磁盘行，例如：scsi0: local:100/base-100-disk-0.qcow2,size=...
     # 或 efidisk0: local:100/base-100-disk-0.qcow2,efitype=4m,ms-cert=2023k,pre-enrolled-keys=1,size=528K
     BUS=$(echo "$line" | awk -F: '{print $1}')
     REST=$(echo "$line" | awk -F': ' '{print $2}')
     # 卷路径为第一个逗号前的部分，后面是额外参数（efitype、size 等）
     VOL_FULL=$(echo "$REST" | cut -d, -f1)
     EXTRA_OPTS=$(echo "$REST" | cut -d, -f2-)
     [ "$EXTRA_OPTS" = "$VOL_FULL" ] && EXTRA_OPTS=""
     # 解析存储名和路径，如 local:100/base-100-disk-0.qcow2 → STORE=local, VOL_PATH=100/base-100-disk-0.qcow2
     STORE=$(echo "$VOL_FULL" | cut -d: -f1)
     VOL_PATH=$(echo "$VOL_FULL" | cut -d: -f2)
     VMID_DIR=$(echo "$VOL_PATH" | cut -d/ -f1)
     BASE_NAME=$(echo "$VOL_PATH" | cut -d/ -f2)    # base-100-disk-0.qcow2
     # 按存储解析 images 目录（dir 型存储磁盘落 <根>/images；local 即 /var/lib/vz），
     # 解析失败回退 local 默认目录，避免 base 盘在非 local 目录存储时路径写死导致找不到/落错位置
     IMG_DIR="/var/lib/vz/images"
     _SR=$(pw_storage_root "$STORE" 2>/dev/null || true)
     [ -n "$_SR" ] && IMG_DIR="$_SR/images"
     # 先清除 base 磁盘的 immutable（+i）属性，否则 qemu-img/cp 读取时报 Operation not permitted
     SRC="$IMG_DIR/$VMID_DIR/$BASE_NAME"
     chattr -i "$SRC" 2>/dev/null || echo "[提示] 清除 immutable 属性失败（文件系统可能不支持 chattr），继续尝试 ..."
     # 检测源盘真实格式（按内容判定，不看扩展名）与是否有 backing file（差异磁盘）
     _qi=$(qemu-img info --output=json "$SRC" 2>/dev/null || true)
     SRC_FMT=$(printf '%s' "$_qi" | grep -oE '"format"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | cut -d'"' -f4 || true)
     BACKING=$(printf '%s' "$_qi" | grep -o '"backing-filename"' || true)
     # 统一输出为 qcow2 格式（支持快照/稀疏占用，raw 不支持快照），文件名扩展名改为 .qcow2
     NEW_DISK="${BASE_NAME#base-}"                 # 100-disk-0.qcow2
     NEW_DISK="${NEW_DISK%.*}.qcow2"               # 100-disk-0.qcow2
     DST="$IMG_DIR/$NEWID/$NEW_DISK"
     echo "  转换磁盘: $BUS → $STORE:$NEWID/$NEW_DISK（源格式 ${SRC_FMT:-未知}）"
     mkdir -p "$IMG_DIR/$NEWID"
     # 安全前提：base 盘是链接克隆的共享基盘，可能属于本机另一台 VM（模板）。若别的 VM 配置仍
     # 引用它，改名/删除会直接毁掉那台机器——只处理本次新建 VM 自己那一份，其余一律拒绝。
     _refs=$(grep -rls -- "$BASE_NAME" /etc/pve/qemu-server/ 2>/dev/null | grep -v "/${NEWID}\.conf$" || true)
     if [ -n "$_refs" ]; then
       echo "[错误] base 磁盘 $SRC 仍被其它 VM 引用（$_refs）：它是链接克隆的共享基盘，改动会破坏那台机器。"
       echo "       请改用在模板所在节点执行 qm clone <模板ID> <新ID> --full 1 得到独立盘，再对完整克隆做备份。"
       exit 1
     fi
     if [ -f "$SRC" ]; then
       # 目标盘若已存在（上次部署失败的残留），先删掉重建：那份可能是「.qcow2 名 + raw 内容」
       # 或转换到一半的半成品，直接沿用会让 qm set 继续报 Image is not in qcow2 format。
       if [ -f "$DST" ]; then
         echo "  目标磁盘已存在（上次失败残留），删除后重新生成: $NEW_DISK"
         rm -f "$DST" 2>/dev/null || true
       fi
       if [ -z "$BACKING" ] && [ "$SRC_FMT" = "qcow2" ]; then
         # 源盘本身就是独立 qcow2：直接改名复用，省掉整盘 qemu-img convert 的全量读+写
         echo "  源盘已是独立 qcow2，原地复用（清除不可变位 + 改名）..."
         if mv -f "$SRC" "$DST" 2>/dev/null && chmod 644 "$DST"; then
           echo "  已原地复用为独立磁盘: $NEW_DISK"
         else
           echo "  [警告] 原地复用失败，回退为整盘转换 ..."
           run "磁盘转换失败" qemu-img convert -p -O qcow2 "$SRC" "$DST"
           chmod 644 "$DST"
         fi
       else
         # 源盘是 raw（模板盘为 raw 时 base-…raw 的内容就是 raw）或含 backing file（差异盘）：
         # 必须整盘转成真 qcow2。只改名会让 .qcow2 里装着 raw 内容，qm set 直接失败
         # （qemu-img: Image is not in qcow2 format）——这条是实测踩过的坑。
         echo "  源盘为 ${SRC_FMT:-未知}${BACKING:+（含 backing file）}，整盘转换为独立 qcow2 磁盘（支持快照）..."
         run "磁盘转换失败" qemu-img convert -p -O qcow2 "$SRC" "$DST"
         chmod 644 "$DST"
       fi
       _ph "独立磁盘转换（qcow2）"
       # 更新配置：用原磁盘所在总线更新，保留原始额外参数（efitype、size 等）
       if [ -n "$EXTRA_OPTS" ]; then
         run "更新磁盘配置失败" qm set "$NEWID" "--${BUS}" "$STORE:$NEWID/$NEW_DISK,$EXTRA_OPTS"
       else
         run "更新磁盘配置失败" qm set "$NEWID" "--${BUS}" "$STORE:$NEWID/$NEW_DISK"
       fi
       # 删除原只读 base 磁盘（immutable 已在前面清除）
       rm -f "$SRC" 2>/dev/null && echo "  已替换为独立磁盘: $NEW_DISK" || echo "[提示] 旧 base 磁盘删除失败，不影响使用"
     elif [ -f "$DST" ]; then
       # 上游 base 盘不在，只剩上次留下的目标盘：按内容核一次格式，是「.qcow2 名 + raw 内容」就地
       # 修成真 qcow2，否则 qm set 还是报 Image is not in qcow2 format。若这份是转换中断的半成品，
       # 建议重新部署一次（.vma 会重新还原出 base 盘，走上一条干净的重建路径）。
       DST_FMT=$(qemu-img info --output=json "$DST" 2>/dev/null | grep -oE '"format"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | cut -d'"' -f4 || true)
       echo "  目标磁盘已存在（格式 ${DST_FMT:-未知}），跳过转换: $NEW_DISK"
       if [ "$DST_FMT" != "qcow2" ]; then
         echo "  [提示] 该盘不是 qcow2（上次失败残留），就地修复为 qcow2 ..."
         run "磁盘修复失败" qemu-img convert -p -O qcow2 "$DST" "$DST.tmp"
         mv -f "$DST.tmp" "$DST" && chmod 644 "$DST"
       fi
       if [ -n "$EXTRA_OPTS" ]; then
         run "更新磁盘配置失败" qm set "$NEWID" "--${BUS}" "$STORE:$NEWID/$NEW_DISK,$EXTRA_OPTS"
       else
         run "更新磁盘配置失败" qm set "$NEWID" "--${BUS}" "$STORE:$NEWID/$NEW_DISK"
       fi
     else
       # base 盘文件不在 → 配置指向一个不存在的卷，虚机必然启动失败。必须就地失败并讲清根因：
       # 否则后面照样改名、重置唯一标识、清模板标记，最后只在启动时刷三遍
       # "volume '…base-…' does not exist"，用户根本看不出问题出在模板备份本身。
       echo "[错误] base 磁盘文件不存在: $SRC"
       echo "       这是「模板备份带上了链接克隆的共享基盘（base- 前缀）」导致的：该 base 盘没有随备份"
       echo "       恢复到本机（或恢复成了别的名字/格式），而 VM 配置仍指向它，启动必然失败。"
       _bd_dir=$(dirname "$SRC")
       if [ -d "$_bd_dir" ]; then
         echo "       该目录现有文件（若数据其实落在别的名字/格式下，可 qemu-img convert 后手工挂回）："
         ls -l "$_bd_dir" 2>/dev/null | sed 's/^/         /'
       fi
       echo "       处理办法（任选其一）："
       echo "         1) 回源节点把模板盘独立化后再备份：先 chattr -i <base 文件>，再"
       echo "            qm clone <模板ID> <新ID> --full 1，对完整克隆做备份（.vma 里不再有 base- 引用）；"
       echo "         2) 源模板仍在集群里时，改用本工具来源一（集群内模板 VMID）或「克隆」的完整克隆，"
       echo "            不要用这个 .vma。"
       exit 1
     fi
   done <<< "$BASE_DISK_LINES"
   # 清理 base 磁盘替换后产生的 unused0/unused1 等残留引用
   UNUSED_LINES=$(qm config "$NEWID" 2>/dev/null | grep -E '^unused[0-9]+:' || true)
   if [ -n "$UNUSED_LINES" ]; then
     echo "[提示] 清理残留的 unused 磁盘引用 ..."
     echo "$UNUSED_LINES" | while IFS= read -r line; do
       UNUSED_KEY=$(echo "$line" | awk -F: '{print $1}')
       qm set "$NEWID" "--delete" "$UNUSED_KEY" 2>/dev/null && echo "  已移除 $UNUSED_KEY" || true
     done
   fi
 fi
 # 名称统一设置
 run "设置名称失败" qm set "$NEWID" --name "$NEWNAME"
 fi

 # 系统盘大小：默认 50 GB（auto 时 PT_SYS_DISK 为空，回落 50）；自定义时为填写的 GB；仅放大不缩小
 SYS_DISK_WANT="${PT_SYS_DISK:-}"
 [ -n "$SYS_DISK_WANT" ] || SYS_DISK_WANT=50
 resize_system_disk "$NEWID" "$SYS_DISK_WANT"

 # 唯一标识：勾选后重置硬件指纹（MAC / SMBIOS UUID+序列号 / 磁盘序列号），
# 避免部署出的 VM 与模板/备份源共用同一套身份，被业务（PCDN/网心云等）判定为同一台
if [ "$PT_UNIQUE" = "1" ]; then
 sanitize_identity "$NEWID"
fi

# 清理模板残留的 ISO 光驱：模板备份常带 ide2/sataX 的 CD-ROM 指向已不存在的 ISO，
# 若不清理，qm start 会因 "volume 'xxx.iso' does not exist" 退出码 255 而启动失败
CDROM_LINES=$(qm config "$NEWID" 2>/dev/null | grep -E '^(ide|sata)[0-9]*:.*,media=cdrom' || true)
if [ -n "$CDROM_LINES" ]; then
 echo "$CDROM_LINES" | while IFS= read -r line; do
   CDROM_KEY=$(echo "$line" | awk -F: '{print $1}')
   CDROM_VOL=$(echo "$line" | sed 's/^[^:]*:[[:space:]]*//' | cut -d, -f1)
   # 仅清除指向 ISO 文件的光驱（cloudinit 盘也是 cdrom 且不存在实际 iso，但需保留）
   case "$CDROM_VOL" in
     *iso*|*:iso/*)
       echo "[清理] 移除残留光驱 $CDROM_KEY（$CDROM_VOL，ISO 不存在或无需挂载）..."
       qm set "$NEWID" --delete "$CDROM_KEY" 2>/dev/null && echo "  已移除 $CDROM_KEY" || true
       ;;
   esac
 done
fi

# CPU 核数
if [ -n "$PT_CORES" ]; then
 run "设置 CPU 核数失败" qm set "$NEWID" --cores "$PT_CORES"
fi
# 内存大小（MB）
if [ -n "$PT_MEMORY" ]; then
 run "设置内存失败" qm set "$NEWID" --memory "$PT_MEMORY"
fi
# 内存气球（内存弹性回收）：不选 = 不启用（PVE 默认 balloon: 0，内存独占）
# 未指定内存大小时，沿用模板/原有配置里的内存值来算百分比
BAL_BASE="${PT_MEMORY:-}"
if [ -z "$BAL_BASE" ]; then
 BAL_BASE=$(qm config "$NEWID" 2>/dev/null | awk -F': ' '/^memory:/{print $2; exit}' || true)
fi
BAL=$(balloon_min_mb "${PT_BALLOON:-}" "${PT_BALLOON_MIN:-}" "$BAL_BASE")
if [ -n "$BAL" ]; then
 run "设置内存气球失败" qm set "$NEWID" --balloon "$BAL"
 echo "[内存] 内存气球已启用：最小 ${BAL} MB（宿主内存紧张时可回收；虚机需有 balloon 驱动，直通 / 大页场景建议不启用）"
elif [ -n "$PT_BALLOON" ]; then
 echo "[提示] 内存气球未启用：未能确定最小内存（未选内存大小且模板里读不到，或自定义值无效）"
fi
# CPU 类型：统一走公共逻辑（所选优先，默认 x86-64-v2-AES；ARM 自动 host）
vm_set_cpu "$NEWID" "${PT_CPU:-}" || true
# BIOS 类型（留空跟随模板；选 OVMF 时自动创建 EFI 磁盘）
if [ -n "$PT_BIOS" ]; then
 BIOS="$PT_BIOS"
 [ "$BIOS" = "ovmf" ] || BIOS="seabios"
 run "设置 BIOS 失败" qm set "$NEWID" --bios "$BIOS"
 if [ "$BIOS" = "ovmf" ]; then
   qm config "$NEWID" 2>/dev/null | grep -q '^efidisk0:' || run "创建 EFI 磁盘失败（OVMF 需要 EFI 磁盘）" qm set "$NEWID" --efidisk0 "${PT_STORAGE:-local}:0,efitype=4m,pre-enrolled-keys=0"
 fi
fi
# 机型（留空跟随模板）
if [ -n "$PT_MACHINE" ]; then
 case "$PT_MACHINE" in i440fx|pc) MACHINE_ARG="pc";; *) MACHINE_ARG="q35";; esac
 run "设置机型失败" qm set "$NEWID" --machine "$MACHINE_ARG"
fi
# 网桥：读取原 net0 保留 MAC，仅替换 bridge= 部分
if [ -n "$PT_BRIDGE" ]; then
 NET0=$(qm config "$NEWID" 2>/dev/null | grep -oE '^net0:.*' | sed 's/^net0:[[:space:]]*//')
 if [ -n "$NET0" ]; then
   MAC=$(echo "$NET0" | grep -oE 'mac=[0-9A-Fa-f:]{17}' || true)
   run "设置网桥失败" qm set "$NEWID" --net0 "virtio${MAC:+,$MAC},bridge=$PT_BRIDGE"
 else
   run "设置网桥失败" qm set "$NEWID" --net0 "virtio,bridge=$PT_BRIDGE"
 fi
fi
# QEMU 代理
if [ "$PT_AGENT" = "1" ]; then
 run "启用 QEMU 代理失败" qm set "$NEWID" --agent enabled=1
fi

# ===== Cloud-Init 实例配置（仅当克隆体带 cloudinit 盘时处理）=====
# 登录凭据（用户名 / 密码）一律沿用模板本身，本工具不再注入：模板里怎么配的就是什么，
# 避免出现「留空即被改成默认密码 123456」这种与直觉相反的行为。
# - SSH 密码登录：Linux 虚机自动开启（写 sshd_config.d 并重启 sshd），Windows 不涉及
# - QEMU 代理：由「启用 QEMU 代理」控制（Linux 虚机内安装并启用 qemu-guest-agent）
# - DNS：仅用户显式填写才覆盖
# - 网络：填了静态 IP 用静态；没填则智能默认 DHCP，防止克隆体与原模板抢同一静态 IP
if qm config "$NEWID" 2>/dev/null | grep -qE 'ide2:.*cloudinit|scsi1:.*cloudinit'; then
 # 系统判定：模板名含 win/windows 视为 Windows（跳过 Linux 专属项），否则按 Linux 处理
 case "$SRCNAME" in
   *[Ww][Ii][Nn]*|*[Ww]indows*) IS_WIN=1 ;;
   *)                            IS_WIN=0 ;;
 esac
 # 时区：Linux 虚机经 cloud-init 注入（Windows 不适用 cloud-init 的 timezone，跳过并在下面提示）
 TZ="${PT_TZ:-$(pw_host_tz)}"
 # 显式设置 cloud-init 主机名（= 生成的 temp-系统-编号）：靠 VM 名称兜底并不可靠，
 # 部分镜像会 fallback 到 VMID
 run "设置主机名失败" qm set "$NEWID" --name "$NEWNAME"
 # Linux 恒定走自定义 user-data（自动开 SSH 密码登录）；Windows 仅在启用 QEMU 代理时才需要
 if [ "$IS_WIN" != "1" ] || [ "$PT_AGENT" = "1" ]; then
   echo "[提示] 注入 Cloud-Init user-data（SSH 密码登录 / QEMU 代理；登录凭据沿用模板）..."
   SNIP_DIR="/var/lib/vz/snippets"
   mkdir -p "$SNIP_DIR"
   USER_YAML="$SNIP_DIR/cloud-${NEWID}-user.yaml"
   {
     echo "#cloud-config"
     echo "hostname: $NEWNAME"
     echo "preserve_hostname: false"
     if [ "$IS_WIN" != "1" ]; then echo "timezone: $TZ"; fi
     echo "manage_etc_hosts: true"
     if [ "$IS_WIN" != "1" ]; then
       # Linux：开启 SSH 密码登录（含 root 登录），由 sshd_config.d 覆盖 PVE 系统默认
       echo "ssh_pwauth: true"
       echo "write_files:"
       echo "  - path: /etc/ssh/sshd_config.d/99-webtools-pwauth.conf"
       echo "    content: |"
       echo "      PasswordAuthentication yes"
       echo "      PermitRootLogin yes"
     fi
     # 勾选「启用 QEMU 代理」时，让 cloud-init 在虚机内装并启用 qemu-guest-agent
     if [ "$PT_AGENT" = "1" ]; then
       echo "packages:"
       echo "  - qemu-guest-agent"
     fi
     echo "runcmd:"
     if [ "$IS_WIN" != "1" ]; then
       echo "  - hostnamectl set-hostname $NEWNAME >/dev/null 2>&1 || hostname $NEWNAME"
       echo "  - systemctl restart ssh || service ssh restart"
     fi
     if [ "$PT_AGENT" = "1" ]; then
       # 放行 guest-exec：CentOS/RHEL 7 等老版本 qemu-guest-agent 默认把 guest-exec 列入黑名单，
       # 导致 pvewebtools 的「虚机内执行」类工具（装 Docker 等）报 guest-exec has been disabled。
       # 只从黑名单「配置文件」里摘掉 guest-exec*（guest-file-* 文件读写仍留在黑名单，最小权限），
       # 不动 systemd 单元 ExecStart——其上的 virtio-serial 通道参数（-p/-m ...）必须原样保留；
       # 覆盖 ExecStart 会连通道一起抹掉，CentOS 7 的 qemu-ga 不自动探测通道，反而导致 agent 连不上。
       if [ "$IS_WIN" != "1" ]; then
         echo "  - |"
         echo "    for _f in /etc/sysconfig/qemu-ga /etc/default/qemu-guest-agent /etc/qemu-ga/qemu-ga.conf; do"
         echo "      [ -f \"\$_f\" ] && sed -i 's/guest-exec[-a-z]*,//g; s/,guest-exec[-a-z]*//g; s/guest-exec[-a-z]*//g' \"\$_f\" 2>/dev/null || true"
         echo "    done"
         echo "    rm -f /etc/systemd/system/qemu-guest-agent.service.d/override.conf 2>/dev/null || true"
         echo "    systemctl daemon-reload || true"
       fi
       echo "  - systemctl enable qemu-guest-agent"
       echo "  - systemctl restart qemu-guest-agent || true"
       fi
       # 关闭 SELinux：置 disabled（写 config 持久，下次开机彻底关；当前开机 setenforce 0 立即生效）。
       # 提前关闭，免去建完机后「一键安装Docker」还要挂盘自愈。
       if [ "$PT_SELINUX" = "1" ] && [ "$IS_WIN" != "1" ]; then
       echo "  - |"
       echo "    if [ -f /etc/selinux/config ]; then"
       echo "      sed -i 's/^SELINUX=.*/SELINUX=disabled/' /etc/selinux/config"
       echo "      setenforce 0 2>/dev/null || true"
       echo "    fi"
       fi
       # 关闭防火墙：停用并禁止开机自启 firewalld（RHEL 系）/ ufw（Debian 系）。未装则空跑。
       if [ "$PT_FIREWALL" = "1" ] && [ "$IS_WIN" != "1" ]; then
       echo "  - systemctl disable --now firewalld 2>/dev/null || true"
       echo "  - systemctl disable --now ufw 2>/dev/null || true"
       fi
       # 系统盘扩容第二步：虚机内扩分区 / LVM / 文件系统（幂等；仅虚拟盘确实变大时写入）。
       # 模板已初始化时 cloud-init 是否重跑取决于实例 ID，故下面开机后还有一层 QEMU 代理兜底。
       if [ -n "$RESIZE_GREW" ] && [ "$IS_WIN" != "1" ]; then
       pw_emit_root_expand_runcmd
       fi
       # 首次启动把 DHCP 地址固定为静态（防重启漂移；未显式指定静态 IP 且勾选时注入）
       if [ "$PT_FIXSTATIC" = "1" ] && [ -z "$PT_IP" ] && [ "$IS_WIN" != "1" ]; then
       pw_emit_dhcp_to_static_runcmd
       fi
       } > "$USER_YAML"
   run "注入 Cloud-Init user-data 失败" qm set "$NEWID" --cicustom "user=local:snippets/cloud-${NEWID}-user.yaml"
 fi
 if [ -n "$PT_IP" ]; then
   # 静态 IP 占用检测：配置层（关机也在）为硬校验，在线探测仅作提示
   _ipown=$(pw_owner_of "${PT_IP%/*}" || true)
   if [ -n "$_ipown" ]; then
     echo "[错误] 静态 IP ${PT_IP%/*} 已被占用：$_ipown"
     echo "       请换一个空闲地址，或用「批量建机」填基址自动跳过已占用地址。"
     exit 1
   fi
   if pw_ip_online "${PT_IP%/*}"; then
     echo "[提示] ${PT_IP%/*} 当前在线但不在 PVE 配置内，可能被物理机/其它设备占用，请确认"
   fi
   IPCFG="ip=$PT_IP"
   [ -n "$PT_GW" ] && IPCFG="$IPCFG,gw=$PT_GW"
   qm set "$NEWID" --ipconfig0 "$IPCFG"
 else
   qm set "$NEWID" --ipconfig0 ip=dhcp
 fi
 if [ -n "$PT_DNS" ]; then
   # DNS 非建机必需项：pct/qm 的 --nameserver 在部分 PVE 版本上连合法 IP 都会被拒，
   # pw_set_dns 逐级兜底（qm set → pvesh API → 直写配置），全部失败也只警告不中断
   pw_set_dns qm "$NEWID" "$PT_DNS" || true
 fi
 # 重新生成 cloud-init 驱动，让 guest 首次启动按新配置初始化
 if qm cloudinit update "$NEWID" 2>/dev/null; then
   echo "[提示] 初始化配置已更新，VM 启动后将按新配置初始化（未指定 IP 时自动 DHCP）"
 else
   echo "[提示] 初始化配置重新生成失败，请检查后重试"
   fi
   else
   echo "[警告] 当前模板/备份不含 Cloud-Init 盘，Cloud-Init 相关配置（IP、主机名、SSH 密码登录）均无法自动注入；"
   echo "        登录凭据不受影响（始终沿用模板本身）。如需自动配置，请改用支持 Cloud-Init 的模板。"
   fi

   # 数据盘：留空则不创建；填写大小（GB）则新建一块盘挂到部署好的 VM（启动前挂载，开机即可识别）
# 界面已标注单位 GB，这里兼容 50 / 50G / 50g 三种写法：剥掉单位后缀只留数字
DATA_DISK="${PT_DATA_DISK%%[Gg]}"
if [ -n "$DATA_DISK" ] && [ "$DATA_DISK" -gt 0 ] 2>/dev/null && qm status "$NEWID" >/dev/null 2>&1; then
 # 数据盘存储：留空或选「跟随系统盘」时，与系统盘使用同一存储
 DISK_STORE="$PT_DATA_DISK_STORAGE"
 [ -n "$DISK_STORE" ] || DISK_STORE="$PT_STORAGE"
 echo "[数据盘] 准备创建 ${DATA_DISK}G 数据盘，存储: $DISK_STORE ..."
 # 跟随 VM 现有磁盘的总线（scsi/virtio/sata/ide），避免模板没有对应控制器导致挂载失败；
 # 排除 cloudinit 行——cloud-init 常占用 ide2/scsi1，不排除会取错总线
 BUS=$(qm config "$NEWID" 2>/dev/null | grep -E '^(scsi|virtio|sata|ide)[0-9]+:' | grep -v 'cloudinit' | head -1 | sed 's/:.*//' | sed 's/[0-9]*$//')
 [ -n "$BUS" ] || BUS="scsi"
 # 各总线的槽位上限，超过 PVE 允许范围会创建失败
 case "$BUS" in
   scsi)   MAX_SLOT=30;;
   virtio) MAX_SLOT=15;;
   sata)   MAX_SLOT=5;;
   ide)    MAX_SLOT=3;;
   *)      MAX_SLOT=15;;
 esac
 # 找第一个空闲槽位（已占用的跳过，cloud-init 占用的也算已占用）
 SLOT=""
 i=0
 while [ "$i" -lt "$MAX_SLOT" ]; do
   if ! qm config "$NEWID" 2>/dev/null | grep -qE "^${BUS}${i}:"; then
     SLOT="${BUS}${i}"
     break
   fi
   i=$((i + 1))
 done
 if [ -z "$SLOT" ]; then
   echo "[警告] ${BUS} 总线已无空闲槽位（上限 ${MAX_SLOT}），已跳过数据盘创建"
 else
   # 单位是 GB：必须显式带 G，裸数字交给 qm set 会被 PVE 按默认单位解析成 20M
  if qm set "$NEWID" --"${SLOT}" "${DISK_STORE}:${DATA_DISK}G" 2>&1 | sed 's/^/  /'; then
     echo "[完成] 数据盘已挂载: ${SLOT}（${DATA_DISK}G，存储 ${DISK_STORE}）"
     echo "[提示] 数据盘在客户机内是未初始化磁盘，需进入系统后分区并格式化才能使用"
   else
     echo "[错误] 数据盘创建失败（存储 ${DISK_STORE} 空间不足或不支持该磁盘格式？）"
   fi
 fi
fi

# 部署结果摘要：无论字段选了「默认配置」还是显式指定，这里都展示最终生效的具体值
echo ""
echo "===== 部署结果 ====="
CFG=$(qm config "$NEWID" 2>/dev/null || true)
F_NAME=$(echo "$CFG" | awk -F': ' '/^name:/{print $2; exit}' || true)
F_CORES=$(echo "$CFG" | awk -F': ' '/^cores:/{print $2; exit}' || true)
F_MEM=$(echo "$CFG" | awk -F': ' '/^memory:/{print $2; exit}' || true)
F_CPU=$(echo "$CFG" | awk -F': ' '/^cpu:/{print $2; exit}' || true)
F_BRIDGE=$(echo "$CFG" | grep -oE 'bridge=[^,]*' | head -1 | cut -d= -f2 || true)
F_SYSSTORE=$(echo "$CFG" | grep -E '^(scsi|virtio|sata|ide)[0-9]+:' | grep -v cloudinit | head -1 | sed 's/^[^:]*:[[:space:]]*//' | cut -d, -f1 | cut -d: -f1 || true)
echo "VMID: $NEWID"
echo "名称: ${F_NAME:-（未命名）}"
echo "CPU 核数: ${F_CORES:-默认（沿用模板）} 核"
echo "内存: ${F_MEM:-默认（沿用模板）} MB"
echo "CPU 类型: ${F_CPU:-默认（沿用模板）}"
F_BIOS=$(echo "$CFG" | awk -F': ' '/^bios:/{print $2; exit}' || true)
F_MACHINE=$(echo "$CFG" | awk -F': ' '/^machine:/{print $2; exit}' || true)
echo "BIOS: ${F_BIOS:-seabios（默认）}"
echo "机型: ${F_MACHINE:-pc（默认）}"
echo "网桥: ${F_BRIDGE:-默认（沿用模板）}"
echo "系统盘存储: ${F_SYSSTORE:--}"
# 登录凭据：本工具不注入，恒为模板自带（不再依赖已移除的「登录用户名 / 登录密码」参数）
echo "登录凭据: 沿用模板（本工具不注入用户名/密码）"
IPCFG=$(echo "$CFG" | awk -F': ' '/^ipconfig0:/{print $2; exit}')
if echo "$IPCFG" | grep -q 'ip=dhcp'; then
 C_IP="DHCP 自动获取"
elif [ -n "$IPCFG" ]; then
 C_IP=$(echo "$IPCFG" | sed -E 's/.*ip=([^,]*).*/\1/')
else
 C_IP="DHCP 自动获取"
fi
echo "IP 地址: $C_IP"
[ -n "$DATA_DISK" ] && echo "数据盘: ${DATA_DISK}G（存储 ${DISK_STORE}）"
[ "$PT_AGENT" = "1" ] && echo "QEMU 代理: 已启用"

# 部署后启动
if [ "$PT_START" = "1" ]; then
 if qm config "$NEWID" 2>/dev/null | grep -qE '^template:[[:space:]]*1'; then
   echo "[提示] 部署结果是模板，无法启动，已跳过"
 else
   vm_start_cpu "$NEWID" "${PT_CPU:-}" || exit 1
   echo "[完成] VM $NEWID 已启动"
   # 系统盘扩容第二步：模板克隆往往注入不了 cloud-init（或 cloud-init 不重跑），
   # 只能等 QEMU 代理就绪后在虚机内扩分区 / LVM / 文件系统。全程 best-effort。
   if [ -n "$RESIZE_GREW" ]; then
     _guest_os=$(qm config "$NEWID" 2>/dev/null | awk -F': ' '/^ostype:/{print $2; exit}' || true)
     case "$_guest_os" in
       win*|w2k*) echo "[扩根] Windows 虚机不支持自动扩分区，请在系统内用磁盘管理「扩展卷」" ;;
       *)
         if qm config "$NEWID" 2>/dev/null | grep -q '^agent:[[:space:]]*enabled=1'; then
           pw_guest_grow_root "$NEWID" || true
         elif echo "$SRCNAME" | grep -qi 'openwrt'; then
           echo "[扩根] OpenWrt 模板未启用 QEMU 代理，无法自动扩根。"
           echo "       模板内预装 qemu-guest-agent 并勾选「启用 QEMU 代理」后重建；或手动执行："
           echo "       parted /dev/sda resizepart <分区号> 100% && resize2fs /dev/sdaN（f2fs 用 resize.f2fs）"
         else
           echo "[扩根] 未启用 QEMU 代理，无法自动扩分区；可进系统执行 growpart + resize2fs（xfs 用 xfs_growfs）"
         fi
         ;;
     esac
   fi
   # 首启动固定静态 IP 的 QEMU 代理兜底：模板预初始化时 cloud-init 可能不重跑、runcmd 不执行，
   # 故等代理就绪后在虚机内执行同样的 DHCP→静态转换（与扩根兜底同思路，best-effort）。
   if [ "$PT_FIXSTATIC" = "1" ] && [ -z "$PT_IP" ] && [ "$IS_WIN" != "1" ]; then
     if qm config "$NEWID" 2>/dev/null | grep -q '^agent:[[:space:]]*enabled=1'; then
       pw_guest_fix_static "$NEWID" || true
     else
       echo "[静态IP] 未启用 QEMU 代理，无法自动固定；可在系统内手动设静态 IP（或勾选 agent 后重跑）"
     fi
   fi
 fi
fi

_ph "部署完成"
DEPLOY_DONE=1
echo "[完成] 已部署 VM $NEWID（$NEWNAME），磁盘已独立"
true
}  # ===== end deploy_one =====

# ===== 本机模板缓存（口径统一在 pw_cache_use，与预热/云镜像/还原共用）=====
# 单台/批量统一走本机缓存：命中 → 本次及本节点后续建机零网络；未命中 → 只在本节点拉取
# 一次（首台慢一次，之后永久直读本地）。
# 旧实现仅批量时「预拉取」，且暂存目录建在共享存储的 dump/ 下 —— 那是 NFS 读 + NFS 写，
# 毫无本地化效果（还多赔一次网络写）。现改为落在 PVE local 存储根下的 pvewebtools-cache。
# 提示文案、失败回退、以及「只在真的要读网络时才做 64M 链路诊断」都在 pw_cache_use 里，
# 它的第 2 行输出就是本次实际要读的路径（缓存不可用时即源路径），这里不因缓存不可用而中断建机。
if [ "$SRC_KIND" = "file" ]; then
  _cres=$(pw_cache_use "$SRC_FILE" 模板)
  _cf=$(printf '%s\n' "$_cres" | sed -n 2p)
  [ -n "$_cf" ] && SRC_FILE="$_cf"
fi

# 拉取模板可能耗时数分钟（模板在慢速共享存储上时更久），期间集群有可能失去仲裁；
# 动手写配置前再探一次：既不做白工（不用先解压/导入几十 G 才发现配置写不进去），
# 也不会留下「磁盘已建、配置写不进去」的半成品。
# 放在这里而不是脚本开头：开头那次探的是任务启动那一刻，覆盖不到拉取期间的变化。
pw_quorum_precheck

# ===== 批量循环（子 shell 调用，单台失败不中断其余）=====
OK=0; FAIL=0; CREATED=""
PREV=""
IP_BASE="$PT_IP"
_ip_off=0; _batch_ips=""
for i in $(seq 1 "$BATCH_COUNT"); do
 if [ "$i" -eq 1 ]; then
   CUR="$FIRST_VMID"
 else
   CUR=$((PREV + 1))
   # VMID 集群唯一：用集群级视图判定占用
   while vmid_state_cluster "$CUR" >/dev/null; do CUR=$((CUR + 1)); done
 fi
 # 批量模式：填了静态 IP 基址则逐台递增末段（网关/DNS 共用）；未填则保持 DHCP。
 # 递增时跳过「配置层已占用 + 本批已分配」的地址，避免与既有虚机/容器抢 IP。
 if [ "$BATCH_COUNT" -gt 1 ] && [ -n "$PT_IP" ]; then
   while :; do
     PT_IP=$(pw_inc_ip "$IP_BASE" "$_ip_off") || exit 1
     if pw_ip_taken "${PT_IP%/*}" "$_batch_ips"; then
       _ip_off=$((_ip_off + 1)); continue
     fi
     break
   done
   _batch_ips="$_batch_ips ${PT_IP%/*}"
   _ip_off=$((_ip_off + 1))
 fi
 # 名称：单台用用户填的主机名；批量不指定前缀，由 deploy_one 内按模板默认生成（含 VMID 唯一）
 if [ "$BATCH_COUNT" -eq 1 ]; then
   THIS_NAME="$PT_NEWNAME"
 else
   THIS_NAME=""
 fi
 echo ""
 echo "########## 批量进度 [$i/$BATCH_COUNT] → VMID $CUR ##########"
 if ( deploy_one "$CUR" "$THIS_NAME" ); then
   OK=$((OK + 1)); CREATED="$CREATED $CUR"
 else
   FAIL=$((FAIL + 1))
   echo "[警告] 第 $i 台（VMID $CUR）部署失败，继续下一台"
 fi
 PREV="$CUR"
done
echo ""
echo "===== 批量建机汇总 ====="
echo "成功: $OK 台，失败: $FAIL 台"
[ -n "$CREATED" ] && echo "已创建 VMID:$CREATED"
# 单台失败时保持原有非零退出（批量模式则容忍单台失败、继续其余）
if [ "$BATCH_COUNT" -eq 1 ] && [ "$FAIL" -gt 0 ]; then exit 1; fi
true
