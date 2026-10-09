from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

import pytest

from galvo.calibration import Calibration, default_calibration
from galvo.config import RigConfig, load_config, parse_config
from galvo.hw import FakeClock, SimulatedBackend

# Tests use a frozen copy of the config, not the real rig's config/rig.toml.
TEST_CONFIG_PATH = Path(__file__).parent / "data" / "rig.toml"


@pytest.fixture
def raw_config() -> dict[str, Any]:
    """The test rig config as a dict, for tests that tweak one value."""
    return copy.deepcopy(tomllib.loads(TEST_CONFIG_PATH.read_text(encoding="utf-8")))


@pytest.fixture
def config() -> RigConfig:
    return load_config(TEST_CONFIG_PATH)


@pytest.fixture
def synced_config(raw_config: dict[str, Any]) -> RigConfig:
    raw_config["sync"].update(trigger_out_line=2, trigger_in_line="port2/line0")
    return parse_config(raw_config)


@pytest.fixture
def calibration(config: RigConfig) -> Calibration:
    return default_calibration(512, 512, config.daq.ao_min_v, config.daq.ao_max_v)


@pytest.fixture
def backend(config: RigConfig) -> SimulatedBackend:
    return SimulatedBackend(config, FakeClock())
