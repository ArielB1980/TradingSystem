"""
Pytest configuration and shared fixtures.
"""
import os

# Set DATABASE_URL for unit tests (must be before any src imports).
# Use postgresql:// so secret_manager validation accepts it; get_db is mocked below so we never connect.
if "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = "postgresql://localhost/unit_test"

import pytest
from unittest.mock import MagicMock, patch, MagicMock


def pytest_addoption(parser):
    """Register custom CLI options."""
    parser.addoption(
        "--regenerate-golden",
        action="store_true",
        default=False,
        help="Regenerate golden fixture files for snapshot tests",
    )


def pytest_configure(config):
    """Register custom marks. Async tests require pytest-asyncio (see requirements.txt)."""
    config.addinivalue_line("markers", "asyncio: mark test as async (pytest-asyncio).")


@pytest.fixture(autouse=True)
def _mock_db_and_events_for_unit(request):
    """
    Auto-mock async_record_event in unit tests so they run without a real DB.
    Skip for integration tests (they use their own mocks).

    Note: SMCEngine and RiskManager use constructor-injected event recorders
    that default to no-op, so they no longer need patching here.
    """
    if "integration" in str(request.node.fspath):
        yield
        return

    def _make_mock_db():
        mock_db = MagicMock()
        mock_session = MagicMock()
        # session.query(...).filter(...).count() → 0 (count_trades_opened_since, etc.)
        mock_session.query.return_value.filter.return_value.count.return_value = 0
        # session.query(...).filter(...).all() → [] (load_recent_intent_hashes)
        mock_session.query.return_value.filter.return_value.all.return_value = []
        # session.query(...).filter(...).with_for_update().first() → None (save_position)
        mock_session.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = None
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=mock_session)
        cm.__exit__ = MagicMock(return_value=False)
        mock_db.get_session.return_value = cm
        mock_db.database_url = "postgresql://localhost/unit_test"
        return mock_db

    with patch("src.storage.repository.async_record_event"), patch(
        "src.storage.repository.get_db", side_effect=_make_mock_db
    ):
        yield
