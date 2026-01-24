"""
Pytest configuration and shared fixtures.
"""
import pytest
from unittest.mock import patch


def pytest_configure(config):
    """Register custom marks. Async tests require pytest-asyncio (see requirements.txt)."""
    config.addinivalue_line("markers", "asyncio: mark test as async (pytest-asyncio).")


@pytest.fixture(autouse=True)
def _mock_db_and_events_for_unit(request):
    """
    Auto-mock record_event and DB access in unit tests so they run without a real DB.
    Skip for integration tests (they use their own mocks).
    """
    if "integration" in str(request.node.fspath):
        yield
        return
    with patch("src.risk.risk_manager.record_event"), patch(
        "src.strategy.smc_engine.record_event"
    ), patch("src.storage.repository.async_record_event"):
        yield
