"""启动参数解析（_parse_cli / _resolve_tls_certs）的单元测试：端口 / --tls / 自定义证书。"""

from core.web import boot


def test_parse_port_only():
    assert boot._parse_cli(["9000"]) == (9000, None, None, False)


def test_parse_flags_do_not_consume_port():
    # 只有开关时不应误把开关当端口
    assert boot._parse_cli(["--debug", "--no-auth"]) == (None, None, None, False)


def test_parse_tls_sets_auto():
    port, cert, key, auto = boot._parse_cli(["8899", "--tls"])
    assert port == 8899
    assert cert is None and key is None
    assert auto is True


def test_parse_explicit_cert_and_key():
    port, cert, key, auto = boot._parse_cli(
        ["--tls-cert", "/x/cert.pem", "--tls-key", "/x/key.pem", "8899"])
    assert (port, cert, key) == (8899, "/x/cert.pem", "/x/key.pem")
    assert auto is False


def test_parse_flags_do_not_consume_port_either():
    port, _, _, auto = boot._parse_cli(["--tls", "9000"])
    assert port == 9000
    assert auto is True


def test_resolve_env_override(monkeypatch):
    monkeypatch.setenv("PVE_WEBTOOLS_TLS_CERT", "/env/c.pem")
    monkeypatch.setenv("PVE_WEBTOOLS_TLS_KEY", "/env/k.pem")
    assert boot._resolve_tls_certs() == ("/env/c.pem", "/env/k.pem")


def test_resolve_project_tls_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("PVE_WEBTOOLS_TLS_CERT", raising=False)
    monkeypatch.delenv("PVE_WEBTOOLS_TLS_KEY", raising=False)
    tls = tmp_path / "tls"
    tls.mkdir()
    (tls / "fullchain.pem").write_text("c", encoding="utf-8")
    (tls / "privkey.pem").write_text("k", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert boot._resolve_tls_certs() == (
        str(tls / "fullchain.pem"), str(tls / "privkey.pem"))


def test_resolve_falls_back_to_pve_certs(tmp_path, monkeypatch):
    monkeypatch.delenv("PVE_WEBTOOLS_TLS_CERT", raising=False)
    monkeypatch.delenv("PVE_WEBTOOLS_TLS_KEY", raising=False)
    monkeypatch.chdir(tmp_path)   # 空目录，找不到项目证书
    assert boot._resolve_tls_certs() == (boot._PVE_SSL_CERT, boot._PVE_SSL_KEY)
