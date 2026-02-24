from src.live.auction_runner import _split_reconcile_issues


def test_split_reconcile_issues_only_orphaned_non_blocking():
    blocking, non_blocking = _split_reconcile_issues(
        [("SOL/USD", "ORPHANED: Registry has position, exchange does not")]
    )
    assert blocking == []
    assert len(non_blocking) == 1


def test_split_reconcile_issues_blocks_non_orphaned():
    blocking, non_blocking = _split_reconcile_issues(
        [
            ("SOL/USD", "ORPHANED: Registry has position, exchange does not"),
            ("PF_ETHUSD", "PHANTOM: Exchange has position, registry does not"),
            ("PF_DOTUSD", "QTY_MISMATCH: Registry 1 vs Exchange 2"),
        ]
    )
    assert len(non_blocking) == 1
    assert len(blocking) == 2
