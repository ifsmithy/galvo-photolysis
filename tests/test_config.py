from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from galvo.config import DEFAULT_CONFIG_PATH, ConfigError, RigConfig, load_config, parse_config


def test_real_rig_config_is_valid() -> None:
    # config/rig.toml is the real rig; it must always load and pass validation.
    real = load_config(DEFAULT_CONFIG_PATH)
    assert real.lasers


def test_test_config_loads(config: RigConfig) -> None:
    assert config.daq.ao_channels == ("ao0", "ao1")
    assert (config.daq.ao_min_v, config.daq.ao_max_v) == (-10.0, 10.0)
    assert config.daq.sample_rate_hz == 5000
    assert [laser.do_line for laser in config.lasers] == [0, 1]
    assert config.sync.trigger_out_line is None
    assert config.sync.trigger_in_line is None
    assert len(config.source_sha256) == 64


def test_original_axis_convention_is_the_default(config: RigConfig) -> None:
    # The original sent (ao0, ao1) = (y, -x).
    gx, gy = 3.0, 5.0
    assert np.allclose(config.axes.as_array() @ [gx, gy], [gy, -gx])


def test_unverified_items_are_reported(config: RigConfig) -> None:
    items = config.unverified()
    assert "axis convention" in items
    assert "settling time" in items
    assert "laser 'Laser 1' line" in items
    assert "daq" not in items


def test_laser_lookup(config: RigConfig) -> None:
    assert config.laser("Laser 2").do_line == 1
    with pytest.raises(KeyError, match="Laser 1"):
        config.laser("Laser 9")


def test_env_var_selects_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: RigConfig
) -> None:
    test_config_path = Path(__file__).parent / "data" / "rig.toml"
    copy = tmp_path / "rig.toml"
    copy.write_bytes(test_config_path.read_bytes().replace(b"0.001 ", b"0.0004 ", 1))
    monkeypatch.setenv("GALVO_RIG_CONFIG", str(copy))
    assert load_config().galvo.settling_time_s == 0.0004
    assert load_config().source_sha256 != config.source_sha256


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        ("axes", "matrix", [[1.0, 2.0], [2.0, 4.0]], "singular"),
        ("daq", "ao_min_v", 20.0, "less than"),
        ("galvo", "settling_time_s", -1.0, "negative"),
        ("sync", "trigger_out_line", 1, "only one"),
        ("sync", "trigger_out_line", 8, "outside"),
        ("daq", "do_line_count", True, "must be int"),
        ("daq", "sample_rate_hz", "fast", "number"),
    ],
)
def test_bad_values_rejected(
    raw_config: dict[str, Any], section: str, key: str, value: object, message: str
) -> None:
    raw_config[section][key] = value
    with pytest.raises(ConfigError, match=message):
        parse_config(raw_config)


def test_bad_laser_rejected(raw_config: dict[str, Any]) -> None:
    raw_config["lasers"][0]["role"] = "zap"
    with pytest.raises(ConfigError, match="role"):
        parse_config(raw_config)
    raw_config["lasers"][0]["role"] = "aim"
    raw_config["lasers"][1]["do_line"] = 0
    with pytest.raises(ConfigError, match="only one"):
        parse_config(raw_config)


def test_missing_section_rejected(raw_config: dict[str, Any]) -> None:
    del raw_config["galvo"]
    with pytest.raises(ConfigError, match=r"\[galvo\]"):
        parse_config(raw_config)
