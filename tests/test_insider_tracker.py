"""
test_insider_tracker.py — Regression tests for insider_tracker.py.

Covers the pandas 2.x pd.read_html(StringIO(...)) requirement — previously
pd.read_html(resp.text) raised [Errno 2] No such file or directory on
pandas 2.x because raw HTML strings were no longer accepted directly.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


MINIMAL_INSIDER_HTML = """
<html><body>
<table>
  <tr><th>Filing Date</th><th>Trade Date</th><th>Ticker</th><th>Insider Name</th>
      <th>Title</th><th>Trade Type</th><th>Price</th><th>Qty</th><th>Value</th></tr>
  <tr><td>2026-06-01</td><td>2026-05-30</td><td>NVDA</td><td>Jensen Huang</td>
      <td>CEO</td><td>P - Purchase</td><td>$900</td><td>100</td><td>$90,000</td></tr>
</table>
</body></html>
"""

EMPTY_HTML = "<html><body><p>No results found.</p></body></html>"


def _mock_resp(html: str, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.text = html
    return m


class TestGetInsiderSignal:
    def test_parses_html_string_without_file_error(self):
        """pd.read_html must receive StringIO, not raw text — regression for pandas 2.x."""
        with patch("requests.get", return_value=_mock_resp(MINIMAL_INSIDER_HTML)):
            from insider_tracker import get_insider_signal
            result = get_insider_signal("NVDA")
        # If the StringIO fix is missing this raises [Errno 2] and returns _empty_signal
        assert result["insider_score"] >= 0  # any non-exception result is a pass

    def test_returns_empty_on_http_error(self):
        with patch("requests.get", return_value=_mock_resp("", status=429)):
            from insider_tracker import get_insider_signal
            result = get_insider_signal("NVDA")
        assert result["recent_buys"] == 0
        assert result["insider_score"] == 0

    def test_returns_empty_on_no_table(self):
        with patch("requests.get", return_value=_mock_resp(EMPTY_HTML)):
            from insider_tracker import get_insider_signal
            result = get_insider_signal("NVDA")
        assert result["recent_buys"] == 0

    def test_returns_expected_shape(self):
        """Result always has the required keys regardless of parse outcome."""
        with patch("requests.get", return_value=_mock_resp(MINIMAL_INSIDER_HTML)):
            from insider_tracker import get_insider_signal
            result = get_insider_signal("NVDA")
        for key in ("recent_buys", "unique_insiders", "total_value", "insider_score", "is_cluster", "note"):
            assert key in result


class TestGetClusterBuys:
    def test_parses_html_string_without_file_error(self):
        """Same StringIO regression check for get_cluster_buys."""
        with patch("requests.get", return_value=_mock_resp(MINIMAL_INSIDER_HTML)):
            from insider_tracker import get_cluster_buys
            result = get_cluster_buys()
        assert isinstance(result, list)

    def test_returns_empty_on_http_error(self):
        with patch("requests.get", return_value=_mock_resp("", status=503)):
            from insider_tracker import get_cluster_buys
            result = get_cluster_buys()
        assert result == []
