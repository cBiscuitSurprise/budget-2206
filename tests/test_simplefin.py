import json
from datetime import date

from budget.ofx import Transaction, load_dir
from budget.simplefin import merge, parse

NOON = 1763985600  # 2025-11-24T12:00Z


def _t(acct: str, fitid: str, posted: date, amount: float) -> Transaction:
    return Transaction(acct, fitid, posted, amount, "X", "DEBIT")


def _feed(tmp_path, txns, balance="100.00"):
    d = tmp_path / "simplefin"
    d.mkdir(parents=True, exist_ok=True)
    body = {"errors": [], "accounts": [{"id": "ACT-1", "name": "Checking", "balance": balance, "balance-date": NOON, "transactions": txns}]}
    (d / "2025-11-24.json").write_text(json.dumps(body))
    return d / "2025-11-24.json"


class TestParse:
    def test_maps_skips_pending_and_unmapped(self, tmp_path):
        path = _feed(tmp_path, [
            {"id": "a", "posted": NOON, "amount": "-12.50", "description": "CHECK 1042", "pending": False},
            {"id": "b", "posted": NOON, "amount": "-3.00", "description": "COFFEE", "pending": True},
        ])
        txns, bals = parse(path, {"ACT-1": "acct"})
        assert [(t.fitid, t.trntype, t.checknum, t.amount) for t in txns] == [("sf:a", "CHECK", "1042", -12.5)]
        assert bals[0].amount == 100.0 and bals[0].as_of == date(2025, 11, 24)
        assert parse(path, {}) == ([], [])


class TestMerge:
    def test_drops_rows_a_statement_covers(self):
        stmt = [_t("a", "F1", date(2025, 11, 10), -20.0)]
        feed = [_t("a", "sf:1", date(2025, 11, 11), -20.0), _t("a", "sf:2", date(2025, 11, 11), -20.0)]
        assert [t.fitid for t in merge(stmt, feed)] == ["sf:2"]

    def test_keeps_rows_after_statement_and_other_accounts(self):
        stmt = [_t("a", "F1", date(2025, 11, 10), -20.0)]
        feed = [_t("a", "sf:1", date(2025, 11, 20), -20.0), _t("b", "sf:2", date(2025, 11, 10), -20.0)]
        assert sorted(t.fitid for t in merge(stmt, feed)) == ["sf:1", "sf:2"]


class TestLoadDir:
    def test_dedupes_repeat_pulls(self, tmp_path):
        _feed(tmp_path, [{"id": "a", "posted": NOON, "amount": "-5", "description": "X"}])
        (tmp_path / "simplefin" / "2025-11-25.json").write_text((tmp_path / "simplefin" / "2025-11-24.json").read_text())
        txns, bals = load_dir(tmp_path, None, {"ACT-1": "acct"})
        assert [t.fitid for t in txns] == ["sf:a"] and bals["acct"].amount == 100.0
