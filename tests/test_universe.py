"""Index pages are scraped from Wikipedia, whose table headers change (footnote markers, renames)."""
import pandas as pd

from screener import universe as U


def test_footnoted_headers(monkeypatch):
    rows = "".join(f"<tr><td>T{i}</td><td>Co {i}</td><td>Technology</td><td>Software</td></tr>" for i in range(60))
    html = ("<table><tr><th>Ticker</th><th>Company</th><th>ICB Industry[2]</th><th>ICB Subsector[2]</th></tr>"
            f"{rows}</table>")
    monkeypatch.setattr(U.http, "get", lambda *a, **k: type("R", (), {"text": html})())
    m = U._index_members("NDX", "https://example.test")
    assert len(m) == 60 and m["industry"].eq("Software").all()
    assert m["sector"].iloc[0] == U._ICB_TO_GICS.get("Technology", "Technology")
    assert isinstance(m, pd.DataFrame)
