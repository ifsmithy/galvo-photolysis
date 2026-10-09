"""Tests against a real NI DAQ. Skipped unless nidaqmx and a device are present (Phase 3)."""

from __future__ import annotations

import pytest

nidaqmx = pytest.importorskip("nidaqmx")

pytestmark = pytest.mark.hardware


def _device_present() -> bool:
    try:
        return len(nidaqmx.system.System.local().devices) > 0
    except Exception:
        return False


@pytest.mark.skipif(not _device_present(), reason="no NI DAQ device connected")
def test_device_visible() -> None:
    names = [d.name for d in nidaqmx.system.System.local().devices]
    assert names
