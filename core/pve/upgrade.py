from __future__ import annotations
from typing import Any
"""产品端「系统升级」模块（编译版自助升级）。

只做本机能做的事，不依赖任何外部服务、不联网：

1. **部署形态识别**：编译版二进制 + systemd 单元托管 = 可自升级；
   源码版（python3 server.py）/ 手工前台运行 = 不可自升级（提示改用 install.sh 部署）。
2. **接收升级包**：管理台编译出的发行包（tar.gz，内含 pvewebtools + manifest.json
   [+ pvewebtools.dist/]），落本机 /var/tmp/pvewebtools-upgrade/packages/。
3. **安装前校验**（全部通过才允许替换二进制，任何一项不过都在安装前拦下）：
   - manifest 合法性与 format 版本
   - 架构一致（x86_64 / aarch64）—— 二进制与内核模块都绑架构，不一致装上直接起不来
   - 版本形态不降级（Pro 客户不许装 Lite 包，避免静默丢失高级玩法）
   - 密钥对同源（key_fp）—— 换过密钥对的包会让客户已有授权码全部验签失败
   - 元数据签名（Ed25519）—— 防 manifest 字段被篡改；旧包无签名时降级为告警
   - 内容完整性（主程序 sha256 / standalone 目录聚合摘要）
   - 版本新旧（装同版本或更低版本需显式确认）
4. **执行升级**：替换动作交给 `systemd-run` 起的独立 transient unit 执行。
   **关键之一**：pvewebtools.service 是 control-group 托管，若在自己进程里 fork 脚本，
   `systemctl stop pvewebtools` 会把脚本一起杀掉（升级永远做不完）→ 必须脱离 cgroup。
   **关键之二**：`systemd-run` 必须带 `--no-block`。不加会同步等 oneshot 跑完，而脚本第一步
   就停本服务 → 提交接口的 HTTP 响应永远发不出去，前端只看到连接中断（Failed to fetch），
   误判「升级未启动」；任务本身照旧在独立 unit 里完成，于是报错与成功同时出现。
   **生效时机**：脚本由「提交那一刻正在运行的二进制」现场渲染（见 _prepare_script），
   因此脚本内容 / systemd-run 参数 / 日志写法这类修复，装包后的第一次操作还是老模板，
   下一次操作才生效 —— 别把这一次的现象当成没修好。
5. **健康检查与自动回滚**：替换后轮询服务状态与监听端口，失败自动还原上一版并重新拉起。
6. **手动回滚**：随时可回到升级前的版本（快照在 /etc/pvewebtools/rollback）。

授权说明：升级**不需要重新签发授权码**。指纹（主板序列号）与密钥对不变时，
/etc/pvewebtools/pvewebtools.key 继续有效；本模块全程不读写授权文件。
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from core import paths

# ---- 路径与常量 ----

# 项目家目录（开源版固定为 /etc/pvewebtools）
APP_DIR = Path("/etc/pvewebtools")

UNIT = "pvewebtools.service"
UNIT_FILE = Path("/etc/systemd/system") / UNIT
UPGRADE_UNIT = "pvewebtools-upgrade"
UPGRADE_UNIT_FILE = UPGRADE_UNIT + ".service"

WORK_DIR = Path("/var/tmp/pvewebtools-upgrade")     # 上传包与暂存解压（本机临时目录）
PKG_DIR = WORK_DIR / "packages"
STAGE_DIR = WORK_DIR / "stage"
LOG_FILE = APP_DIR / "upgrade.log"
STATE_FILE = APP_DIR / "upgrade-state.json"
INSTALLED_AT_FILE = APP_DIR / "version-installed-at"   # 当前版本「升级/部署时间」（自升级/回滚/首次部署写入）
ROLLBACK_DIR = APP_DIR / "rollback"

BINARY_NAME = "pvewebtools"
DIST_NAME = "pvewebtools.dist"
MANIFEST_NAME = "manifest.json"
INSTALLER_NAME = "install.sh"

MAX_PKG_SIZE = 512 * 1024 * 1024        # 单包上限 512MB（onefile 产物通常几十 MB）
MANIFEST_FORMAT = 1                     # 本版本支持的 manifest 格式
_HEALTH_TIMEOUT = 40                    # 健康检查最长等待秒数

_ALLOWED_TOP_FILES = {BINARY_NAME, MANIFEST_NAME, INSTALLER_NAME}


class UpgradeError(Exception):
    """升级相关的可预期错误（直接回显给前端，不打印栈）。"""


# ---- 版本与形态 ----

def _build_marks() -> Any:
    """编译期标记（edition.py 由 build.sh 生成）：返回 (edition, version, build, built_at)。

    - version  ：功能版本号（产品端据此判断新旧）；
    - build    ：git 构建串（如 `v1.0.0-3-g1a3e831-dirty`），仅用于展示与「同版本不同构建」的判定。
    - built_at ：构建时刻（UTC ISO8601），仅用于展示与追溯。

    源码版没有该模块，返回 ("", "", "", "")，由调用方回退到源码根 VERSION 文件。
    """
    try:
        import edition  # noqa: PLC0415
    except ImportError:
        return "", "", "", ""
    return (str(getattr(edition, "EDITION", "") or ""),
            str(getattr(edition, "VERSION", "") or ""),
            str(getattr(edition, "BUILD", "") or ""),
            str(getattr(edition, "BUILT_AT", "") or ""))


def _source_version() -> Any:
    """产品源码 VERSION 文件里的版本号（源码版/开发版用）。"""
    try:
        text = (Path(paths.RESOURCE_DIR) / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text.splitlines()[0].strip() if text else ""


_GIT_SOURCE_MARKS = None  # 源码版构建参考（git 最近提交 hash + 时间）懒缓存：("", "") 表示取不到


def _git_source_marks() -> Any:
    """源码版没有 edition.py，用工作区 git 最近一次提交作为「构建参考」：

    返回 (build, built_at)。build 形如 `g<短hash>`（与 `git describe` 风格一致），
    built_at 是该提交的提交时间（UTC ISO8601）。取不到时返回 ("", "")（如非 git 仓库、
    .git 被删、或 git 不可用）—— 这种情况前端不显示构建时间即可。
    """
    global _GIT_SOURCE_MARKS
    if _GIT_SOURCE_MARKS is not None:
        return _GIT_SOURCE_MARKS
    try:
        repo = str(paths.RESOURCE_DIR)
        r = subprocess.run(
            ["git", "-C", repo, "log", "-1", "--format=%H%n%cI"],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            out = r.stdout.strip().split("\n")
            if len(out) == 2 and out[0] and out[1]:
                _GIT_SOURCE_MARKS = ("g" + out[0][:12], out[1])
                return _GIT_SOURCE_MARKS
    except (OSError, subprocess.SubprocessError):
        pass
    _GIT_SOURCE_MARKS = ("", "")
    return _GIT_SOURCE_MARKS


def _arch() -> Any:
    """归一化本机架构：x86_64 / aarch64。"""
    m = (os.uname().machine or "").lower()
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "aarch64"
    return m


def _parse_version(v: Any) -> Any:
    parts = re.findall(r"\d+", str(v or ""))
    return tuple(int(x) for x in parts) if parts else ()


def _cmp_version(a: Any, b: Any) -> Any:
    """比较版本号：a > b 返回 1，a < b 返回 -1，相等 0（缺位补 0）。"""
    ta, tb = _parse_version(a), _parse_version(b)
    n = max(len(ta), len(tb))
    ta = ta + (0,) * (n - len(ta))
    tb = tb + (0,) * (n - len(tb))
    return (ta > tb) - (ta < tb)


def version_info() -> dict:
    """当前运行版本的展示信息。

    version 参与新旧比较；build 是编译期注入的 git 构建串，仅用于展示与追溯
    （产品端用它区分「同版本号的不同构建」）；built_at 是构建时刻（UTC），仅展示用；
    installed_at 是本机把当前版本升上来/部署上的时刻（UTC），回答「这台机器这版是何时上线的」。
    """
    edition, version, build, built_at = _build_marks()
    source = "build"
    if not version:
        version = _source_version()
        source = "source"
    # 源码版没有 edition.py：用工作区 git 最近提交作为构建参考（build/built_at），
    # 让「系统升级」页对源码版也能显示构建时间（取不到则保持空串，前端不显示）。
    if source == "source" and not build and not built_at:
        git_build, git_built = _git_source_marks()
        build, built_at = git_build, git_built
    installed = read_installed_at()
    installed_at = installed.get("at", "") if isinstance(installed, dict) else ""
    return {"version": version or "未知", "edition": edition,
            "build": build, "built_at": built_at, "installed_at": installed_at,
            "source": source}


def read_installed_at() -> Any:
    """读取「升级/部署时间」标记（version-installed-at）。

    返回 dict {"version": str, "at": "<UTC ISO8601>"}，文件缺失/损坏时返回 None。
    仅自升级/回滚成功或首次部署时写入，故能反映本机当前版本真实的上线时刻。
    """
    try:
        data = json.loads(INSTALLED_AT_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("at"):
            return data
    except (OSError, ValueError):
        pass
    return None


def ensure_installed_at_marker() -> None:
    """若本机尚未记录当前版本上线时间，则写入当前 UTC 时间作为兜底。

    用于老版本首次迁移、源码/调试启动等没有 version-installed-at 标记的场景；
    已有有效标记时不覆盖，避免把「当前启动时间」误当成「上线时间」。
    """
    if read_installed_at():
        return
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        data = {"version": version_info()["version"], "at": now}
        INSTALLED_AT_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ---- 部署形态 ----

def _self_path() -> Any:
    """当前进程的可执行文件路径（编译版为二进制自身）。"""
    argv0 = sys.argv[0] if sys.argv else ""
    if not argv0:
        return Path("")
    try:
        return Path(argv0).resolve()
    except OSError:
        return Path(argv0)


def _unit_port() -> Any:
    """从 systemd 单元文件解析监听端口；解析不到返回 0。"""
    try:
        text = UNIT_FILE.read_text(encoding="utf-8")
    except OSError:
        return 0
    m = re.search(r"^ExecStart=(.*)$", text, re.M)
    if not m:
        return 0
    nums = re.findall(r"(?<![\w.])(\d{2,5})(?![\w.])", m.group(1))
    for n in reversed(nums):
        p = int(n)
        if 1 <= p <= 65535:
            return p
    return 0


def workdir_writable() -> Any:
    """升级包暂存目录（或最近的已存在父目录）是否可写。

    决定能否接收上传：上传与校验只写这个临时目录，不触碰运行中的程序，
    因此这一点与「能否自助升级」相互独立。
    """
    p = WORK_DIR
    while not p.exists() and p != p.parent:
        p = p.parent
    return os.access(str(p), os.W_OK)


def deployment(port_hint: int = 0) -> dict:
    """识别部署形态，判断是否具备自升级能力。port_hint 为运行期已知端口（后端传入）。

    can_self_upgrade：能否网页自助替换程序（需编译版 + root + systemd）；
    can_upload      ：能否接收并校验升级包（只写临时目录，源码版/开发环境同样开放，
                      便于联调与发包前体检；真正的替换仍由 can_self_upgrade 把关）。
    """
    binary = _self_path()
    installed = bool(binary) and binary.name == BINARY_NAME and binary.parent == APP_DIR
    unit_exists = UNIT_FILE.is_file()
    systemd_run = bool(shutil.which("systemd-run"))
    port = _unit_port() or int(port_hint or 0) or 8899
    unit_active = False
    if unit_exists:
        try:
            r = subprocess.run(["systemctl", "is-active", UNIT],
                               capture_output=True, text=True, timeout=10)
            unit_active = (r.stdout or "").strip() == "active"
        except (OSError, subprocess.SubprocessError):
            unit_active = False

    reason = ""
    if os.geteuid() != 0:
        reason = "需要以 root 运行才能升级"
    elif not installed:
        # 不在此处回显运行脚本的绝对路径：出口净化器会把 `.py` 路径整条替换掉，
        # 前端只会看到「内部错误」而失去可读原因（binary 字段仍带完整路径，供排障用）。
        reason = ("当前是源码版运行（不是发行包二进制），不支持网页自助升级；"
                  "请用发行包内的 install.sh 手工部署。")
    elif not unit_exists:
        reason = "未找到 systemd 单元 %s，请先用 install.sh 注册服务" % UNIT_FILE
    elif not systemd_run:
        reason = "系统缺少 systemd-run，无法脱离服务 cgroup 安全执行升级"
    can_upload = workdir_writable()
    return {
        "app_dir": str(APP_DIR),
        "binary": str(binary),
        "installed": installed,
        "unit": UNIT,
        "unit_file": unit_exists,
        "unit_active": unit_active,
        "port": port,
        "systemd_run": systemd_run,
        "can_self_upgrade": not reason,
        "can_upload": can_upload,
        "upload_reason": "" if can_upload else "升级包暂存目录 %s 不可写，无法上传" % WORK_DIR,
        "reason": reason,
    }


# ---- 上传 ----

def valid_pkg_name(name: str) -> Any:
    """升级包文件名白名单：只接受 .tar.gz / .tgz，防路径穿越。"""
    n = str(name or "").strip()
    if not re.fullmatch(r"[\w][\w.\-]{0,99}", n):
        return False
    return n.lower().endswith((".tar.gz", ".tgz"))


def package_path(name: str) -> Any:
    return PKG_DIR / name


def receive_upload(rfile: Any, length: Any, name: str) -> dict:
    """流式接收升级包（单请求整包上传），返回 {name, size, sha256}。"""
    if not valid_pkg_name(name):
        raise UpgradeError("升级包文件名不合法（仅支持 .tar.gz / .tgz）")
    length = int(length or 0)
    if length <= 0:
        raise UpgradeError("上传内容为空（缺少 Content-Length）")
    if length > MAX_PKG_SIZE:
        raise UpgradeError("升级包超过 %d MB 上限" % (MAX_PKG_SIZE // 1024 // 1024))
    if not workdir_writable():
        raise UpgradeError("升级包暂存目录 %s 不可写，无法上传" % WORK_DIR)

    try:
        PKG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise UpgradeError("创建升级包目录失败: %s" % e)
    tmp = PKG_DIR / (name + ".part")
    total = 0
    h = hashlib.sha256()
    try:
        with open(tmp, "wb") as f:
            while total < length:
                buf = rfile.read(min(length - total, 1024 * 1024))
                if not buf:
                    break
                f.write(buf)
                h.update(buf)
                total += len(buf)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise UpgradeError("写入升级包失败: %s" % e)
    if total != length:
        tmp.unlink(missing_ok=True)
        raise UpgradeError("上传中断（收到 %d / %d 字节）" % (total, length))
    os.replace(tmp, PKG_DIR / name)
    return {"name": name, "size": total, "sha256": h.hexdigest()}


def list_packages() -> Any:
    """已上传的升级包清单（按修改时间倒序）。"""
    out = []
    try:
        files = [p for p in PKG_DIR.iterdir() if p.is_file() and p.suffix != ".part"]
    except OSError:
        return out
    for p in sorted(files, key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({"name": p.name, "size": st.st_size, "mtime": int(st.st_mtime)})
    return out


def delete_package(name: str) -> bool:
    """删除已上传的包（含对应的暂存解压目录）。"""
    if not valid_pkg_name(name):
        raise UpgradeError("升级包文件名不合法")
    p = PKG_DIR / name
    if p.is_file():
        p.unlink()
    shutil.rmtree(STAGE_DIR / name, ignore_errors=True)
    return True


# ---- 包解析与校验 ----

def _safe_extract(tar: Any, dest: str) -> Any:
    """安全解包：只允许普通文件，路径不得越界，且只接受已知成员。

    允许：pvewebtools / manifest.json / install.sh / pvewebtools.dist/**(任意内容)。
    其它文件一律跳过（进 skipped 列表，不阻断），目录/链接/设备文件一律拒绝。
    返回 (extracted, skipped)。
    """
    dest = dest.resolve()
    members = tar.getmembers()
    names = [m.name for m in members if m.isfile()]
    if not names:
        raise UpgradeError("升级包为空")

    # 发行包外层有一层目录（pvewebtools-lite-1.2.0-20260923-120000/）：识别并剥离
    prefix = ""
    tops = {n.split("/", 1)[0] for n in names}
    if len(tops) == 1:
        top = tops.pop()
        if top not in _ALLOWED_TOP_FILES and all("/" in n for n in names):
            prefix = top + "/"

    extracted, skipped = [], []
    for m in members:
        if m.isdir():
            continue
        if not m.isfile():
            raise UpgradeError("升级包内含非普通文件（%s），已拒绝安装" % m.name)
        rel = m.name[len(prefix):] if prefix and m.name.startswith(prefix) else m.name
        rel = rel.lstrip("./")
        if not rel or rel.startswith("/") or "\\" in rel or ".." in Path(rel).parts:
            raise UpgradeError("升级包内路径非法（%s），已拒绝安装" % m.name)
        parts = Path(rel).parts
        if len(parts) == 1:
            if parts[0] not in _ALLOWED_TOP_FILES:
                skipped.append(rel)
                continue
        elif parts[0] != DIST_NAME:
            skipped.append(rel)
            continue

        target = dest / rel
        try:
            resolved = target.resolve()
        except OSError:
            raise UpgradeError("升级包内路径无法解析（%s）" % m.name)
        if not str(resolved).startswith(str(dest)):
            raise UpgradeError("升级包内路径越界（%s）" % m.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        src = tar.extractfile(m)
        if src is None:
            raise UpgradeError("升级包内文件无法读取（%s）" % m.name)
        with open(target, "wb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
        if rel == BINARY_NAME:
            os.chmod(target, 0o755)
        extracted.append(rel)
    return extracted, skipped


def _sha256_file(path: str, chunk: Any = 1024 * 1024) -> Any:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _dist_digest(dist_dir: Any) -> Any:
    """standalone 目录聚合摘要：与 build.sh / 管理台打包时的算法完全一致。"""
    h = hashlib.sha256()
    for p in sorted(x for x in dist_dir.rglob("*") if x.is_file()):
        h.update(p.relative_to(dist_dir).as_posix().encode("utf-8"))
        h.update(b"\x00")
        h.update(_sha256_file(p).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _check(name: str, ok: bool, detail: Any, level: str = 'error') -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail, "level": level}


def inspect(name: str, force: bool = False, allow_downgrade: bool = False) -> Any:
    """解包并校验升级包，返回元数据 + 逐项检查结果。

    errors 非空即不可安装（ok=False）。同一包重复调用会重新解压，保证结果与包内容一致。
    """
    if not valid_pkg_name(name):
        raise UpgradeError("升级包文件名不合法")
    tar_path = package_path(name)
    if not tar_path.is_file():
        raise UpgradeError("升级包不存在，请先上传")

    stage = STAGE_DIR / name
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            extracted, skipped = _safe_extract(tar, stage)
    except tarfile.TarError as e:
        shutil.rmtree(stage, ignore_errors=True)
        raise UpgradeError("升级包不是合法的 tar.gz 文件: %s" % e)
    except (OSError, EOFError) as e:
        shutil.rmtree(stage, ignore_errors=True)
        raise UpgradeError("解包升级包失败: %s" % e)

    cur = version_info()
    dep = deployment()
    errors, warnings, checks = [], [], []

    if BINARY_NAME not in extracted:
        errors.append("升级包内缺少主程序 %s" % BINARY_NAME)
        checks.append(_check("包内容", False, "缺少主程序 " + BINARY_NAME))
        return _inspect_result(name, tar_path, stage, None, checks, errors, warnings,
                               skipped, dep)

    # ---- manifest ----
    manifest = None
    mf_path = stage / MANIFEST_NAME
    if mf_path.is_file():
        try:
            manifest = json.loads(mf_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = None
    if not isinstance(manifest, dict):
        errors.append("升级包缺少合法的 %s（请使用管理台编译产出的发行包）" % MANIFEST_NAME)
        checks.append(_check("元数据", False, "缺少或无法解析 manifest.json"))
        return _inspect_result(name, tar_path, stage, None, checks, errors, warnings,
                               skipped, dep)

    # ---- 元数据签名（防篡改）----
    # 开源版无授权密钥：不校验发行包签名，仅依赖下方的 sha256 / 架构 / 版本校验。
    # 请仅从可信来源（官方仓库发布的更新包）获取升级包。
    checks.append(_check("元数据签名", True, "开源版跳过签名校验", level="warn"))

    pkg_ver = str(manifest.get("version") or "")
    pkg_ed = str(manifest.get("edition") or "")
    pkg_build = str(manifest.get("build") or "")
    fmt = manifest.get("format")
    checks.append(_check(
        "元数据", manifest.get("product") == "pvewebtools",
        "版本 %s（%s）%s / 架构 %s"
        % (pkg_ver or "未知", (pkg_ed or "未知").upper(),
           ("，构建 " + pkg_build) if pkg_build else "",
           manifest.get("arch") or "未知")))
    if manifest.get("product") != "pvewebtools":
        errors.append("该包不是 pvewebtools 发行包")
    if not isinstance(fmt, int) or fmt > MANIFEST_FORMAT:
        errors.append("升级包元数据格式（format=%s）高于本程序支持（%d），请先升级到中间版本"
                      % (fmt, MANIFEST_FORMAT))

    # ---- 架构 ----
    # 元数据里已不再记录编译期运行时版本（避免对外暴露实现细节），故此处只校验架构。
    arch_ok = str(manifest.get("arch") or "") == _arch()
    checks.append(_check("架构", arch_ok, "包 %s / 本机 %s"
                         % (manifest.get("arch") or "未知", _arch())))
    if not arch_ok:
        errors.append("架构不匹配（包 %s / 本机 %s），该包装上去无法运行"
                      % (manifest.get("arch") or "未知", _arch()))

    # ---- 版本形态（edition） ----
    if cur["edition"] == "pro" and pkg_ed == "lite":
        errors.append("当前是 Pro 版，不允许安装 Lite 包（会丢失「高级玩法」功能）；"
                      "确需降级请用发行包内的 install.sh 手工部署")
        checks.append(_check("版本形态", False, "Pro → Lite 降级被拒绝"))
    elif cur["edition"] == "lite" and pkg_ed == "pro":
        # Lite → Pro：物理上只是多出 toolbox_pro 的 .so，替换时会整体清理旧依赖目录，不会残留冲突
        checks.append(_check("版本形态", True, "Lite → Pro（启用「高级玩法」）"))
    else:
        checks.append(_check("版本形态", True, "%s → %s"
                             % ((cur["edition"] or "未知").upper(), (pkg_ed or "未知").upper())))

    # ---- 密钥对同源（开源版无密钥对概念，跳过） ----
    key_fp = str(manifest.get("key_fp") or "")
    checks.append(_check("密钥对", True, "开源版跳过密钥校验", level="warn"))

    # ---- 内容完整性 ----
    main = manifest.get("main")
    main_sha = str(main.get("sha256") or "") if isinstance(main, dict) else ""
    actual_sha = _sha256_file(stage / BINARY_NAME)
    sha_ok = bool(main_sha) and main_sha == actual_sha
    checks.append(_check("主程序校验", sha_ok, ("sha256 匹配" if sha_ok else
                          "不匹配（期望 %s… / 实际 %s…）"
                          % (main_sha[:12] or "无", actual_sha[:12]))))
    if not main_sha:
        errors.append("升级包元数据缺少主程序校验和（main.sha256）")
    elif not sha_ok:
        errors.append("主程序校验和不匹配，升级包可能已损坏或被篡改")

    if manifest.get("has_dist"):
        dist_dir = stage / DIST_NAME
        if not dist_dir.is_dir():
            errors.append("元数据声明含 %s 目录，但包内未找到" % DIST_NAME)
            checks.append(_check("依赖目录", False, "缺少 " + DIST_NAME))
        else:
            digest = _dist_digest(dist_dir)
            dok = digest == str(manifest.get("dist_digest") or "")
            checks.append(_check("依赖目录", dok, "聚合摘要匹配" if dok else "聚合摘要不匹配"))
            if not dok:
                errors.append("依赖目录（%s）内容与元数据不一致" % DIST_NAME)

    # ---- 版本新旧 ----
    # 版本号只表达功能版本；git 构建串（build）用于区分「同版本号的不同构建」——
    # 同一标签下又提交了几次时，新包 build 会变而 version 不变，这属于正常的新构建，不该拦。
    diff = _cmp_version(pkg_ver, cur["version"])
    cur_build = str(cur.get("build") or "")
    edition_switch = bool(pkg_ed) and pkg_ed != (cur["edition"] or "")
    new_build = bool(pkg_build) and bool(cur_build) and pkg_build != cur_build
    if diff > 0:
        checks.append(_check("版本", True, "%s → %s（升级）" % (cur["version"], pkg_ver)))
    elif diff == 0:
        if force:
            checks.append(_check("版本", True, "同版本重装（已确认）", level="warn"))
            warnings.append("目标版本与本机相同（%s），将重装该版本" % pkg_ver)
        elif edition_switch:
            checks.append(_check("版本", True, "同版本切换形态（%s → %s）"
                                 % ((cur["edition"] or "未知").upper(), pkg_ed.upper())))
        elif new_build:
            checks.append(_check("版本", True, "同版本新构建（%s → %s）"
                                 % (cur_build, pkg_build), level="warn"))
            warnings.append("版本号与本机相同（%s），但构建串不同，将安装新构建" % pkg_ver)
        else:
            errors.append("目标版本与本机相同（%s）；如确需重装请勾选「允许重装同版本」" % pkg_ver)
            checks.append(_check("版本", False, "同版本重装未确认"))
    else:
        if allow_downgrade:
            checks.append(_check("版本", True, "%s → %s（降级，已确认）"
                                 % (cur["version"], pkg_ver), level="warn"))
            warnings.append("目标版本（%s）低于本机（%s），将执行降级" % (pkg_ver, cur["version"]))
        else:
            errors.append("目标版本（%s）低于本机（%s）；如确需降级请勾选「允许降级」"
                          % (pkg_ver, cur["version"]))
            checks.append(_check("版本", False, "降级未确认"))

    # ---- 机器上限变化（信息项，不阻断） ----
    try:
        pkg_max = int(manifest.get("hard_max") or 0)
    except (TypeError, ValueError):
        pkg_max = 0
    cur_max = 0
    if pkg_max and pkg_max != cur_max:
        warnings.append("本版本机器上限将由 %d 变为 %d（含控制机）；已授权机器不受影响，"
                        "超出上限后无法再添加新机器" % (cur_max, pkg_max))
        checks.append(_check("机器上限", True, "%d → %d" % (cur_max, pkg_max), level="warn"))

    # 环境不支持自助升级只降级为告警：校验本身是只读体检，不改动任何文件，源码版/开发
    # 环境也应能看完整报告；「能否真正替换程序」由结果里的 can_apply 表达，且 apply()
    # 照旧在最前面用 can_self_upgrade 拦截，安全性不依赖这条 error。
    if not dep["can_self_upgrade"]:
        warnings.append("当前环境不支持自助升级：" + dep["reason"])
        checks.append(_check("自升级能力", True, "不支持自助升级（仅可上传与校验）", level="warn"))

    return _inspect_result(name, tar_path, stage, manifest, checks, errors, warnings,
                           skipped, dep)


def _inspect_result(name: str, tar_path: str, stage: Any, manifest: Any, checks: list, errors: list, warnings: list, skipped: bool, dep: Any = None) -> dict:
    try:
        size = tar_path.stat().st_size
    except OSError:
        size = 0
    dep = dep or deployment()
    return {
        "pkg": name,
        "size": size,
        "staged": str(stage),
        "manifest": manifest,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "skipped": skipped[:20],
        "ok": not errors,
        "can_apply": bool(dep.get("can_self_upgrade")),
        "apply_blocked_reason": dep.get("reason") or "",
        "current": version_info(),
    }


# ---- 执行升级（脱离 cgroup） ----

def _write_state(state: dict, mode: str = '', frm: str = '', to: str = '', message: str = '') -> None:
    data = {"state": state, "mode": mode, "from": frm, "to": to,
            "ts": int(time.time()), "message": message}
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def _render_script(mode: Any, frm: Any, to: Any, port: int, stage: Any) -> Any:
    """生成升级/回滚执行脚本。

    脚本在独立 transient unit 中运行，自行把输出追加到 upgrade.log 并更新 state 文件，
    因此产品端进程被重启打断也不影响升级本身的完成与结果记录。
    """
    q = shlex.quote
    return """#!/bin/bash
# 由 pvewebtools「系统升级」模块自动生成，请勿手工修改。
# 该脚本由 systemd-run 起的独立 unit 执行，已脱离 pvewebtools.service 的 cgroup，
# 因此 systemctl stop pvewebtools 不会把它一并杀掉。
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
APP_DIR={app_dir}
UNIT={unit}
PORT={port}
LOG={log}
STATE={state}
INSTALLED_AT={installed_at}
STAGE={stage}
BACKUP={backup}
MODE={mode}
FROM={frm}
TO={to}

# 日志必须显式写进 $LOG：该 unit 是以 StandardOutput=null / StandardError=null 起的，
# 只 echo 到标准输出会被整个丢弃，upgrade.log 永远为空（界面「升级日志」也就永远是「暂无」）
log() {{ echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }}
# 日志轮转：超过 1MB 时只保留尾部，避免多次升级后无限增长
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
  tail -c 262144 "$LOG" > "$LOG.tmp" 2>/dev/null && mv -f "$LOG.tmp" "$LOG"
fi
write_state() {{
  printf '{{"state":"%s","mode":"%s","from":"%s","to":"%s","ts":%s,"message":"%s"}}\\n' \\
    "$1" "$MODE" "$FROM" "$TO" "$(date +%s)" "$2" > "$STATE"
}}
# 记录当前版本在本机的「升级/部署时间」（UTC）：自升级或回滚成功时调用，
# 前端据此显示「升级时间」，比编译时间更能反映本机这版实际何时上线。
write_installed() {{
  printf '{{"version":"%s","at":"%s"}}\\n' "$TO" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$INSTALLED_AT"
}}
stop_service() {{
  systemctl stop "$UNIT" 2>/dev/null || true
  pkill -f "$APP_DIR/pvewebtools" 2>/dev/null || true
  sleep 1
}}
health_check() {{
  local i
  for i in $(seq 1 {timeout}); do
    sleep 1
    if systemctl is-active --quiet "$UNIT" && ss -ltn 2>/dev/null | grep -q ":$PORT "; then
      return 0
    fi
  done
  return 1
}}
install_binary() {{
  # 先写 .new 再 mv 覆盖：运行中的二进制不允许直接 cp 覆盖（ETXTBSY），rename 才安全
  install -m 755 "$1" "$APP_DIR/pvewebtools.new" || return 1
  mv -f "$APP_DIR/pvewebtools.new" "$APP_DIR/pvewebtools" || return 1
  return 0
}}
restore_backup() {{
  [ -f "$BACKUP/pvewebtools" ] || return 1
  rm -rf "$APP_DIR/pvewebtools.dist"
  if [ -d "$BACKUP/pvewebtools.dist" ]; then cp -a "$BACKUP/pvewebtools.dist" "$APP_DIR/pvewebtools.dist"; fi
  install_binary "$BACKUP/pvewebtools"
}}

log "=== $MODE：$FROM -> $TO ==="
write_state running "正在停止服务"
stop_service

if [ "$MODE" = "upgrade" ]; then
  log "备份当前版本到 $BACKUP"
  rm -rf "$BACKUP"
  mkdir -p "$BACKUP"
  cp -a "$APP_DIR/pvewebtools" "$BACKUP/pvewebtools" || {{ write_state error "备份当前主程序失败，未做任何改动"; log "备份失败，中止"; exit 1; }}
  if [ -d "$APP_DIR/pvewebtools.dist" ]; then cp -a "$APP_DIR/pvewebtools.dist" "$BACKUP/pvewebtools.dist"; fi
  printf '%s\\n' "$FROM" > "$BACKUP/VERSION"

  log "替换程序文件"
  write_state running "正在替换程序文件"
  rm -rf "$APP_DIR/pvewebtools.dist"
  if [ -d "$STAGE/pvewebtools.dist" ]; then cp -a "$STAGE/pvewebtools.dist" "$APP_DIR/pvewebtools.dist"; fi
  if ! install_binary "$STAGE/pvewebtools"; then
    log "写入新主程序失败，回滚"
    restore_backup
    systemctl start "$UNIT" 2>/dev/null || true
    write_state error "写入新主程序失败，已回滚到 $FROM"
    exit 1
  fi
else
  if [ ! -f "$BACKUP/pvewebtools" ]; then
    log "没有可回滚的版本快照"
    systemctl start "$UNIT" 2>/dev/null || true
    write_state error "没有可回滚的版本快照"
    exit 1
  fi
  log "从快照恢复程序文件"
  write_state running "正在恢复上一版本"
  restore_backup || {{ write_state error "恢复上一版本失败"; exit 1; }}
fi

log "启动服务并做健康检查（最长 {timeout}s）"
write_state running "正在启动服务"
systemctl start "$UNIT" 2>/dev/null || true
if health_check; then
  log "$MODE 成功"
  write_state done "$MODE 完成"
  write_installed
  exit 0
fi

log "健康检查失败，回滚上一版本"
write_state running "启动失败，正在回滚"
stop_service
if restore_backup; then
  systemctl start "$UNIT" 2>/dev/null || true
  sleep 3
  if systemctl is-active --quiet "$UNIT"; then
    write_state error "升级失败，已自动回滚到 $FROM（详见 $LOG）"
  else
    write_state error "升级失败且回滚后仍未启动，请手工检查：journalctl -u $UNIT -n 50"
  fi
else
  write_state error "升级失败，且未找到可用的版本快照，请手工恢复"
fi
exit 1
""".format(
        app_dir=q(str(APP_DIR)), unit=q(UNIT), port=int(port or 8899),
        log=q(str(LOG_FILE)), state=q(str(STATE_FILE)),
        installed_at=q(str(INSTALLED_AT_FILE)),
        stage=q(str(stage)), backup=q(str(ROLLBACK_DIR)),
        mode=q(mode), frm=q(str(frm)), to=q(str(to)),
        timeout=_HEALTH_TIMEOUT,
    )


def _run_detached(script_path: str) -> None:
    """把脚本交给独立 transient unit 执行（脱离 pvewebtools.service 的 cgroup）。"""
    if not shutil.which("systemd-run"):
        raise UpgradeError("系统缺少 systemd-run，无法安全执行升级")
    # 上次同名单例若失败残留，先清掉，否则 --unit= 重名会启动失败
    try:
        subprocess.run(["systemctl", "reset-failed", UPGRADE_UNIT_FILE],
                       capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
    cmd = [
        "systemd-run",
        # --no-block 必需：不加时 systemd-run 会同步等这次启动作业完成，而 unit 是 Type=oneshot，
        # oneshot 的启动作业以「进程退出」为完成条件 —— 等于一直等到脚本跑完（含最长 40s 健康检查）。
        # 可脚本第一步就 stop 本服务，于是 HTTP 响应永远发不出去，前端只能收到连接中断
        # （Failed to fetch），被误判成「升级未启动」。加了这个只负责入队，毫秒级返回。
        "--no-block",
        "--unit=" + UPGRADE_UNIT,
        "--collect",
        "--property=Type=oneshot",
        "--property=TimeoutStartSec=1800",
        "--property=StandardOutput=null",
        "--property=StandardError=null",
        "/bin/bash", str(script_path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise UpgradeError("启动升级任务失败: %s" % e)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()
        raise UpgradeError("启动升级任务失败: %s" % (detail[-500:] or "未知原因"))


def _prepare_script(mode: Any, frm: Any, to: Any, stage: Any) -> Any:
    """写升级脚本并返回路径。

    **执行顺序要注意**：脚本是在提交任务时、由**当前正在运行的那个二进制**现场渲染的
    （不是预先放在发行包里）。所以凡属「升级脚本本身」的修复 —— 脚本内容、systemd-run
    参数、日志写法 —— 装好新包后的**第一次操作仍是老进程用老模板跑的**，要等下一次操作
    才生效。排障时可直接看落盘的脚本确认当时用的是哪版模板：

        grep -h "^log()" /var/tmp/pvewebtools-upgrade/upgrade-*.sh
    """
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    dep = deployment()
    script = WORK_DIR / ("%s-%s.sh" % (mode, int(time.time())))
    script.write_text(_render_script(mode, frm, to, dep["port"], stage), encoding="utf-8")
    os.chmod(script, 0o700)
    return script


def apply(name: str, force: bool = False, allow_downgrade: bool = False) -> dict:
    """校验并执行升级。返回 {started, ...}；校验不通过抛 UpgradeError。"""
    dep = deployment()
    if not dep["can_self_upgrade"]:
        raise UpgradeError("当前环境不支持自升级：" + dep["reason"])

    result = inspect(name, force=force, allow_downgrade=allow_downgrade)
    if not result["ok"]:
        raise UpgradeError("升级包校验未通过：" + "；".join(result["errors"]))

    cur = version_info()
    pkg_ver = str((result["manifest"] or {}).get("version") or "")
    # 先把状态置为排队，避免产品端在脚本真正起来之前读到上一轮的旧状态
    _write_state("queued", "upgrade", cur["version"], pkg_ver, "已提交升级任务")
    script = _prepare_script("upgrade", cur["version"], pkg_ver, Path(result["staged"]))
    try:
        _run_detached(script)
    except UpgradeError as e:
        _write_state("error", "upgrade", cur["version"], pkg_ver, str(e))
        raise
    return {"started": True, "mode": "upgrade", "from": cur["version"], "to": pkg_ver,
            "port": dep["port"], "message": "升级任务已启动，服务将短暂重启"}


def rollback() -> dict:
    """回滚到升级前的版本（快照在 /etc/pvewebtools/rollback）。"""
    dep = deployment()
    if not dep["can_self_upgrade"]:
        raise UpgradeError("当前环境不支持自升级：" + dep["reason"])
    if not (ROLLBACK_DIR / BINARY_NAME).is_file():
        raise UpgradeError("没有可回滚的版本快照（仅升级过一次以上才有）")

    cur = version_info()
    target = ""
    try:
        target = (ROLLBACK_DIR / "VERSION").read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        target = ""
    _write_state("queued", "rollback", cur["version"], target, "已提交回滚任务")
    script = _prepare_script("rollback", cur["version"], target or "上一版本", ROLLBACK_DIR)
    try:
        _run_detached(script)
    except UpgradeError as e:
        _write_state("error", "rollback", cur["version"], target, str(e))
        raise
    return {"started": True, "mode": "rollback", "from": cur["version"], "to": target,
            "port": dep["port"], "message": "回滚任务已启动，服务将短暂重启"}


# ---- 状态与日志 ----

def state() -> dict:
    """升级状态（含可回滚信息）。"""
    data = {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    st = str(data.get("state") or "")
    message = str(data.get("message") or "")
    ts = int(data.get("ts") or 0)
    # 升级脚本被强杀（如所在 unit 被直接干掉）时状态会永远停在 running：
    # 超过 15 分钟视为中断，避免前端一直等下去
    if st in ("queued", "running") and ts and time.time() - ts > 900:
        st = "error"
        message = (message + "（任务超时，可能已中断，详见 %s）" % LOG_FILE).strip()
    available = (ROLLBACK_DIR / BINARY_NAME).is_file()
    roll_ver = ""
    if available:
        try:
            roll_ver = (ROLLBACK_DIR / "VERSION").read_text(encoding="utf-8").strip().splitlines()[0].strip()
        except (OSError, IndexError):
            roll_ver = ""
    return {
        "state": st,
        "mode": str(data.get("mode") or ""),
        "from": str(data.get("from") or ""),
        "to": str(data.get("to") or ""),
        "ts": ts,
        "message": message,
        "running": st in ("queued", "running"),
        "rollback_available": available,
        "rollback_version": roll_ver,
    }


def log_tail(lines: int = 200) -> Any:
    """升级日志尾部（脚本以追加方式写入，服务重启后仍可查阅）。"""
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-max(1, int(lines)):])


def status(port_hint: int = 0) -> dict:
    """汇总给前端的完整状态：当前版本 / 部署形态 / 升级状态 / 已上传包。"""
    return {
        "current": version_info(),
        "deployment": deployment(port_hint),
        "state": state(),
        "packages": list_packages(),
        # 不带 log：界面已不展示升级日志，而升级期间前端每 2 秒轮询一次这个接口，
        # 没必要每次把日志尾部一起搬过去；需要时走 /api/upgrade/log 或直接 tail 文件
    }
