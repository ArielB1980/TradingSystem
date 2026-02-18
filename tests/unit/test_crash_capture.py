"""Tests for src.runtime.crash_capture."""

import asyncio
import os
import signal
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from src.runtime.crash_capture import (
    _CRASH_LOG,
    _FAULT_LOG,
    enable_faulthandler,
    install_asyncio_exception_handler,
    register_sigusr2_dump,
    setup_all,
    write_crash_log,
)


@pytest.fixture(autouse=True)
def _patch_logs_dir(tmp_path, monkeypatch):
    """Redirect logs/ to a temp directory for all tests."""
    monkeypatch.setattr("src.runtime.crash_capture._LOGS_DIR", tmp_path)
    monkeypatch.setattr("src.runtime.crash_capture._FAULT_LOG", tmp_path / "fault.log")
    monkeypatch.setattr("src.runtime.crash_capture._CRASH_LOG", tmp_path / "crash.log")
    return tmp_path


class TestEnableFaulthandler:
    def test_enables_without_error(self, _patch_logs_dir):
        enable_faulthandler()

    def test_creates_logs_dir(self, _patch_logs_dir):
        import shutil
        shutil.rmtree(_patch_logs_dir, ignore_errors=True)
        enable_faulthandler()
        assert _patch_logs_dir.exists()


class TestWriteCrashLog:
    def test_writes_crash_entry(self, _patch_logs_dir):
        try:
            raise ValueError("test boom")
        except ValueError as exc:
            write_crash_log(exc, context="unit_test", cycle_id="cycle_42")

        crash_log = _patch_logs_dir / "crash.log"
        assert crash_log.exists()
        content = crash_log.read_text()
        assert "ValueError" in content
        assert "test boom" in content
        assert "cycle_id=cycle_42" in content
        assert "context=unit_test" in content

    def test_multiple_crashes_appended(self, _patch_logs_dir):
        for i in range(3):
            try:
                raise RuntimeError(f"crash {i}")
            except RuntimeError as exc:
                write_crash_log(exc, context=f"test_{i}")

        content = (_patch_logs_dir / "crash.log").read_text()
        assert content.count("CRASH at") == 3

    def test_truncates_long_messages(self, _patch_logs_dir):
        """write_crash_log should not fail on very long exception messages."""
        try:
            raise ValueError("x" * 10000)
        except ValueError as exc:
            write_crash_log(exc, context="long_msg")

        assert (_patch_logs_dir / "crash.log").exists()

    def test_never_raises(self, _patch_logs_dir, monkeypatch):
        """write_crash_log must never raise, even if file I/O fails."""
        monkeypatch.setattr("src.runtime.crash_capture._CRASH_LOG", Path("/nonexistent/dir/crash.log"))
        monkeypatch.setattr("src.runtime.crash_capture._LOGS_DIR", Path("/nonexistent/dir"))
        try:
            raise ValueError("boom")
        except ValueError as exc:
            write_crash_log(exc, context="should_not_raise")


class TestSIGUSR2:
    def test_registers_handler(self, _patch_logs_dir):
        register_sigusr2_dump()
        handler = signal.getsignal(signal.SIGUSR2)
        assert callable(handler)

    def test_sigusr2_writes_fault_log(self, _patch_logs_dir):
        register_sigusr2_dump()
        os.kill(os.getpid(), signal.SIGUSR2)
        fault_log = _patch_logs_dir / "fault.log"
        assert fault_log.exists()
        content = fault_log.read_text()
        assert "SIGUSR2 traceback dump" in content


class TestAsyncioExceptionHandler:
    def test_installs_handler(self, _patch_logs_dir):
        loop = asyncio.new_event_loop()
        try:
            install_asyncio_exception_handler(loop)
            handler = loop.get_exception_handler()
            assert handler is not None
        finally:
            loop.close()

    def test_handler_writes_crash_log(self, _patch_logs_dir):
        loop = asyncio.new_event_loop()
        try:
            install_asyncio_exception_handler(loop)

            exc = RuntimeError("async boom")
            try:
                raise exc
            except RuntimeError:
                pass

            handler = loop.get_exception_handler()
            handler(loop, {"message": "test", "exception": exc})

            content = (_patch_logs_dir / "crash.log").read_text()
            assert "async boom" in content
            assert "asyncio_task" in content
        finally:
            loop.close()


class TestSetupAll:
    def test_setup_all_runs(self, _patch_logs_dir):
        setup_all()
