
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

# 生成一段 cloud-init runcmd 条目：虚机首次启动把软件源换成 $1 指定的国内镜像。
# apt / apk 完整重写；rpm(dnf/yum) 改写现有 repo 的 baseurl 指向国内镜像；
# CentOS 7/8 EOL 转所选镜像的 centos-vault（官方 vault 对国内常 403，仅兜底）。所有更新带重试且失败不致命（|| true）。
# 仅输出条目文本（含 2 空格缩进的 "- |" 与 4 空格脚本体），调用方负责写进 user-data 的 runcmd。
pve_emit_source_swap() {
cat <<'SW' | sed "s/__MIRROR__/$1/"
  - |
    # === pvewebtools: 更换国内软件源 (__MIRROR__) ===
    mkdir -p /root/pve-source-bak
    . /etc/os-release 2>/dev/null
    case "__MIRROR__" in
      ustc)     APT=https://mirrors.ustc.edu.cn; RPM=https://mirrors.ustc.edu.cn; APK=https://mirrors.ustc.edu.cn;;
      tsinghua) APT=https://mirrors.tuna.tsinghua.edu.cn; RPM=https://mirrors.tuna.tsinghua.edu.cn; APK=https://mirrors.tuna.tsinghua.edu.cn;;
      aliyun)   APT=https://mirrors.aliyun.com; RPM=https://mirrors.aliyun.com; APK=https://mirrors.aliyun.com;;
      tencent)  APT=https://mirrors.cloud.tencent.com; RPM=https://mirrors.cloud.tencent.com; APK=https://mirrors.cloud.tencent.com;;
      huawei)   APT=https://repo.huaweicloud.com; RPM=https://repo.huaweicloud.com; APK=https://repo.huaweicloud.com;;
    esac
    MIRROR_KEY="__MIRROR__"
    # 镜像×发行版 可用性表：标准发行版（ubuntu/debian/alpine/centos-stream/rocky/almalinux/fedora）五大镜像均收录，无需门控；
    # Anolis / openEuler 路径非标，按 config/cloud-image-presets.json 的 mirror_repo_support 字段自动生成，不在表内的组合跳过换源（保留官方源）以免 404。
        case "$MIRROR_KEY" in
      aliyun|huawei|ustc|tsinghua) CAN_ANOLIS=1 ;;
      *) CAN_ANOLIS=0 ;;
    esac
    case "$MIRROR_KEY" in
      aliyun|huawei|ustc|tsinghua) CAN_OPENEULER=1 ;;
      *) CAN_OPENEULER=0 ;;
    esac

    case "$ID" in
      ubuntu|debian)
        mv -f /etc/apt/sources.list /root/pve-source-bak/apt-sources.list 2>/dev/null
        mv -f /etc/apt/sources.list.d/*.list /root/pve-source-bak/ 2>/dev/null
        # Ubuntu 23.10+ / Debian 12+ 默认改用 deb822 格式（/etc/apt/sources.list.d/*.sources）。
        # 只清 *.list 时官方源（archive.ubuntu.com / deb.debian.org）仍然生效，换源等于白做，
        # 后续 apt 还会去连国外源而超时，这里一并移走（备份以便换源失败时回滚）。
        for _f in /etc/apt/sources.list.d/*.sources; do
          [ -f "$_f" ] && mv -f "$_f" /root/pve-source-bak/ 2>/dev/null
        done
        CN=$( . /etc/os-release; echo ${VERSION_CODENAME:-${UBUNTU_CODENAME}} )
        if [ "$ID" = "ubuntu" ]; then
          printf 'deb %s/ubuntu/ %s main restricted universe multiverse\ndeb %s/ubuntu/ %s-updates main restricted universe multiverse\ndeb %s/ubuntu/ %s-security main restricted universe multiverse\ndeb %s/ubuntu/ %s-backports main restricted universe multiverse\n' "$APT" "$CN" "$APT" "$CN" "$APT" "$CN" "$APT" "$CN" > /etc/apt/sources.list.d/99-pve-mirror.list
        else
          printf 'deb %s/debian/ %s main contrib non-free non-free-firmware\ndeb %s/debian/ %s-updates main contrib non-free non-free-firmware\ndeb %s/debian-security/ %s-security main contrib non-free non-free-firmware\n' "$APT" "$CN" "$APT" "$CN" "$APT" "$CN" > /etc/apt/sources.list.d/99-pve-mirror.list
        fi
        # 换源后更新失败（镜像未收录该发行版 / 网络不通）则回滚镜像自带源，
        # 否则 apt 会既连不上镜像、又丢了原源，彻底不可用（装 qemu-guest-agent 随之失败）
        if ! apt-get update -o Acquire::Retries=3 -o Acquire::http::Timeout=20; then
          echo "[mirror] ${MIRROR_KEY} 源更新失败，回滚为镜像自带源"
          rm -f /etc/apt/sources.list.d/99-pve-mirror.list
          [ -f /root/pve-source-bak/apt-sources.list ] && mv -f /root/pve-source-bak/apt-sources.list /etc/apt/sources.list
          for _f in /root/pve-source-bak/*.list /root/pve-source-bak/*.sources; do
            case "$_f" in *apt-sources.list) continue ;; esac
            [ -f "$_f" ] && mv -f "$_f" /etc/apt/sources.list.d/ 2>/dev/null
          done
          apt-get update -o Acquire::Retries=2 -o Acquire::http::Timeout=20 || true
        fi
        ;;
      alpine)
        cp -f /etc/apk/repositories /root/pve-source-bak/ 2>/dev/null
        V=$( . /etc/os-release; echo $VERSION_ID )
        printf '%s/alpine/v%s/main\n%s/alpine/v%s/community\n' "$APK" "$V" "$APK" "$V" > /etc/apk/repositories
        apk update || true
        ;;
      centos|rhel)
        MAJ="${VERSION_ID%%.*}"
        if [ "$MAJ" = "7" ] || [ "$MAJ" = "8" ]; then
          # EOL 仓库：官方 vault.centos.org 对国内大量 IP 返回 403，优先用所选镜像的 centos-vault。
          # vault 目录名是完整小版本号（如 7.9.2009 / 8.5.2111）而非主版本，从 /etc/centos-release 解析；
          # 架构用 yum 自带的 $basearch 字面量（repo 文件内 yum 自动展开，不依赖 shell 变量）。
          # 逐候选 curl 探测 repomd 可达后选定 base（探测失败默认镜像 vault，官方 vault 兜底）。
          FULLVER=$(sed -n 's/.*release \([0-9][0-9.]*\).*/\1/p' /etc/centos-release 2>/dev/null)
          FULLVER="${FULLVER:-$MAJ}"
          _ARCH=$(uname -m)
          _base=""
          if [ "$MAJ" = "7" ]; then
            for u in "$RPM/centos-vault/$FULLVER" "$RPM/centos/$MAJ" "https://vault.centos.org/centos/$FULLVER"; do
              if curl -fsI -m 8 "$u/os/$_ARCH/repodata/repomd.xml" >/dev/null 2>&1; then _base="$u"; break; fi
            done
            [ -n "$_base" ] || _base="$RPM/centos-vault/$FULLVER"
            printf '[base]\nname=CentOS 7 vault base\nbaseurl=%s/os/$basearch/\ngpgcheck=0\nenabled=1\n[updates]\nname=CentOS 7 vault updates\nbaseurl=%s/updates/$basearch/\ngpgcheck=0\nenabled=1\n[extras]\nname=CentOS 7 vault extras\nbaseurl=%s/extras/$basearch/\ngpgcheck=0\nenabled=1\n' "$_base" "$_base" "$_base" > /etc/yum.repos.d/99-pve-vault.repo
          else
            for u in "$RPM/centos-vault/$FULLVER" "https://vault.centos.org/centos/$FULLVER"; do
              if curl -fsI -m 8 "$u/BaseOS/$_ARCH/os/repodata/repomd.xml" >/dev/null 2>&1; then _base="$u"; break; fi
            done
            [ -n "$_base" ] || _base="$RPM/centos-vault/$FULLVER"
            printf '[baseos]\nname=CentOS 8 vault baseos\nbaseurl=%s/BaseOS/$basearch/os/\ngpgcheck=0\nenabled=1\n[appstream]\nname=CentOS 8 vault appstream\nbaseurl=%s/AppStream/$basearch/os/\ngpgcheck=0\nenabled=1\n[extras]\nname=CentOS 8 vault extras\nbaseurl=%s/extras/$basearch/os/\ngpgcheck=0\nenabled=1\n' "$_base" "$_base" "$_base" > /etc/yum.repos.d/99-pve-vault.repo
          fi
          rm -f /etc/yum.repos.d/CentOS-*.repo 2>/dev/null
        else
          # CentOS Stream / RHEL 9+：官方免费仓库即 CentOS Stream，国内镜像路径为 <镜像>/centos-stream
          mv -f /etc/yum.repos.d/centos*.repo /root/pve-source-bak/ 2>/dev/null
          mv -f /etc/yum.repos.d/CentOS-*.repo /root/pve-source-bak/ 2>/dev/null
          printf '[baseos]\nname=CentOS Stream $releasever - BaseOS\nbaseurl=%s/centos-stream/$releasever/BaseOS/$basearch/os/\ngpgcheck=0\nenabled=1\n[appstream]\nname=CentOS Stream $releasever - AppStream\nbaseurl=%s/centos-stream/$releasever/AppStream/$basearch/os/\ngpgcheck=0\nenabled=1\n[extras]\nname=CentOS Stream $releasever - Extras\nbaseurl=%s/centos-stream/$releasever/extras/$basearch/os/\ngpgcheck=0\nenabled=1\n' "$RPM" "$RPM" "$RPM" > /etc/yum.repos.d/centos-stream.repo
        fi
        yum makecache || true
        ;;
      rocky)
        # Rocky 官方仓库路径为 dl.rockylinux.org/$contentdir，国内镜像统一为 <镜像>/rocky
        mv -f /etc/yum.repos.d/rocky*.repo /root/pve-source-bak/ 2>/dev/null
        printf '[baseos]\nname=Rocky Linux $releasever - BaseOS\nbaseurl=%s/rocky/$releasever/BaseOS/$basearch/os/\ngpgcheck=0\nenabled=1\n[appstream]\nname=Rocky Linux $releasever - AppStream\nbaseurl=%s/rocky/$releasever/AppStream/$basearch/os/\ngpgcheck=0\nenabled=1\n[extras]\nname=Rocky Linux $releasever - Extras\nbaseurl=%s/rocky/$releasever/extras/$basearch/os/\ngpgcheck=0\nenabled=1\n' "$RPM" "$RPM" "$RPM" > /etc/yum.repos.d/rocky.repo
        dnf makecache || true
        ;;
      almalinux|alma)
        # Alma 官方仓库路径为 repo.almalinux.org/almalinux，国内镜像统一为 <镜像>/almalinux
        mv -f /etc/yum.repos.d/almalinux*.repo /root/pve-source-bak/ 2>/dev/null
        printf '[baseos]\nname=AlmaLinux $releasever - BaseOS\nbaseurl=%s/almalinux/$releasever/BaseOS/$basearch/os/\ngpgcheck=0\nenabled=1\n[appstream]\nname=AlmaLinux $releasever - AppStream\nbaseurl=%s/almalinux/$releasever/AppStream/$basearch/os/\ngpgcheck=0\nenabled=1\n[extras]\nname=AlmaLinux $releasever - Extras\nbaseurl=%s/almalinux/$releasever/extras/$basearch/os/\ngpgcheck=0\nenabled=1\n' "$RPM" "$RPM" "$RPM" > /etc/yum.repos.d/almalinux.repo
        dnf makecache || true
        ;;
      fedora)
        # Fedora 官方仓库路径为 download.fedoraproject.org/pub/fedora/linux，国内镜像统一为 <镜像>/fedora
        mv -f /etc/yum.repos.d/fedora*.repo /root/pve-source-bak/ 2>/dev/null
        printf '[fedora]\nname=Fedora $releasever - $basearch\nbaseurl=%s/fedora/releases/$releasever/Everything/$basearch/os/\ngpgcheck=0\nenabled=1\n[updates]\nname=Fedora $releasever - $basearch - Updates\nbaseurl=%s/fedora/updates/$releasever/Everything/$basearch/\ngpgcheck=0\nenabled=1\n' "$RPM" "$RPM" > /etc/yum.repos.d/fedora.repo
        dnf makecache || true
        ;;
      anolis)
        # Anolis 官方仓库主机 mirrors.openanolis.cn，各镜像统一为 <镜像>/anolis。
        # 沿用系统内自带的 AnolisOS*.repo（结构正确，仅替换主机名，避免拍错 openEuler 式非标准布局）。
        if [ "${CAN_ANOLIS:-0}" != "1" ]; then
          echo "[跳过] ${MIRROR_KEY} 镜像未收录 Anolis 仓库，保留官方源"
        else
          for f in /etc/yum.repos.d/AnolisOS*.repo; do
            [ -f "$f" ] && sed -i -e "s|https\?://mirrors.openanolis.cn/anolis|${RPM}/anolis|g" "$f"
          done
          dnf makecache || true
        fi
        ;;
      openeuler)
        # openEuler 官方仓库主机 repo.openeuler.org，各镜像统一为 <镜像>/openeuler。
        # 沿用系统内 openEuler.repo（保留 openEuler-$releasever/OS|everything|EPOL 结构，仅替换主机名）。
        if [ "${CAN_OPENEULER:-0}" != "1" ]; then
          echo "[跳过] ${MIRROR_KEY} 镜像未收录 openEuler 仓库，保留官方源"
        elif [ -f /etc/yum.repos.d/openEuler.repo ]; then
          sed -i -e "s|https\?://repo.openeuler.org|${RPM}/openeuler|g" /etc/yum.repos.d/openEuler.repo
          dnf makecache || true
        fi
        ;;
      oracle|ol)
        # Oracle Linux 9/10 与 CentOS Stream 二进制兼容，linuxmirrors.cn 直接复用其仓库路径 <镜像>/centos-stream。
        MAJ="${VERSION_ID%%.*}"
        if [ "$MAJ" = "7" ] || [ "$MAJ" = "8" ]; then
          # Oracle 7/8 无统一国内镜像路径，保留官方源（可用，仅国外），以免改写后 404
          :
        else
          mv -f /etc/yum.repos.d/ol*.repo /root/pve-source-bak/ 2>/dev/null
          printf '[baseos]\nname=CentOS Stream $releasever - BaseOS\nbaseurl=%s/centos-stream/$releasever/BaseOS/$basearch/os/\ngpgcheck=0\nenabled=1\n[appstream]\nname=CentOS Stream $releasever - AppStream\nbaseurl=%s/centos-stream/$releasever/AppStream/$basearch/os/\ngpgcheck=0\nenabled=1\n[extras]\nname=CentOS Stream $releasever - Extras\nbaseurl=%s/centos-stream/$releasever/extras/$basearch/os/\ngpgcheck=0\nenabled=1\n' "$RPM" "$RPM" "$RPM" > /etc/yum.repos.d/centos-stream.repo
          dnf makecache || true
        fi
        ;;
    esac
SW
}
IMG="$PT_IMAGE"
[ -n "$IMG" ] || { echo "[错误] 请选择云镜像"; exit 1; }
# 集群失去仲裁时 /etc/pve 只读，qm create/importdisk 等写操作必然失败；先探一次给出可操作指引
pw_quorum_precheck
# 源镜像位置：value 形如 <storage>::<name>，默认导入目录 storage 为 __import__
case "$IMG" in
 *::*) IMG_STORAGE="${IMG%%::*}"; IMG_NAME="${IMG##*::}" ;;
 *)    IMG_STORAGE="__import__"; IMG_NAME="$IMG" ;;
esac
if [ "$IMG_STORAGE" = "__import__" ]; then
 SRC_ROOT=$(pw_storage_root pvewebtools)
 [ -n "$SRC_ROOT" ] && SRC="$SRC_ROOT/cloud-images/$IMG_NAME" || SRC="/var/lib/vz/cloud-images/$IMG_NAME"
else
 S_ROOT=$(pw_storage_root "$IMG_STORAGE")
 [ -n "$S_ROOT" ] || { echo "[错误] 无法解析存储 $IMG_STORAGE 的根路径"; exit 1; }
 SRC="$S_ROOT/cloud-images/$IMG_NAME"
fi
[ -f "$SRC" ] || { echo "[错误] 镜像文件不存在: $SRC（请先到顶部「上传模板/备份文件」卡片下载或上传云镜像）"; exit 1; }

# ===== 本机镜像缓存 =====
# 与模板建机共用同一套本机缓存（pw_cache_use 里含提示与「不可用才诊断」的口径）：
# 云镜像常放在共享存储上，首次使用时拉取到本机，之后本节点所有建机直读本地。
_cres=$(pw_cache_use "$SRC" 镜像)
_cf=$(printf '%s\n' "$_cres" | sed -n 2p)
[ -n "$_cf" ] && SRC="$_cf"

# 选项 B：压缩包（.xz/.gz/.zst）原样保留在导入目录，此处现场解压后再用
DEC=""
_clean_dec() { [ -n "$DEC" ] && rm -f "$DEC" 2>/dev/null; }
trap '_clean_dec' EXIT
case "$SRC" in
 *.xz)  DEC="${SRC%.*}.dec.tmp"; echo "[解压] xz -dc $SRC"; xz -dc "$SRC" > "$DEC" ;;
 *.gz)  DEC="${SRC%.*}.dec.tmp"; echo "[解压] gzip -dc $SRC"; gzip -dc "$SRC" > "$DEC" ;;
 *.zst) DEC="${SRC%.*}.dec.tmp"; echo "[解压] zstd -dc $SRC"; zstd -dc "$SRC" > "$DEC" ;;
esac
if [ -n "$DEC" ]; then
 [ -s "$DEC" ] || { echo "[错误] 解压失败或文件为空: $SRC"; exit 1; }
 echo "[解压] 已解压为临时文件: $DEC"
 SRC="$DEC"
fi

# 首台 VMID（留空自动分配；手动指定已存在则报错，避免误覆盖）
FIRST_VMID="$PT_NEWID"
if [ -z "$FIRST_VMID" ]; then
 FIRST_VMID=100
 # VMID 集群唯一：用集群级视图判定占用，避免本机 qm status 看不到其它节点上的 VM 而误判空闲
 while vmid_state_cluster "$FIRST_VMID" >/dev/null; do
   FIRST_VMID=$((FIRST_VMID + 1))
 done
else
 if [ -n "$(vmid_state_cluster "$FIRST_VMID")" ]; then
   echo "[错误] VMID $FIRST_VMID 已存在（集群内），请换一个或留空自动分配"; exit 1
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

# 单台部署逻辑（包成函数供批量循环调用）；参数：本次 VMID、名称（空则由函数内按镜像生成）
deploy_one() {
NEWID="$1"
NEWNAME="$2"

# 失败自动清理：本函数异常退出（含 run 的 exit 1）且 VM 是本次新建的，则销毁残缺 VM，
# 避免留下半成品（缺网络/代理/cloudinit 更新等）。创建成功只设 DEPLOY_DONE=1，trap 不再销毁。
DEPLOY_DONE=0
VM_EXISTED_BEFORE=0; qm status "$NEWID" >/dev/null 2>&1 && VM_EXISTED_BEFORE=1
_cleanup_on_fail() {
  # 只在「本次新建且没走到成功」时动它：成功路径必须保留片段（虚机配置里的
  # cicustom 仍指向它，删了虚机下次开机会因 volume ... does not exist 失败）
  if [ "$DEPLOY_DONE" = "1" ] || [ "$VM_EXISTED_BEFORE" != "0" ]; then return 0; fi
  # 半成品虚机的 Cloud-Init 片段一并清掉（片段是普通文件，qm destroy 不管它，见 pw_snippet_rm）
  pw_snippet_rm "$NEWID"
  if qm status "$NEWID" >/dev/null 2>&1; then
    echo "[清理] 部署未完成，自动销毁残缺 VM $NEWID ..."
    qm stop "$NEWID" >/dev/null 2>&1
    if qm destroy "$NEWID" --purge >/dev/null 2>&1; then
      echo "[清理] 已销毁残缺 VM $NEWID"
    else
      echo "[警告] 残缺 VM $NEWID 自动销毁失败，请手动清理: qm destroy $NEWID --purge"
    fi
  fi
  return 0
}
trap _cleanup_on_fail EXIT

# 由镜像文件名推导系统简称（小写），用于生成 cloud-系统-编号 默认主机名
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
 # 别名表未命中：由镜像名清洗出前缀，别一律回落 linux（见 DEPLOY_LABEL_HELPERS）
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

# 判断系统盘总线：老系统（CentOS/RHEL 6 及更早）的 initramfs 通常不含 virtio_scsi 模块，
# 用默认 virtio-scsi 控制器会导致启动找不到磁盘；这类改用 virtio（virtio-blk）总线更稳。
deploy_disk_bus() {
 local n=$(echo "$IMG_NAME" | tr '[:upper:]' '[:lower:]')
 local ver=$(echo "$n" | grep -oE '(centos|rhel|scientific|sl|oracle|rocky|alma|fedora)[^0-9]*[0-9]+' | grep -oE '[0-9]+' | head -1)
 if [ -n "$ver" ] && [ "$ver" -lt 7 ]; then
   echo "virtio"
 else
   echo "scsi"
 fi
}

# 主机名：Cloud-Init 注入（名称空则由函数内按云镜像文件名自动生成 cloud-系统-编号）
if [ -z "$NEWNAME" ]; then
 DISTRO=$(deploy_distro_label "$IMG_NAME")
 DVER=$(deploy_distro_version "$IMG_NAME")
 NEWNAME="cloud-${DISTRO}${DVER}-${NEWID}"
fi
STORAGE="${PT_STORAGE:-local}"
CORES="${PT_CORES:-1}"
MEM="${PT_MEMORY:-2048}"
BRIDGE="${PT_BRIDGE:-vmbr0}"

echo "云镜像: $IMG"
echo "目标: VM $NEWID（$NEWNAME）"

# 1. 创建 VM（最小配置：virtio 网卡 + virtio-scsi 控制器）
echo "[1/6] 创建 VM $NEWID ..."
run "创建 VM 失败" qm create "$NEWID" --name "$NEWNAME" --memory "$MEM" --cores "$CORES" \
 --net0 "virtio,bridge=$BRIDGE" --scsihw virtio-scsi-pci --ostype l26 --serial0 socket
# 内存气球（内存弹性回收）：不选 = 不启用（PVE 默认 balloon: 0，内存独占）
BAL=$(balloon_min_mb "${PT_BALLOON:-}" "${PT_BALLOON_MIN:-}" "$MEM")
if [ -n "$BAL" ]; then
 run "设置内存气球失败" qm set "$NEWID" --balloon "$BAL"
 echo "[内存] 内存气球已启用：最小 ${BAL} MB（宿主内存紧张时可回收；虚机需有 balloon 驱动，直通 / 大页场景建议不启用）"
elif [ -n "$PT_BALLOON" ]; then
 echo "[提示] 内存气球未启用：自定义最小内存值无效（应为 2~6 位数字）"
fi

# 2. 导入云镜像为磁盘（统一转 qcow2，支持快照）
echo "[2/6] 导入云镜像为磁盘（存储 $STORAGE）..."
OUT=$(qm importdisk "$NEWID" "$SRC" "$STORAGE" --format qcow2 2>&1)
echo "$OUT"
# 解析导入后的磁盘 volid：不同 PVE 版本 importdisk 输出文案不一
# （常见 "imported disk as 'local:103/...'"，也有 "unused0: successfully imported disk 'local:103/...'"）
# 直接用 volid 格式 <存储>:<VMID>/vm-<VMID>-disk-<N>.<格式> 提取，兼容两种写法
VOLID=$(echo "$OUT" | grep -oE '[A-Za-z0-9_.-]+:[0-9]+/vm-[0-9]+-disk-[0-9]+\.(qcow2|raw|vmdk)' | head -1)
# 兜底：直接从刚导入挂上的 unused 盘解析
if [ -z "$VOLID" ]; then
 VOLID=$(qm config "$NEWID" 2>/dev/null | grep -oE '^unused[0-9]+: [^, ]+' | head -1 | sed -E 's/^unused[0-9]+: //')
fi
# 校验确为合法 volid，避免把错误文案误当磁盘传给 qm set 引发级联失败
case "$VOLID" in
 *:*/vm-*-disk-*) : ;;
 *) echo "[错误] 未能从导入输出解析磁盘 volid（导入输出见上）"; exit 1 ;;
esac
BUS=$(deploy_disk_bus)
if [ "$BUS" = "virtio" ]; then
 run "挂载系统盘失败" qm set "$NEWID" --virtio0 "$VOLID"
 BOOT_DEV=virtio0
 echo "[总线] 检测到老系统（CentOS/RHEL 6 及更早），系统盘改用 virtio（virtio-blk）总线以兼容其 initramfs"
else
 run "挂载系统盘失败" qm set "$NEWID" --scsi0 "$VOLID"
 BOOT_DEV=scsi0
fi
run "设置启动顺序失败" qm set "$NEWID" --boot "order=$BOOT_DEV"

# 系统盘大小：镜像盘不足所选容量时自动扩容（仅放大不缩小）
# 注意：这一步只扩 PVE 侧虚拟盘；虚机内的分区/LVM/文件系统由首次开机时的 Cloud-Init
# runcmd 补扩（见下方 pw_emit_root_expand_runcmd），所以「硬件里是 100G、登进去 df 还是 40G」
# 只可能是虚机从未成功走完 Cloud-Init 首启。
SYS_DISK_WANT="${PT_SYS_DISK:-}"
[ -n "$SYS_DISK_WANT" ] || SYS_DISK_WANT=50
resize_system_disk "$NEWID" "$SYS_DISK_WANT"
if [ -n "$RESIZE_GREW" ]; then
  echo "[提示] 虚机内分区/文件系统由首次开机自动扩展（guest 内日志: /var/log/cloud-init-output.log）"
fi

# 3. Cloud-Init 驱动盘
echo "[3/6] 创建 Cloud-Init 驱动盘 ..."
run "创建 Cloud-Init 盘失败" qm set "$NEWID" --ide2 "${STORAGE}:cloudinit"

# 4/5. 登录用户 / 密码（Linux 云镜像标准：默认 root，密码 123456）
CIUSER="${PT_CIUSER:-root}"
CIPASS="${PT_CIPASSWORD:-123456}"

# 凭据 / 主机名注入：恒定走自定义 Cloud-Init user-data（--cicustom），不再按勾选项分叉。
# 为什么必须恒定注入主机名：本环境 PVE 的 qm 不支持 --cihostname（报 Unknown option），
# 不走 user-data 时主机名只能靠 qm create --name 兜底；而部分云镜像（openEuler / RHEL 系）
# 在 /etc/cloud/cloud.cfg 里带 preserve_hostname: true，会保留镜像自带主机名，表现为
# 「PVE 列表名称 = cloud-系统-编号，登进系统 hostname 却不是它」。故这里始终写 user-data：
# 显式 hostname + preserve_hostname: false（user-data 优先级高于镜像内 cloud.cfg），
# 并在 runcmd 里再兜一次 hostnamectl，保证「PVE 名称 == guest 主机名」在任何勾选组合下都成立。
# 勾选项只决定附加行为：ssh_pwauth + 允许 root 密码登录 / 装并启用 qemu-guest-agent。
# 时区：统一为宿主机时区（可用参数覆盖）。虚机与宿主机时刻天然一致（kvm-clock/RTC），
# 这里统一的是 guest 内显示的时区，经 cloud-init 的 timezone 字段注入。
TZ="${PT_TZ:-$(pw_host_tz)}"
echo "[4/6] 注入 Cloud-Init user-data（主机名 / 登录凭据 / ssh_pwauth / QEMU 代理）..."
SNIP_DIR="/var/lib/vz/snippets"
mkdir -p "$SNIP_DIR"
USER_YAML="$SNIP_DIR/cloud-${NEWID}-user.yaml"
# 扩根的「第二步」（写进 Cloud-Init runcmd，首次开机执行）：实现在 COMMON_HELPERS 的
# pw_emit_root_expand_runcmd / pw_root_expand_body（模板快速建机也要用同一份逻辑）。
{
 echo "#cloud-config"
 echo "hostname: $NEWNAME"
 echo "preserve_hostname: false"
 echo "timezone: $TZ"
 echo "manage_etc_hosts: true"
 echo "manage_resolv_conf: true"
 echo "resolv_conf:"
 echo "  nameservers:"
 # 逐个 nameserver 写成 YAML 列表项：先规范化（逗号/空格混写都成空格分隔），再按词拆分
 for _d in $(pw_dns_list "${PT_DNS:-223.5.5.5,119.29.29.29}"); do echo "    - $_d"; done
 if [ "$PT_SSH_PWAUTH" = "1" ]; then echo "ssh_pwauth: true"; fi
 echo "users:"
 echo "  - name: $CIUSER"
 echo "    lock_passwd: false"
 echo "    sudo: ALL=(ALL) NOPASSWD:ALL"
 echo "chpasswd:"
 echo "  list: |"
 printf '    %s:%s\n' "$CIUSER" "$CIPASS"
 echo "  expire: false"
 if [ "$PT_SSH_PWAUTH" = "1" ]; then
  echo "write_files:"
  echo "  - path: /etc/ssh/sshd_config.d/99-webtools-pwauth.conf"
  echo "    content: |"
  echo "      PasswordAuthentication yes"
  echo "      PermitRootLogin yes"
 fi
 echo "runcmd:"
 # DNS 先行：换源与 apt 安装都要先能解析镜像域名；解析失败时先重置 resolv.conf
 echo "  - |"
 echo "    if ! getent hosts deb.debian.org >/dev/null 2>&1; then"
 echo "      rm -f /etc/resolv.conf; printf 'nameserver 223.5.5.5\\nnameserver 119.29.29.29\\n' > /etc/resolv.conf; echo '[net] DNS 解析失败，已重置 /etc/resolv.conf'"
 echo "    fi"
 # 换源必须排在装 qemu-guest-agent 之前：原先追加在 runcmd 末尾，首次开机装 agent 用的是
 # 云镜像自带官方源（archive.ubuntu.com），国内常 20s 超时 → agent 装不上 → PVE 界面 IPs 空白
 # 注意：这里只写 stdout，由外层代码块统一重定向落盘。不能再对同一文件单独追加：
 # 同一文件开第二个 fd，写入位置与外层不同步，后续 echo 会把换源段覆盖掉（YAML 被截断）。
 if [ "${PT_MIRROR:-none}" != "none" ] && [ -n "$PT_MIRROR" ]; then
   pve_emit_source_swap "$PT_MIRROR"
 fi
 # 双保险：本次 user-data 已把 preserve_hostname 覆盖为 false；个别镜像的 cloud-init 对
 # hostname 模块处理不一致（如残留旧 instance-id 缓存），再显式执行一次 hostnamectl
 echo "  - hostnamectl set-hostname $NEWNAME >/dev/null 2>&1 || hostname $NEWNAME"
 if [ "$PT_SSH_PWAUTH" = "1" ]; then echo "  - systemctl restart ssh || service ssh restart"; fi
 # 勾选「启用 QEMU 代理」时，让 cloud-init 在虚机内装并启用 qemu-guest-agent
 # （此时软件源已切换为国内镜像，安装不再走国外官方源）
 # 包管理器必须三路探测：原先写死 apt-get，openEuler / CentOS / Rocky 等 dnf/yum 系统上
 # 这步必然失败（镜像本身又通常不带 qemu-ga），表现为「勾了 QEMU 代理，PVE 里仍看不到 IP、
 # 后续依赖代理的工具（一键安装Docker 等）全部用不了」。
 if [ "$PT_AGENT" = "1" ]; then
  echo "  - |"
  echo "    ga_bin=\$(command -v qemu-ga 2>/dev/null || ls /usr/bin/qemu-ga /usr/libexec/qemu-ga /usr/sbin/qemu-ga 2>/dev/null | head -1 || true)"
  echo "    if [ -z \"\$ga_bin\" ]; then"
  echo "      if command -v apt-get >/dev/null 2>&1; then"
  echo "        apt-get update -o Acquire::Retries=3 -o Acquire::http::Timeout=20 || true"
  echo "        apt-get install -y qemu-guest-agent || echo '[agent] 安装失败（apt）'"
  echo "      elif command -v dnf >/dev/null 2>&1; then"
  echo "        dnf install -y qemu-guest-agent || echo '[agent] 安装失败（dnf）'"
  echo "      elif command -v yum >/dev/null 2>&1; then"
  echo "        yum install -y qemu-guest-agent || echo '[agent] 安装失败（yum）'"
  echo "      else"
  echo "        echo '[agent] 未找到可用的包管理器（apt/dnf/yum），跳过安装'"
  echo "      fi"
  echo "    fi"
  echo "    # openEuler/RHEL 的 qemu-guest-agent 是 static 单元（无 [Install]，靠 udev/virtio-serial 端口触发），"
  echo "    # enable 会报错且不生效；用 start 拉起即可，enable 仅作兼容尝试。"
  echo "    systemctl start qemu-guest-agent 2>/dev/null || true"
  echo "    systemctl enable qemu-guest-agent >/dev/null 2>&1 || true"
  echo "    ga_bin=\$(command -v qemu-ga 2>/dev/null || ls /usr/bin/qemu-ga /usr/libexec/qemu-ga /usr/sbin/qemu-ga 2>/dev/null | head -1 || true)"
  echo "    if [ -n \"\$ga_bin\" ]; then echo '[agent] qemu-guest-agent 已安装并启动'; else echo '[agent] 未安装成功，PVE 将无法通过代理读取虚机信息'; fi"
 fi
 # 关闭 SELinux：置 disabled（写 config 持久，下次开机彻底关；当前开机 setenforce 0 立即生效）。
 # 提前关闭，免去建完机后「一键安装Docker」还要挂盘自愈。
 if [ "$PT_SELINUX" = "1" ]; then
  echo "  - |"
  echo "    if [ -f /etc/selinux/config ]; then"
  echo "      sed -i 's/^SELINUX=.*/SELINUX=disabled/' /etc/selinux/config"
  echo "      setenforce 0 2>/dev/null || true"
  echo "    fi"
 fi
 # 关闭防火墙：停用并禁止开机自启 firewalld（RHEL 系）/ ufw（Debian 系）。未装则空跑。
 if [ "$PT_FIREWALL" = "1" ]; then
  echo "  - |"
  echo "    systemctl disable --now firewalld 2>/dev/null || true"
  echo "    systemctl disable --now ufw 2>/dev/null || true"
 fi
 # 系统盘扩容第二步：虚机内扩分区 / LVM / 文件系统。放在 runcmd 末尾，避免与换源、
 # 装 qemu-guest-agent 抢 IO；幂等，已扩满时整段空跑。只在虚拟盘确实变大时才需要。
 if [ -n "$RESIZE_GREW" ]; then
  pw_emit_root_expand_runcmd
 fi
 # 首次启动把 DHCP 地址固定为静态（防重启漂移；未显式指定静态 IP 且勾选时注入）
 if [ "$PT_FIXSTATIC" = "1" ] && [ -z "$PT_IP" ]; then
  pw_emit_dhcp_to_static_runcmd
 fi
} > "$USER_YAML"
# 不再回退 --ciuser/--cipassword：PVE 标准 user-data 不带 preserve_hostname，正是主机名
# 不生效的根源；登录凭据已在上面的 users / chpasswd 中一并落定。
run "注入登录凭据失败" qm set "$NEWID" --cicustom "user=local:snippets/cloud-${NEWID}-user.yaml"

# 6. 网络：静态 IP / 网关 / DNS（留空则显式 DHCP，避免镜像未收到网络配置而无法上网）
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
  run "设置静态 IP 失败" qm set "$NEWID" --ipconfig0 "$IPCFG"
else
  run "设置 DHCP 失败" qm set "$NEWID" --ipconfig0 ip=dhcp
fi
if [ -n "$PT_DNS" ]; then
 # DNS 非建机必需项：pct/qm 的 --nameserver 在部分 PVE 版本上连合法 IP 都会被拒，
 # pw_set_dns 逐级兜底（qm set → pvesh API → 直写配置），全部失败也只警告
 pw_set_dns qm "$NEWID" "$PT_DNS" || true
fi

# CPU 类型：统一走公共逻辑（所选优先，默认 x86-64-v2-AES；ARM 自动 host）
vm_set_cpu "$NEWID" "${PT_CPU:-}" || true
# BIOS 类型：默认 SeaBIOS；选 OVMF(UEFI) 时需同时创建 EFI 磁盘，否则无法启动
BIOS="${PT_BIOS:-seabios}"
[ "$BIOS" = "ovmf" ] || BIOS="seabios"
run "设置 BIOS 失败" qm set "$NEWID" --bios "$BIOS"
if [ "$BIOS" = "ovmf" ]; then
  qm config "$NEWID" 2>/dev/null | grep -q '^efidisk0:' || run "创建 EFI 磁盘失败（OVMF 需要 EFI 磁盘）" qm set "$NEWID" --efidisk0 "${STORAGE}:0,efitype=4m,pre-enrolled-keys=0"
fi
# 机型：默认 q35（PVE 原生默认是 pc/i440fx，这里显式设置）；可选 i440fx(pc)
MACHINE="${PT_MACHINE:-q35}"
case "$MACHINE" in i440fx|pc) MACHINE="i440fx"; MACHINE_ARG="pc";; *) MACHINE="q35"; MACHINE_ARG="q35";; esac
run "设置机型失败" qm set "$NEWID" --machine "$MACHINE_ARG"
# QEMU 代理（勾选才启用；取消则不装 agent）
if [ "$PT_AGENT" = "1" ]; then
 run "启用 QEMU 代理失败" qm set "$NEWID" --agent enabled=1
fi
# 重新生成 Cloud-Init 配置，确保首次启动生效
qm cloudinit update "$NEWID" 2>/dev/null && echo "[提示] Cloud-Init 配置已生成" || echo "[提示] Cloud-Init 重新生成失败，可手动: qm cloudinit update $NEWID"

# 数据盘：留空则不创建；填写大小（GB）则新建一块盘挂到 VM（启动前挂载，开机即可识别）
# 界面已标注单位 GB，这里兼容 50 / 50G / 50g 三种写法：剥掉单位后缀只留数字
DATA_DISK="${PT_DATA_DISK%%[Gg]}"
if [ -n "$DATA_DISK" ] && [ "$DATA_DISK" -gt 0 ] 2>/dev/null; then
 # 数据盘存储：留空或选「跟随系统盘」时，与系统盘使用同一存储
 DISK_STORE="$PT_DATA_DISK_STORAGE"
 [ -n "$DISK_STORE" ] || DISK_STORE="$STORAGE"
 echo "[数据盘] 准备创建 ${DATA_DISK}G 数据盘，存储: $DISK_STORE ..."
 # 跟随 VM 现有磁盘的总线（scsi/virtio/sata/ide）；排除 cloudinit 行——云镜像常占 ide2/scsi1
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

# 部署结果摘要（与模板快速建机输出格式一致）
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
echo "主机名: $NEWNAME（首次启动由 Cloud-Init 注入，与名称保持一致）"
echo "CPU 核数: ${F_CORES:-1} 核"
echo "内存: ${F_MEM:-2048} MB"
echo "CPU 类型: ${F_CPU:-默认配置}"
echo "BIOS: ${BIOS:-seabios}"
echo "机型: ${MACHINE:-q35}"
echo "网桥: ${F_BRIDGE:-vmbr0}"
echo "系统盘存储: ${F_SYSSTORE:--}"
echo "登录用户: $CIUSER"
echo "登录密码: $CIPASS"
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

DEPLOY_DONE=1
echo "[完成] 已创建 VM $NEWID（$NEWNAME）"
if [ "$PT_START" = "1" ]; then
 vm_start_cpu "$NEWID" "${PT_CPU:-}" || exit 1
 echo "[完成] VM $NEWID 已启动，首次启动将走 Cloud-Init 初始化（设置主机名/IP/登录凭据）"
fi
true
}  # ===== end deploy_one =====

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
 # 名称：单台用用户填的主机名；批量不指定前缀，由 deploy_one 内按镜像默认生成（含 VMID 唯一）
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
