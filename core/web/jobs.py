"""异步任务执行器。

工具箱中的耗时操作（apt 升级、镜像转换等）在后台线程执行，
前端通过轮询 /api/jobs/<id> 获取增量日志与状态。
仅标准库实现：threading + subprocess。

任务结束后日志落盘到 logs/<id>.json，服务重启后仍可查询历史。
"""

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
import queue
from pathlib import Path

from core import paths
from core.pve import pve_api

# 日志落盘目录：源码运行落仓库根 logs/（打包运行落资源目录），重启后历史可查
LOG_DIR = Path(paths.PROJECT_ROOT) / "logs"
# 内存与磁盘各保留的任务上限，超出清理最旧的已结束任务
MAX_JOBS = 200

# 远程节点 SSH 连接复用（ControlMaster）：同一节点复用已建立的连接，避免逐项采集重复握手。
# ServerAliveInterval 保活：dd 全盘覆写、大容量备份还原等任务可能跑几十分钟，
# 没有心跳会被中间设备或服务端判定为空闲连接而断开，导致任务中途失败。
_SSH_CTRL = ["-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/pvewebtools-ssh-%C", "-o", "ControlPersist=300",
             "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=20"]

_jobs = {}
_lock = threading.Lock()

# ---- 执行并发控制（B3 并发限流 + B5 同节点互斥）----
# 每个节点（含本机 None）有独立「闸门」线程，保证同节点脚本严格串行，避免并发改同一节点配置互相踩踏；
# 不同节点的闸门并行，互不阻塞（节点 A 排队不影响节点 B）；全局 _exec_sem 再兜一层总并发上限，
# 防止瞬间拉起过多脚本拖垮本机/网络。
MAX_WORKERS = 8
_exec_sem = threading.Semaphore(MAX_WORKERS)
_node_queues = {}
_node_gates = {}
_node_gate_lock = threading.Lock()


def _ensure_gate(node):
    with _node_gate_lock:
        q = _node_queues.get(node)
        if q is None:
            q = queue.Queue()
            _node_queues[node] = q
            _node_gates[node] = True
            threading.Thread(target=_node_gate, args=(node, q), daemon=True).start()
    return q


def _node_gate(node, q):
    """按节点串行的执行闸门：从本节点队列取任务，拿全局并发信号量后执行。"""
    while True:
        item = q.get()
        try:
            if item is None:
                return
            job, script, env_params, fn = item
            with _exec_sem:
                # 取消可能发生在本节点排队或等待全局并发名额时；执行权和取消
                # 共用生命周期锁，确保已取消的任务不会再次启动。
                with job._state_lock:
                    if job.killed:
                        continue
                    job._started = True
                if fn is not None:
                    # track_event 型轻量任务：已在节点闸门（本节点串行）内执行回调
                    try:
                        ok = bool(fn(job))
                    except Exception as e:
                        job.append(f"[错误] {e}")
                        ok = False
                    with job._state_lock:
                        if job.killed:
                            ok = False
                        job.status = "success" if ok else "failed"
                        job.returncode = 0 if ok else 1
                        job.ended_at = time.time()
                    _persist(job)
                else:
                    _run_script(job, script, env_params, node)
        finally:
            q.task_done()


class Job:
    def __init__(self, tool_id, title, node=None):
        self.id = uuid.uuid4().hex[:12]
        self.tool_id = tool_id
        self.title = title
        self.node = node               # 执行节点：None=本机
        self.status = "running"        # running | success | failed
        self.lines = []                # 累积输出行
        self.returncode = None
        self.pid = None                # 执行进程 PID（强杀按钮展示用，重启后历史无）
        self.proc = None               # Popen 对象，强杀时定位进程组
        self.killed = False            # 被用户强杀标记，结束时写入日志
        self._started = False          # 私有执行阶段；排队任务对外仍兼容 running
        self._state_lock = threading.Lock()  # 取消、开始执行、发布进程和终态互斥
        self.started_at = time.time()
        self.ended_at = None
        self._line_lock = threading.Lock()
        self._new_data = threading.Event()   # 日志新增/状态变更信号，供 SSE 实时推送唤醒

    def append(self, line):
        with self._line_lock:
            self.lines.append(line)
        self._new_data.set()   # 有新日志，唤醒 SSE 推送

    def snapshot(self, offset=0):
        """返回 offset 之后的增量日志，供前端轮询"""
        with self._line_lock:
            return {
                "id": self.id,
                "tool_id": self.tool_id,
                "title": self.title,
                "node": self.node,
                "status": self.status,
                "returncode": self.returncode,
                "pid": self.pid,
                "offset": len(self.lines),
                "lines": self.lines[offset:],
                "started_at": self.started_at,
                "ended_at": self.ended_at,
            }

    def to_dict(self):
        """落盘格式：元数据 + 全量日志"""
        snap = self.snapshot()
        snap.pop("offset")
        snap.pop("pid")  # 进程 PID 只在运行期有意义，不落盘
        return snap


def _persist(job):
    """任务结束后写 logs/<id>.json；写失败不影响主流程"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = LOG_DIR / (job.id + ".tmp")
        tmp.write_text(json.dumps(job.to_dict(), ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(LOG_DIR / (job.id + ".json"))  # 原子替换，避免写一半的脏文件
        _cleanup_log_files()
        job._new_data.set()   # 任务终态，唤醒 SSE 推送 done
    except OSError:
        pass


def _cleanup_log_files():
    """磁盘日志超限时按修改时间删最旧的"""
    try:
        files = sorted(LOG_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for f in files[:-MAX_JOBS]:
            f.unlink()
    except OSError:
        pass


def _load_history():
    """启动时把历史日志读回内存，列表页直接可查"""
    if not LOG_DIR.is_dir():
        return
    records = []
    for f in LOG_DIR.glob("*.json"):
        try:
            records.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    records.sort(key=lambda r: r.get("started_at", 0))
    for r in records[-MAX_JOBS:]:
        # 损坏/旧格式日志可能缺 id：跳过该条而非让整段加载崩溃（否则服务启动即失败）
        jid = r.get("id")
        if not jid:
            continue
        job = Job(r.get("tool_id", ""), r.get("title", ""), r.get("node"))
        job.id = jid
        job.status = r.get("status", "failed")
        job.returncode = r.get("returncode")
        job.lines = r.get("lines", [])
        job.started_at = r.get("started_at", 0)
        job.ended_at = r.get("ended_at")
        _jobs[job.id] = job


_load_history()


def _cleanup_old_jobs():
    finished = [j for j in _jobs.values() if j.status != "running"]
    finished.sort(key=lambda j: j.started_at)
    while len(_jobs) > MAX_JOBS and finished:
        oldest = finished.pop(0)
        _jobs.pop(oldest.id, None)


# 敏感参数名关键字：日志参数摘要里脱敏为 ***，避免密码/密钥落盘
_SENSITIVE_KEYS = ("password", "passwd", "ssh_key", "token", "secret")


def _param_summary(env_params):
    """参数摘要行：失败排查时能直接看到当时用了什么参数"""
    if not env_params:
        return ""
    parts = []
    for k, v in env_params.items():
        if any(s in k.lower() for s in _SENSITIVE_KEYS):
            v = "***"
        parts.append(f"{k}={v}")
    return "[参数] " + " ".join(parts)


def _run_script(job, script, env_params, node=None):
    """在子进程中跑 bash 脚本，逐行捕获 stdout+stderr。

    脚本一律经 stdin 传入（bash -s），不拼进命令行，ps 里看不到脚本全文；
    参数只通过环境变量传入（PT_ 前缀），脚本内容固定不拼接用户输入，
    从根源上避免命令注入。

    指定 node（集群节点名）时走集群免密 SSH 在目标节点执行，
    参数用 shlex.quote 注入远端 env，同样不给注入留口子。
    """
    env = dict(os.environ)
    for k, v in (env_params or {}).items():
        env["PT_" + k.upper()] = str(v)
    # 任务 ID 注入环境变量：脚本里 pw_job_mktemp_* 据此把临时文件落到任务级目录，
    # 强杀/结束后由 _cleanup_job_tmp 按此 ID 统一回收（远端分支已在命令行单独注入）
    env["PT_JOB_ID"] = job.id
    # 本地执行时也显示真实节点名，与前端下拉框保持一致
    target = f"节点 {node}（SSH）" if node else f"节点 {socket.gethostname().split('.')[0]}"
    job.append(f"[开始] {time.strftime('%Y-%m-%d %H:%M:%S')} 在{target}执行「{job.title}」")
    summary = _param_summary(env_params)
    if summary:
        job.append(summary)
    if node:
        # PT_JOB_ID 写进远端命令行：强杀时在目标节点上 pkill -f 能精确定位到它
        assigns = "PT_JOB_ID=%s" % job.id + " " + " ".join(
            "PT_%s=%s" % (k.upper(), shlex.quote(str(v)))
            for k, v in (env_params or {}).items())
        remote_cmd = "env " + assigns + " bash -s"
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
               *pve_api.ssh_host_opts(node),
               f"root@{pve_api.ssh_host(node)}", remote_cmd]
        job.append(f"[节点] 通过 SSH 在 {node} 上执行")
    else:
        # 脚本经 stdin 传入（与 SSH 远端分支一致）：避免整段脚本出现在进程命令行里，
        # ps 中只显示干净的 bash -s，参数仍走环境变量，注入防护不受影响
        cmd = ["bash", "-s"]
    outcome = "failed"
    try:
        with job._state_lock:
            if job.killed:
                return
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            # 独立进程组：强杀时 killpg 能干掉脚本拉起的子孙进程（apt/sleep 等），不留孤儿
            start_new_session=True,
        )
        with job._state_lock:
            job.proc = proc
            job.pid = proc.pid
            cancelled = job.killed
        # Popen 期间 kill() 可能还拿不到 proc。发布后再次检查，并在任何
        # 脚本写入 stdin 前终止新进程，补上取消与进程创建之间的竞态。
        if cancelled:
            _kill_process(proc)
            proc.wait()
            job.returncode = proc.returncode
            return
        # 脚本要经 stdin 喂给 `bash -s`（远端分支同理，ssh 再转发给远端 bash），
        # 而 bash 是边读边执行的：执行到 apt upgrade 这类长耗时命令时它不再消费 stdin，
        # 期间 stdout 会被子进程输出填满管道（默认 64KB）。若在主线程「先写完脚本再读输出」，
        # 双方互等即成死锁 —— 日志会永远停在 [开始]/[参数] 两行，任务一直显示执行中。
        # compose 类工具（如一键优化，脚本拼接后可到数百 KB）必然踩中这个坑。
        # 故写 stdin 放独立线程，主线程自始至终只负责 drain stdout。
        # 写 stdin 放独立线程；写失败要记录并上报——否则脚本喂一半失败时，
        # bash 可能空跑完返回 0，任务被误判为成功（相当于静默丢失整段脚本）。
        feed_errors = []

        def _feed_stdin(proc=proc):
            try:
                proc.stdin.write(script)
                proc.stdin.flush()
            except BrokenPipeError:
                pass  # 被强杀 / 子进程提前退出：喂不完属正常，不算错误
            except Exception as e:  # 编码失败等真实写入错误，记录下来上报
                feed_errors.append(e)
            finally:
                try:
                    proc.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass

        writer = threading.Thread(target=_feed_stdin, name=f"job-stdin-{job.id}", daemon=True)
        writer.start()
        for line in proc.stdout:
            job.append(line.rstrip("\n"))
        proc.wait()
        # 正常结束时脚本已喂完；子进程异常退出时靠管道关闭让线程自行收敛，这里只等一小会
        writer.join(timeout=5)
        job.returncode = proc.returncode
        if feed_errors and not job.killed:
            job.append(f"[错误] 脚本写入失败: {feed_errors[0]}")
            outcome = "failed"
        else:
            outcome = "success" if proc.returncode == 0 else "failed"
    except FileNotFoundError:
        job.append("[错误] 未找到 bash/ssh，本工具需在 PVE 主机上运行")
    except Exception as e:  # 兜底：任何异常都要让任务收敛
        job.append(f"[错误] {e}")
    finally:
        with job._state_lock:
            job.ended_at = time.time()
            job.status = "failed" if job.killed else outcome
        if job.killed:
            job.append("[强杀] 任务已被用户强制结束（进程组已终止；如为远端任务且远端仍在跑，请到目标节点确认）")
        mark = "[完成]" if job.status == "success" else "[失败]"
        job.append(f"{mark} 耗时 {job.ended_at - job.started_at:.1f} 秒，"
                   f"返回码 {job.returncode if job.returncode is not None else '-'}")
        _persist(job)  # 落盘，重启后仍可查询
        _cleanup_job_tmp(job.id, job.node)  # 回收该任务落下的临时文件（正常结束同样清理）


def _cleanup_job_tmp(job_id, node=None):
    """回收该任务遗留的临时文件/目录（/var/tmp/pvewebtools-job-<id>）。

    与工具箱 COMMON_HELPERS 的 pw_job_tmp_root 约定一致：脚本里所有 pw_job_mktemp_*
    产生的临时文件都落在这个目录下，强杀/结束后在此统一回收。
    本地任务直接删目录；远端任务 SSH 到目标节点删。best-effort，任何失败都不阻断主流程。
    """
    if not job_id:
        return
    target = f"/var/tmp/pvewebtools-job-{job_id}"
    try:
        if node:
            subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
                 *pve_api.ssh_host_opts(node),
                 f"root@{pve_api.ssh_host(node)}", f"rm -rf {target}"],
                capture_output=True, timeout=15)
        else:
            shutil.rmtree(target, ignore_errors=True)
    except Exception:
        pass


def _kill_process(proc):
    """尽可能终止本地进程组；取消请求和进程刚创建后的兜底共用。"""
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (AttributeError, OSError):
            try:
                proc.kill()
            except OSError:
                pass


def kill(job_id):
    """强制结束运行中的任务，返回 (是否成功, 说明)。

    本地任务：SIGKILL 整个进程组（bash/ssh 及其拉起的 apt 等子孙进程）。
    远端任务：先 SSH 到目标节点按 PT_JOB_ID 标记杀远端进程（只杀本地 ssh
    断连接不会停掉远端脚本），再终结本地 ssh 连接。
    """
    with _lock:
        job = _jobs.get(job_id)
    if job is None:
        return False, "任务不存在或不在运行中"
    with job._state_lock:
        if job.status != "running":
            return False, "任务不存在或不在运行中"
        job.killed = True
        pending = not job._started
        proc = job.proc
        if pending:
            job.status = "failed"
            job.returncode = 1
            job.ended_at = time.time()
    if pending:
        job.append("[取消] 任务在执行前已被用户取消")
        _persist(job)
        return True, "任务已取消（尚未执行）"
    if job.node:
        # pkill 模式里用 [x] 正则小技巧，避免匹配到 pkill 命令自身的命令行
        pat = "PT_JOB_ID=%s[%s]%s" % (job.id[0], job.id[1], job.id[2:])
        try:
            subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
                 *pve_api.ssh_host_opts(job.node),
                 f"root@{pve_api.ssh_host(job.node)}", f"pkill -KILL -f '{pat}' || true"],
                capture_output=True, timeout=15)
        except Exception:
            pass  # 远端杀失败不阻断，接着断本地 ssh 连接
    _kill_process(proc)
    _cleanup_job_tmp(job.id, job.node)  # 立即回收该任务落下的临时文件（finally 兜底仍会再清一次，幂等）
    return True, "强杀信号已发送"


def submit(tool_id, title, script, env_params=None, node=None):
    """提交任务：入队到对应节点闸门，同节点串行、跨节点并行，返回 job_id。"""
    job = Job(tool_id, title, node=node)
    with _lock:
        _jobs[job.id] = job
        _cleanup_old_jobs()
    _ensure_gate(node).put((job, script, env_params, None))
    return job.id


def record_event(tool_id, title, node, ok, lines):
    """记录不走脚本执行的轻量操作（如概览页虚机电源操作），同样可在日志页查询。"""
    job = Job(tool_id, title, node=node)
    job.lines = list(lines)
    job.status = "success" if ok else "failed"
    job.returncode = 0 if ok else 1
    job.ended_at = time.time()
    with _lock:
        _jobs[job.id] = job
        _cleanup_old_jobs()
    _persist(job)
    return job.id


def track_event(tool_id, title, node, fn):
    """跟踪型日志：fn(job) 入队到对应节点闸门，在本节点锁内执行，结束后落盘。"""
    job = Job(tool_id, title, node=node)
    with _lock:
        _jobs[job.id] = job
        _cleanup_old_jobs()
    _ensure_gate(node).put((job, None, None, fn))
    return job.id


def get(job_id, offset=0):
    job = _jobs.get(job_id)
    return job.snapshot(offset) if job else None


def get_raw(job_id):
    """返回 Job 对象本身（SSE 实时推送用，需访问 .lines / ._new_data / .status）；内存中无则返回 None。"""
    return _jobs.get(job_id)


def load_log(job_id):
    """从落盘日志读取已结束任务的快照（内存任务被淘汰后 SSE 兜底回放用）。"""
    try:
        p = LOG_DIR / (job_id + ".json")
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return None


def delete(job_id):
    """删除单条任务日志（内存 + 磁盘），返回是否删除成功"""
    with _lock:
        job = _jobs.pop(job_id, None)
    if job is None or job.status == "running":
        if job is not None:  # 运行中的不许删，放回内存
            with _lock:
                _jobs[job_id] = job
        return False
    try:
        (LOG_DIR / (job_id + ".json")).unlink(missing_ok=True)
    except OSError:
        pass
    return True


def delete_bulk(before_ts=None):
    """批量清理已结束任务日志（内存 + 磁盘），运行中的永不删。

    before_ts 非空时只删开始时间早于该时间戳的（按时间删）；
    为空则全部删除（清空）。返回实际删除条数。
    """
    with _lock:
        victims = [j for j in _jobs.values()
                   if j.status != "running"
                   and (before_ts is None or j.started_at < before_ts)]
        for j in victims:
            _jobs.pop(j.id, None)
    count = 0
    for j in victims:
        try:
            (LOG_DIR / (j.id + ".json")).unlink(missing_ok=True)
        except OSError:
            pass
        count += 1
    return count


def list_jobs():
    """按启动时间倒序返回任务摘要"""
    out = []
    for j in sorted(_jobs.values(), key=lambda x: x.started_at, reverse=True):
        out.append({
            "id": j.id, "tool_id": j.tool_id, "title": j.title,
            "node": j.node, "status": j.status, "pid": j.pid,
            "started_at": j.started_at, "ended_at": j.ended_at,
        })
    return out


def last_success_times(tool_ids):
    """每个工具最近一次成功执行的时间戳，从未成功过为 None（依赖提示用）"""
    out = {}
    with _lock:
        for tid in tool_ids:
            ts = [j.started_at for j in _jobs.values()
                  if j.tool_id == tid and j.status == "success"]
            out[tid] = max(ts) if ts else None
    return out
