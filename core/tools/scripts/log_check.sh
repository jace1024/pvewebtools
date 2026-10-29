
echo "---- 系统错误日志（最近 50 条）----"
journalctl -p err -n 50 --no-pager 2>/dev/null || echo "(journal 不可读)"
echo; echo "---- 内核错误（dmesg 最近 30 条）----"
dmesg -l err,crit,alert,emerg 2>/dev/null | tail -30 || echo "(dmesg 不可读)"
echo; echo "---- 失败单元 ----"
FAILED=$(systemctl list-units --state=failed --no-legend 2>/dev/null || true)
if [ -n "$FAILED" ]; then printf '%s\n' "$FAILED"; else echo "(无失败单元)"; fi
echo; echo "提示：若以上无明显错误，说明近期系统无重大异常"
