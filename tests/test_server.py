from fastapi.testclient import TestClient

from screener import explorer, screen, server


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(explorer, "LIVE", tmp_path / "explorer.html")
    monkeypatch.setattr(screen, "LATEST", tmp_path / "latest_xs.pkl")
    monkeypatch.setattr(server, "WEB", tmp_path / "web")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "index.html").write_text("<p>app</p>")
    return TestClient(server.create_app())


def test_explorer_before_first_refresh(tmp_path, monkeypatch):
    r = _client(tmp_path, monkeypatch).get("/explorer.html")
    assert r.status_code == 200 and "first refresh" in r.text


def test_explorer_serves_the_refresh_copy(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    (tmp_path / "explorer.html").write_text("<h1>Backtest explorer</h1>")
    assert c.get("/explorer.html").text == "<h1>Backtest explorer</h1>"
