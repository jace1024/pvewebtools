
export DEBIAN_FRONTEND=noninteractive
KEEP="$PT_JOURNAL"
free_mb() { df -BM --output=avail / | tail -1 | tr -dc '0-9'; }
echo "---- 清理前容量（根分区）----"
df -h /
BEFORE=$(free_mb)
echo; echo "[1/4] 清理 apt 缓存与冗余软件包 ..."
run "apt 缓存清理失败" apt-get clean
run "apt 冗余包清理失败" apt-get autoremove -y
echo; echo "[2/4] 系统日志（journal）瘦身到 $KEEP 以内 ..."
journalctl --disk-usage 2>/dev/null || true
run "journal 瘦身失败" journalctl --vacuum-size="$KEEP"
echo; echo "[3/4] 清理轮转旧日志（*.gz/*.xz/*.old/*.数字，超过 7 天）..."
DEL=$(find /var/log -type f \( -name '*.gz' -o -name '*.xz' -o -name '*.old' -o -regex '.*\.[0-9]+' \) -mtime +7 2>/dev/null | wc -l || true)
# 单个被占用的文件删除失败不影响整体，不做硬性要求
find /var/log -type f \( -name '*.gz' -o -name '*.xz' -o -name '*.old' -o -regex '.*\.[0-9]+' \) -mtime +7 -delete 2>/dev/null || true
echo "已清理约 $DEL 个轮转日志文件"
echo; echo "[4/4] 清理 /var/tmp 过期临时文件（超过 15 天）..."
find /var/tmp -mindepth 1 -mtime +15 -delete 2>/dev/null || true
AFTER=$(free_mb)
RELEASED=$((AFTER - BEFORE))
[ "$RELEASED" -lt 0 ] && RELEASED=0
echo; echo "---- 清理后容量（根分区）----"
df -h /
echo "[完成] 本次共释放约 ${RELEASED} MB 磁盘空间"
