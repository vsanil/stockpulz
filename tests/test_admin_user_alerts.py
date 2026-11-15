"""The /admin user drawer must show the user's real alerts.

It imported `PriceAlertManager` — a class that has never existed — and
swallowed the ImportError, so every user's Alerts tab read "No active alerts"
no matter what was stored (found 2026-11-15, while checking whether the
overnight crypto-only alert run had fired the right alerts).
"""
from tests.conftest import TEST_CHAT_ID


def _admin(client):
    with client.session_transaction() as s:
        s["admin"] = True


class TestUserDrawerAlerts:
    def test_stored_alerts_are_listed_with_the_price_the_drawer_reads(self, client, _gist_store):
        _gist_store.write("price_alerts.json", {
            TEST_CHAT_ID: [
                {"ticker": "BTC", "direction": "below", "target": 60000.0, "auto": True},
                {"ticker": "AAPL", "direction": "above", "target": 200.0},
            ],
            f"_history_{TEST_CHAT_ID}": [{"ticker": "ETH", "triggered_price": 1.0}],
        })
        _admin(client)
        r = client.get(f"/admin/user/{TEST_CHAT_ID}")
        assert r.status_code == 200
        alerts = r.get_json()["alerts"]
        assert [(a["ticker"], a["price"]) for a in alerts] == [("BTC", 60000.0), ("AAPL", 200.0)]

    def test_a_user_without_alerts_gets_an_empty_list(self, client, _gist_store):
        _gist_store.write("price_alerts.json", {})
        _admin(client)
        assert client.get(f"/admin/user/{TEST_CHAT_ID}").get_json()["alerts"] == []
