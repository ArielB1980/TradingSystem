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
