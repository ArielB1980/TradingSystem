import json
from datetime import datetime, timezone

from src.dashboard import static_dashboard as dashboard
from src.dashboard.static_dashboard import generate_html, parse_log_entries


def test_parse_log_entries_uses_cycle_summary_for_coin_count_fallback():
    entries = [
        {
            "raw": (
                "2026-02-06T07:14:09.813864Z [info] Coin processing status summary "
                "coins_processed_recently=26 coins_waiting_for_candles=0 "
                "coins_with_sufficient_candles=26 total_coins=26"
            ),
            "level": "info",
        }
    ]

    parsed = parse_log_entries(entries)

    assert parsed["coins"] == []
    assert parsed["coins_reviewed_count"] == 26
    assert parsed["coins_processed_recently"] == 26
    assert parsed["total_coins"] == 26


def test_generate_html_displays_coin_count_fallback_when_coin_rows_absent():
    html = generate_html(
        {
            "coins": [],
            "coins_reviewed_count": 26,
            "signals": [],
            "auction": {"opens_executed": 0, "signals_collected": 0},
            "errors": [],
        },
        positions=[],
    )

    assert "Top 50 of 26" in html
    assert "Reviewed 26 coins in recent cycle" in html
    assert "Coins Reviewed</div>" in html


def test_generate_market_discovery_html_shows_last_update_and_counts():
    report = {
        "generated_at": "2026-02-06T08:19:30+00:00",
        "config": {
            "allow_futures_only_pairs": False,
            "allow_futures_only_universe": False,
        },
        "totals": {
            "futures_markets": 302,
            "candidate_pairs": 243,
            "eligible_pairs": 28,
            "gap_count": 274,
        },
        "status_counts": {
            "eligible": 28,
            "rejected_by_filters": 215,
            "unmapped_no_spot": 59,
            "excluded_disallowed_base": 0,
        },
        "new_futures_summary": {"total": 302, "eligible": 28, "gaps": 274},
        "top_rejection_reasons": [{"reason": "OI $0 < $500,000", "count": 6}],
        "entries": [
            {
                "spot_symbol": "ETH/USD",
                "futures_symbol": "PF_ETHUSD",
                "status": "eligible",
                "reason": "Passed all discovery filters.",
                "is_new": True,
                "spot_market_available": True,
                "candidate_considered": True,
                "candidate_source": "spot_mapped",
            }
        ],
        "new_futures_gaps": [],
    }

    html = dashboard.generate_market_discovery_html(
        report,
        now=datetime(2026, 2, 6, 9, 19, 30, tzinfo=timezone.utc),
    )

    assert "Market Discovery Report" in html
    assert "Last discovery update (UTC)" in html
    assert "2026-02-06 08:19:30 UTC" in html
    assert "Cadence: once per day" in html
    assert "Futures Markets" in html
    assert ">302<" in html


def test_update_market_discovery_dashboard_updates_daily_or_when_report_changes(
    tmp_path, monkeypatch
):
    report_file = tmp_path / "discovery_gap_report.json"
    page_file = tmp_path / "market-discovery.html"
    meta_file = tmp_path / "market-discovery-meta.json"

    monkeypatch.setattr(dashboard, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "DISCOVERY_GAP_FILE", report_file)
    monkeypatch.setattr(dashboard, "DISCOVERY_REPORT_FILE", page_file)
    monkeypatch.setattr(dashboard, "DISCOVERY_META_FILE", meta_file)

    report_v1 = {
        "generated_at": "2026-02-06T08:00:00+00:00",
        "totals": {"futures_markets": 10, "candidate_pairs": 5, "eligible_pairs": 2, "gap_count": 8},
        "status_counts": {"eligible": 2, "rejected_by_filters": 5, "unmapped_no_spot": 3, "excluded_disallowed_base": 0},
        "entries": [],
        "new_futures_summary": {"total": 1, "eligible": 0, "gaps": 1},
    }
    report_file.write_text(json.dumps(report_v1))

    assert dashboard.update_market_discovery_dashboard(force=False) is True
    assert page_file.exists()
    assert dashboard.update_market_discovery_dashboard(force=False) is False

    report_v2 = dict(report_v1)
    report_v2["generated_at"] = "2026-02-06T09:00:00+00:00"
    report_file.write_text(json.dumps(report_v2))

    assert dashboard.update_market_discovery_dashboard(force=False) is True
