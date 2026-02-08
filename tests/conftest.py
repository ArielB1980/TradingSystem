"""
Pytest configuration and shared fixtures.
"""
import pytest
from unittest.mock import patch


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
    with patch("src.storage.repository.async_record_event"):
        yield
