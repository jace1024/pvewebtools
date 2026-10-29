"""任务取消回归：使用事件控制排队和启动时序，不运行 bash/SSH 或真实运维操作。"""

import queue
import threading

import pytest

from core.web import jobs


@pytest.fixture
def gate(monkeypatch, tmp_path):
    pending = queue.Queue()
    release = threading.Event()
    cleanups = []
    workers = []
    unexpected = []
    monkeypatch.setattr(jobs, "_jobs", {})
    monkeypatch.setattr(jobs, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(jobs, "_exec_sem", threading.Semaphore(1))
    monkeypatch.setattr(jobs, "_ensure_gate", lambda node: pending)
    monkeypatch.setattr(jobs, "_cleanup_job_tmp", lambda *args: cleanups.append(args))

    def unexpected_process(*args, **kwargs):
        unexpected.append(args)
        raise AssertionError("测试不得启动真实子进程")

    monkeypatch.setattr(jobs.subprocess, "Popen", unexpected_process)
    monkeypatch.setattr(jobs.subprocess, "run", unexpected_process)

    def start(node=None):
        worker = threading.Thread(target=jobs._node_gate, args=(node, pending), daemon=True)
        workers.append(worker)
        worker.start()

    def finish():
        release.set()
        pending.put(None)
        for worker in workers:
            worker.join(timeout=5)
            assert not worker.is_alive(), "任务闸门未结束"

    yield start, finish, release, cleanups
    finish()
    assert unexpected == [], "测试执行了未预期的进程调用"


def _submit(kind, calls, node=None):
    if kind == "callback":
        return jobs.track_event("test", "待取消任务", node, lambda job: calls.append(job.id) or True)
    return jobs.submit("test", "待取消任务", "must not execute", node=node)


def _assert_cancelled(job_id):
    snapshot = jobs.get(job_id)
    assert snapshot["status"] == "failed"
    assert snapshot["returncode"] != 0
    assert snapshot["ended_at"] is not None
    assert jobs.load_log(job_id)["status"] == "failed"
    assert jobs.get_raw(job_id)._new_data.is_set()


@pytest.mark.parametrize("kind", ["script", "callback"])
def test_cancel_queued_job_finishes_immediately_and_never_runs(gate, monkeypatch, kind):
    start, finish, release, cleanups = gate
    entered = threading.Event()
    calls = []

    def blocker(job):
        entered.set()
        assert release.wait(5)
        return True

    monkeypatch.setattr(jobs, "_run_script", lambda job, *args: calls.append(job.id))
    first = jobs.track_event("blocker", "占用节点队列", "pve1", blocker)
    start("pve1")
    assert entered.wait(2)
    cancelled = _submit(kind, calls, node="pve1")
    assert jobs.kill(cancelled)[0] is True
    _assert_cancelled(cancelled)
    assert jobs.kill(cancelled)[0] is False
    assert cleanups == []  # 未执行的远端任务不能发 SSH 清理或强杀。
    finish()
    assert calls == []
    assert jobs.get(first)["status"] == "success"
    _assert_cancelled(cancelled)


@pytest.mark.parametrize("kind", ["script", "callback"])
def test_cancel_while_waiting_for_global_slot_never_runs(gate, monkeypatch, kind):
    start, finish, release, _cleanups = gate
    waiting = threading.Event()
    calls = []

    class BlockedSemaphore:
        def __enter__(self):
            waiting.set()
            assert release.wait(5)

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(jobs, "_exec_sem", BlockedSemaphore())
    monkeypatch.setattr(jobs, "_run_script", lambda job, *args: calls.append(job.id))
    cancelled = _submit(kind, calls)
    start()
    assert waiting.wait(2)
    assert jobs.kill(cancelled)[0] is True
    _assert_cancelled(cancelled)
    finish()
    assert calls == []


class FakeInput:
    def __init__(self):
        self.writes = []
        self.written = threading.Event()

    def write(self, value):
        self.writes.append(value)
        self.written.set()
        return len(value)

    def flush(self):
        pass

    def close(self):
        pass


class FakeProcess:
    def __init__(self):
        self.pid = 123456
        self.stdin = FakeInput()
        self.stdout = iter(())
        self.returncode = None
        self.killed = threading.Event()

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9
        self.killed.set()

    def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _fake_process_group(monkeypatch):
    def unavailable(*args):
        raise OSError("模拟进程组信号失败，使用 Popen.kill 兜底")

    monkeypatch.setattr(jobs.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(jobs.os, "killpg", unavailable, raising=False)
    monkeypatch.setattr(jobs.signal, "SIGKILL", 9, raising=False)


def test_cancel_during_process_creation_kills_before_feeding_script(gate, monkeypatch):
    start, finish, release, _cleanups = gate
    creating = threading.Event()
    proc = FakeProcess()

    def popen(*args, **kwargs):
        creating.set()
        assert release.wait(5)
        return proc

    _fake_process_group(monkeypatch)
    monkeypatch.setattr(jobs.subprocess, "Popen", popen)
    cancelled = jobs.submit("test", "进程创建竞态", "must not execute")
    start()
    assert creating.wait(2)
    assert jobs.get_raw(cancelled).proc is None
    assert jobs.kill(cancelled)[0] is True
    finish()
    assert proc.killed.is_set()
    assert proc.stdin.writes == []
    _assert_cancelled(cancelled)


def test_cancel_after_gate_admission_skips_process_creation(gate, monkeypatch):
    start, finish, release, _cleanups = gate
    preparing = threading.Event()

    def prepare(env_params):
        preparing.set()
        assert release.wait(5)
        return ""

    monkeypatch.setattr(jobs, "_param_summary", prepare)
    cancelled = jobs.submit("test", "启动准备阶段取消", "must not execute")
    start()
    assert preparing.wait(2)
    assert jobs.kill(cancelled)[0] is True
    finish()
    _assert_cancelled(cancelled)


def test_cancel_running_process_still_terminates_it(gate, monkeypatch):
    start, finish, _release, _cleanups = gate
    proc = FakeProcess()

    def output():
        assert proc.killed.wait(5)
        yield "terminated\n"

    proc.stdout = output()
    _fake_process_group(monkeypatch)
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *args, **kwargs: proc)
    cancelled = jobs.submit("test", "执行中取消", "simulated script")
    start()
    assert proc.stdin.written.wait(2)
    assert jobs.kill(cancelled)[0] is True
    finish()
    assert proc.killed.is_set()
    _assert_cancelled(cancelled)


def test_cancel_running_callback_cannot_be_overwritten_by_success(gate):
    start, finish, release, _cleanups = gate
    entered = threading.Event()

    def callback(job):
        entered.set()
        assert release.wait(5)
        return True

    cancelled = jobs.track_event("test", "执行中回调取消", None, callback)
    start()
    assert entered.wait(2)
    assert jobs.kill(cancelled)[0] is True
    finish()
    _assert_cancelled(cancelled)


def test_uncancelled_script_finishes_successfully(gate, monkeypatch):
    start, finish, _release, cleanups = gate
    proc = FakeProcess()
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *args, **kwargs: proc)
    job_id = jobs.submit("test", "正常执行", "simulated script")
    start()
    finish()
    assert proc.stdin.writes == ["simulated script"]
    assert not proc.killed.is_set()
    assert jobs.get(job_id)["status"] == "success"
    assert jobs.load_log(job_id)["status"] == "success"
    assert cleanups == [(job_id, None)]
