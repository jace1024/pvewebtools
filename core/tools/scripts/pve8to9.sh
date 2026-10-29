
# ── 版本检查 ──
CUR_VER=$(pveversion 2>/dev/null | sed -n 's/.*pve-manager\/\([0-9]\).*/\1/p' | head -1)
if [ "$CUR_VER" = "9" ]; then
 echo "[提示] 当前已是 PVE 9.x，无需升级"; exit 0
elif [ "$CUR_VER" != "8" ]; then
 echo "[错误] 当前版本不是 8.x（检测到 ${CUR_VER:-未知}），仅支持从 8.x 升级"; exit 1
fi
echo "[0/8] 当前 PVE 版本: 8.x，目标: 9.x"
ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
echo "  架构: $ARCH"
export DEBIAN_FRONTEND=noninteractive

# ── 1. 升级到最新 8.x ──
echo "[1/8] 升级到最新 8.x ..."
wait_apt_lock || { echo "[错误] apt 锁被占用，请稍后重试"; exit 1; }
fix_dpkg_interrupted || exit 1
run "apt update 失败，请先检查源配置" apt update
run "apt upgrade 失败" apt upgrade -y
run "apt dist-upgrade 失败" apt dist-upgrade -y

# ── 2. pve8to9 检查 ──
echo "[2/8] 运行 pve8to9 升级前检查 ..."
pve8to9 --full 2>&1 | tee /tmp/pve8to9_check.log || true
if grep -qi "FAIL" /tmp/pve8to9_check.log; then
 echo "[错误] pve8to9 检查存在 FAIL 项，请先处理后再升级"; exit 1
fi
echo "  检查通过"

# ── 3. 微码（仅 x86）──
echo "[3/8] 安装微码 ..."
if [ "$ARCH" = "amd64" ] || [ "$ARCH" = "x86_64" ]; then
 if grep -qi intel /proc/cpuinfo; then apt install -y intel-microcode || true
 else apt install -y amd64-microcode || true; fi
else
 echo "  非 x86 架构（$ARCH），跳过微码安装"
fi

# ── 4. 备份关键配置 ──
echo "[4/8] 备份关键配置 ..."
BAK=/etc/pvewebtools-bak
mkdir -p "$BAK"
cp -a /etc/apt/sources.list* "$BAK/" 2>/dev/null || true
cp -a /etc/apt/sources.list.d/* "$BAK/" 2>/dev/null || true
echo "  已备份到 $BAK"

# ── 5. 切换 bookworm → trixie 源 ──
echo "[5/8] 切换源: bookworm → trixie ..."
# Debian 源
SL=/etc/apt/sources.list
if [ -s "$SL" ]; then
 sed -i 's/bookworm/trixie/g' "$SL"
fi
# deb822 格式
for F in /etc/apt/sources.list.d/debian.sources; do
 [ -f "$F" ] || continue
 sed -i 's/bookworm/trixie/g' "$F"
done
# PVE 源
for F in /etc/apt/sources.list.d/pve-no-subscription.sources \
        /etc/apt/sources.list.d/pve-enterprise.sources; do
 [ -f "$F" ] || continue
 sed -i 's/bookworm/trixie/g' "$F"
done
# Ceph 源
for F in /etc/apt/sources.list.d/ceph.sources \
        /etc/apt/sources.list.d/ceph-enterprise.sources; do
 [ -f "$F" ] || continue
 sed -i 's/bookworm/trixie/g' "$F"
done
# 传统格式 .list 文件
for F in /etc/apt/sources.list.d/*.list; do
 [ -f "$F" ] || continue
 sed -i 's/bookworm/trixie/g' "$F"
done

# ── 6. dist-upgrade ──
echo "[6/8] 执行 dist-upgrade（耗时较长，请耐心等待）..."
run "apt update 失败，请检查源配置" apt update
run "dist-upgrade 失败，请查看上方 dpkg 报错手动处理" \
   apt dist-upgrade -y -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold"
run "apt autoremove 失败" apt autoremove -y
apt autoclean 2>/dev/null || true

# ── 7. 验证 ──
echo "[7/8] 验证升级结果 ..."
NEW_VER=$(pveversion 2>/dev/null | sed -n 's/.*pve-manager\/\([0-9]\).*/\1/p' | head -1)
if [ "$NEW_VER" = "9" ]; then
 echo "  升级成功！当前版本: $(pveversion)"
else
 echo "[错误] 升级后版本检测为 ${NEW_VER:-未知}，非 9.x，请检查日志"
 exit 1
fi

# ── 8. 重启 ──
echo "[8/8] 30 秒后自动重启以加载新内核 ..."
echo "[完成] PVE 8.x → 9.x 升级成功，30 秒后自动重启"
(sleep 30 && reboot) &
