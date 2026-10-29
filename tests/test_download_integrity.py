"""后台下载只有完整接收后才发布；使用真实 HTTPResponse 解析器覆盖提前 EOF。"""

import gzip
import http.client
import io
import json

import pytest

from core.web import ops


class _ResponseSocket:
    def __init__(self, stream):
        self.stream = stream

    def makefile(self, mode):
        assert mode == "rb"
        return self.stream


class _ResponseStream(io.BytesIO):
    def __init__(self, data, on_read=None):
        super().__init__(data)
        self.on_read = on_read

    def read(self, size=-1):
        data = super().read(size)
        if self.on_read is not None:
            self.on_read()
        return data


def _http_response(monkeypatch, body, content_length=None, on_read=None):
    headers = b"HTTP/1.1 200 OK\r\nConnection: close\r\n"
    if content_length is not None:
        headers += f"Content-Length: {content_length}\r\n".encode("ascii")
    stream = _ResponseStream(headers + b"\r\n" + body, on_read=on_read)
    response = http.client.HTTPResponse(_ResponseSocket(stream))
    response.begin()
    monkeypatch.setattr(ops.urllib.request, "urlopen", lambda request, timeout: response)
    return response


@pytest.mark.parametrize("decompress", [False, True])
def test_complete_download_publishes_content_and_metadata(monkeypatch, tmp_path, decompress):
    payload = b"disk-image-content"
    body = gzip.compress(payload) if decompress else payload
    response = _http_response(monkeypatch, body, content_length=len(body))
    dest = tmp_path / "image.raw"
    dest.write_bytes(b"previous-image")
    meta = {"version": "24.04"}
    registry = {}

    ops.OpsMixin._bg_download("https://example.test/image", str(dest), dest.name,
                             registry, set(), decompress=decompress, meta=meta)

    assert registry[dest.name]["status"] == "done"
    assert registry[dest.name]["done"] == registry[dest.name]["total"] == len(body)
    assert dest.read_bytes() == payload
    assert json.loads((tmp_path / "image.raw.meta.json").read_text(encoding="utf-8")) == meta
    assert not (tmp_path / "image.raw.part").exists()
    assert response.closed


@pytest.mark.parametrize("decompress", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_short_response_is_not_published(monkeypatch, tmp_path, decompress, existing):
    # 完整的 gzip 也必须匹配传输层长度，不能以「还能解压」代替下载完整性检查。
    body = gzip.compress(b"replacement-image") if decompress else b"short"
    expected = len(body) + 5
    response = _http_response(monkeypatch, body, content_length=expected)
    dest = tmp_path / "image.raw"
    sidecar = tmp_path / "image.raw.meta.json"
    if existing:
        dest.write_bytes(b"previous-image")
        sidecar.write_text('{"version": "previous"}', encoding="utf-8")
    registry = {}

    ops.OpsMixin._bg_download("https://example.test/image", str(dest), dest.name,
                             registry, set(), decompress=decompress, meta={"version": "new"})

    assert registry[dest.name]["status"] == "error"
    assert f"预期 {expected} 字节，实际收到 {len(body)} 字节" in registry[dest.name]["error"]
    if existing:
        assert dest.read_bytes() == b"previous-image"
        assert json.loads(sidecar.read_text(encoding="utf-8")) == {"version": "previous"}
    else:
        assert not dest.exists()
        assert not sidecar.exists()
    assert not (tmp_path / "image.raw.part").exists()
    assert response.closed


@pytest.mark.parametrize("body, content_length", [(b"unknown-length", None), (b"", None), (b"", 0)])
def test_response_without_length_and_empty_response(monkeypatch, tmp_path, body, content_length):
    response = _http_response(monkeypatch, body, content_length=content_length)
    dest = tmp_path / "image.raw"
    registry = {}

    ops.OpsMixin._bg_download("https://example.test/image", str(dest), dest.name, registry, set())

    assert registry[dest.name]["status"] == "done"
    assert registry[dest.name]["done"] == len(body)
    assert registry[dest.name]["total"] == 0
    assert dest.read_bytes() == body
    assert not (tmp_path / "image.raw.part").exists()
    assert response.closed


@pytest.mark.parametrize("cancel_during_read", [False, True])
def test_cancel_cleans_partial_file_without_replacing_target(monkeypatch, tmp_path, cancel_during_read):
    dest = tmp_path / "image.raw"
    dest.write_bytes(b"previous-image")
    cancel_set = set() if cancel_during_read else {dest.name}
    body = b"x" * (1024 * 1024 + 5)
    response = _http_response(monkeypatch, body, content_length=len(body),
                              on_read=lambda: cancel_set.add(dest.name))
    registry = {}

    ops.OpsMixin._bg_download("https://example.test/image", str(dest), dest.name,
                             registry, cancel_set, meta={"version": "new"})

    assert registry[dest.name]["status"] == "cancelled"
    assert dest.read_bytes() == b"previous-image"
    assert not (tmp_path / "image.raw.meta.json").exists()
    assert not (tmp_path / "image.raw.part").exists()
    assert dest.name not in cancel_set
    assert response.closed
