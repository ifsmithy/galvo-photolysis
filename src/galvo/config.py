"""Rig configuration: the single place where facts about the physical rig enter the code.

Values come from ``config/rig.toml``. Every section carries a ``verified`` flag; anything
not yet measured on the real rig is ``verified = false`` and is reported by
:meth:`RigConfig.unverified`, so the GUI and the exposure log can say so.
"""

from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import numpy.typing as npt

LaserRole = Literal["aim", "uncage", "unknown"]
_ROLES: tuple[LaserRole, ...] = ("aim", "uncage", "unknown")

ENV_VAR = "GALVO_RIG_CONFIG"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "rig.toml"


class ConfigError(ValueError):
    """rig.toml is missing a value or contains an impossible one."""


@dataclass(frozen=True)
class DaqConfig:
    device: str
    ao_channels: tuple[str, str]
    ao_min_v: float
    ao_max_v: float
    sample_rate_hz: float
    do_port: str
    do_line_count: int
    verified: bool


@dataclass(frozen=True)
class AxisConfig:
    """Linear map from logical galvo volts (gx, gy) to channel volts (ao0, ao1)."""

    matrix: tuple[tuple[float, float], tuple[float, float]]
    verified: bool

    def as_array(self) -> npt.NDArray[np.float64]:
        return np.array(self.matrix, dtype=np.float64)


@dataclass(frozen=True)
class GalvoConfig:
    settling_time_s: float
    verified: bool


@dataclass(frozen=True)
class LaserConfig:
    name: str
    role: LaserRole
    wavelength_nm: float | None
    do_line: int
    verified: bool


@dataclass(frozen=True)
class SyncConfig:
    trigger_out_line: int | None
    trigger_out_pulse_s: float
    trigger_in_line: str | None
    verified: bool


@dataclass(frozen=True)
class RigConfig:
    daq: DaqConfig
    axes: AxisConfig
    galvo: GalvoConfig
    lasers: tuple[LaserConfig, ...]
    sync: SyncConfig
    source_sha256: str

    def laser(self, name: str) -> LaserConfig:
        for laser in self.lasers:
            if laser.name == name:
                return laser
        known = ", ".join(repr(laser.name) for laser in self.lasers)
        raise KeyError(f"No laser named {name!r}; configured lasers are {known}")

    def unverified(self) -> list[str]:
        """Human-readable list of rig facts not yet measured on the real rig."""
        items = [
            label
            for label, ok in (
                ("daq", self.daq.verified),
                ("axis convention", self.axes.verified),
                ("settling time", self.galvo.verified),
                ("sync wiring", self.sync.verified),
            )
            if not ok
        ]
        items += [f"laser {laser.name!r} line" for laser in self.lasers if not laser.verified]
        return items


def default_config_path() -> Path:
    return Path(os.environ.get(ENV_VAR, DEFAULT_CONFIG_PATH))


def load_config(path: Path | str | None = None) -> RigConfig:
    path = Path(path) if path is not None else default_config_path()
    raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return parse_config(data, source_sha256=hashlib.sha256(raw).hexdigest())


def parse_config(data: dict[str, Any], source_sha256: str = "") -> RigConfig:
    daq_t = _table(data, "daq")
    channels = _get(daq_t, "ao_channels", list, "daq")
    if len(channels) != 2:
        raise ConfigError("daq.ao_channels must list exactly two channels")
    daq = DaqConfig(
        device=_get(daq_t, "device", str, "daq"),
        ao_channels=(str(channels[0]), str(channels[1])),
        ao_min_v=_num(daq_t, "ao_min_v", "daq"),
        ao_max_v=_num(daq_t, "ao_max_v", "daq"),
        sample_rate_hz=_num(daq_t, "sample_rate_hz", "daq"),
        do_port=_get(daq_t, "do_port", str, "daq"),
        do_line_count=_get(daq_t, "do_line_count", int, "daq"),
        verified=_get(daq_t, "verified", bool, "daq"),
    )

    axes_t = _table(data, "axes")
    m = np.array(_get(axes_t, "matrix", list, "axes"), dtype=np.float64)
    if m.shape != (2, 2):
        raise ConfigError("axes.matrix must be 2x2")
    axes = AxisConfig(
        matrix=((float(m[0, 0]), float(m[0, 1])), (float(m[1, 0]), float(m[1, 1]))),
        verified=_get(axes_t, "verified", bool, "axes"),
    )

    galvo_t = _table(data, "galvo")
    galvo = GalvoConfig(
        settling_time_s=_num(galvo_t, "settling_time_s", "galvo"),
        verified=_get(galvo_t, "verified", bool, "galvo"),
    )

    lasers_raw = data.get("lasers")
    if not isinstance(lasers_raw, list) or not lasers_raw:
        raise ConfigError("rig.toml needs at least one [[lasers]] entry")
    lasers = tuple(_parse_laser(t) for t in lasers_raw)

    sync_t = _table(data, "sync")
    out_line = _get(sync_t, "trigger_out_line", int, "sync")
    in_line = _get(sync_t, "trigger_in_line", str, "sync")
    sync = SyncConfig(
        trigger_out_line=None if out_line < 0 else out_line,
        trigger_out_pulse_s=_num(sync_t, "trigger_out_pulse_s", "sync"),
        trigger_in_line=in_line or None,
        verified=_get(sync_t, "verified", bool, "sync"),
    )

    config = RigConfig(daq, axes, galvo, lasers, sync, source_sha256)
    _validate(config)
    return config


def _parse_laser(t: Any) -> LaserConfig:
    if not isinstance(t, dict):
        raise ConfigError("each [[lasers]] entry must be a table")
    role = _get(t, "role", str, "lasers")
    if role not in _ROLES:
        raise ConfigError(f"lasers.role must be one of {_ROLES}, got {role!r}")
    wavelength = _num(t, "wavelength_nm", "lasers")
    return LaserConfig(
        name=_get(t, "name", str, "lasers"),
        role=role,
        wavelength_nm=wavelength or None,
        do_line=_get(t, "do_line", int, "lasers"),
        verified=_get(t, "verified", bool, "lasers"),
    )


def _validate(c: RigConfig) -> None:
    if not c.daq.ao_min_v < c.daq.ao_max_v:
        raise ConfigError("daq.ao_min_v must be less than daq.ao_max_v")
    if c.daq.sample_rate_hz <= 0:
        raise ConfigError("daq.sample_rate_hz must be positive")
    if abs(np.linalg.det(c.axes.as_array())) < 1e-9:
        raise ConfigError("axes.matrix is singular: the two mirrors would not span the field")
    if c.galvo.settling_time_s < 0:
        raise ConfigError("galvo.settling_time_s cannot be negative")

    names = [laser.name for laser in c.lasers]
    if len(set(names)) != len(names):
        raise ConfigError("laser names must be unique")
    used = [laser.do_line for laser in c.lasers]
    if c.sync.trigger_out_line is not None:
        used.append(c.sync.trigger_out_line)
    for line in used:
        if not 0 <= line < c.daq.do_line_count:
            raise ConfigError(f"digital line {line} is outside 0..{c.daq.do_line_count - 1}")
    if len(set(used)) != len(used):
        raise ConfigError("each digital line may drive only one laser or trigger")


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    t = data.get(key)
    if not isinstance(t, dict):
        raise ConfigError(f"rig.toml is missing the [{key}] section")
    return t


def _get[T](t: dict[str, Any], key: str, kind: type[T], section: str) -> T:
    if key not in t:
        raise ConfigError(f"{section}.{key} is missing")
    value = t[key]
    # bool is a subclass of int in Python; don't let `true` pass as a line number.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ConfigError(f"{section}.{key} must be {kind.__name__}, got {value!r}")
    return value


def _num(t: dict[str, Any], key: str, section: str) -> float:
    if key not in t:
        raise ConfigError(f"{section}.{key} is missing")
    value = t[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{section}.{key} must be a number, got {value!r}")
    return float(value)
