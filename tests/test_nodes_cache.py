"""`?fresh=1` 改为后台重采（stale-while-revalidate）的回归用例。

覆盖两条关键语义：
1. 有缓存时手动刷新**立即**返回旧缓存，慢采集转后台，不阻塞 HTTP 线程；
2. 冷启动（无缓存）仍同步回快数据，同时排一次后台全量重采补慢硬件。
"""

import threading
import time

import pytest

from core.web import handlers


@pytest.fixture(autouse=True)
def _reset_nodes_state(monkeypatch):
    """每个用例都从干净缓存开始，避免后台线程把状态带到下一个用例。"""
    monkeypatch.setattr(handlers, "_nodes_cache", {"ts": 0.0, "data": None})
    monkeypatch.setattr(handlers, "_nodes_preheating", False)
    monkeypatch.setattr(handlers, "_nodes_refreshing", False)
    yield
    # 等后台线程收尾，避免 monkeypatch 撤销后它继续写真实模块属性
    for _ in range(100):
        if not handlers._nodes_refreshing:
            break
        time.sleep(0.02)


def test_force_returns_cache_immediately_and_refreshes_in_background(monkeypatch):
    """手动刷新立即回旧缓存；后台线程采完后缓存被新数据替换。"""
    handlers._nodes_cache["data"] = ["old"]
    started, release = threading.Event(), threading.Event()

    def fake_collect(fast_only=False):
        assert fast_only is False        # 手动刷新走全量（含慢硬件）
        started.set()
        release.wait(5)
        return ["new"]

    monkeypatch.setattr(handlers, "collect_nodes", fake_collect)

    t0 = time.time()
    out = handlers.collect_nodes_cached(force=True)
    cost = time.time() - t0

    assert out == ["old"]                # 不等慢采集，直接给旧数据
    assert cost < 1.0
    assert started.wait(2)               # 后台重采已启动

    release.set()
    for _ in range(100):
        if handlers._nodes_cache["data"] == ["new"]:
            break
        time.sleep(0.02)
    assert handlers._nodes_cache["data"] == ["new"]
    assert handlers._nodes_refreshing is False


def test_concurrent_force_only_spawns_one_refresh(monkeypatch):
    """连续点刷新不叠加 SSH 探测：同一时刻只跑一个后台重采任务。"""
    handlers._nodes_cache["data"] = ["old"]
    started, release = threading.Event(), threading.Event()
    calls = []

    def fake_collect(fast_only=False):
        calls.append(fast_only)
        started.set()
        release.wait(5)
        return ["new"]

    monkeypatch.setattr(handlers, "collect_nodes", fake_collect)

    assert handlers.collect_nodes_cached(force=True) == ["old"]
    assert started.wait(2)
    # 重采进行中：再点刷新只回缓存，不再开新线程
    assert handlers.collect_nodes_cached(force=True) == ["old"]
    assert handlers._trigger_nodes_refresh() is False
    release.set()
    for _ in range(100):
        if not handlers._nodes_refreshing:
            break
        time.sleep(0.02)
    assert len(calls) == 1


def test_cold_start_returns_fast_data_and_triggers_background(monkeypatch):
    """缓存为空时同步采快数据保证首屏不空白，慢硬件交给后台。"""
    calls = []

    def fake_collect(fast_only=False):
        calls.append(fast_only)
        return ["quick"] if fast_only else ["full"]

    monkeypatch.setattr(handlers, "collect_nodes", fake_collect)

    assert handlers.collect_nodes_cached() == ["quick"]
    assert calls[0] is True              # 首次只采快数据

    for _ in range(100):
        if handlers._nodes_cache["data"] == ["full"]:
            break
        time.sleep(0.02)
    assert handlers._nodes_cache["data"] == ["full"]   # 后台补齐慢数据
