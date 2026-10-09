from __future__ import annotations

import ast
from pathlib import Path

import pytest

from galvo.explog import ExposureLog, read_log

SRC = Path(__file__).resolve().parents[1] / "src" / "galvo"


def test_log_appends_one_line_per_record(tmp_path: Path) -> None:
    log = ExposureLog(tmp_path / "sub" / "log.jsonl")
    log.append({"a": 1})
    log.append({"b": 2})
    assert log.read() == [{"a": 1}, {"b": 2}]
    assert len(log.path.read_text(encoding="utf-8").splitlines()) == 2
    assert read_log(tmp_path / "missing.jsonl") == []


def test_log_refuses_nan(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ExposureLog(tmp_path / "log.jsonl").append({"x": float("nan")})


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


# module -> galvo modules it must never import (dependencies point one way)
FORBIDDEN = {
    "config.py": {
        "galvo.geometry",
        "galvo.patterns",
        "galvo.calibration",
        "galvo.hw",
        "galvo.sequencer",
        "galvo.explog",
    },
    "geometry.py": {"galvo.hw", "galvo.sequencer", "galvo.calibration", "galvo.patterns"},
    "patterns.py": {"galvo.hw", "galvo.sequencer", "galvo.calibration"},
    "calibration.py": {"galvo.hw", "galvo.sequencer"},
    "explog.py": {"galvo.hw", "galvo.sequencer"},
    "hw": {"galvo.patterns", "galvo.calibration", "galvo.sequencer", "galvo.explog"},
}


@pytest.mark.parametrize("path", sorted(SRC.rglob("*.py")), ids=lambda p: str(p.relative_to(SRC)))
def test_dependency_rules(path: Path) -> None:
    rel = path.relative_to(SRC)
    imports = _imports(path)
    if rel.parts[0] != "gui":
        qt = {i for i in imports if i.split(".")[0] in {"PySide6", "PyQt5", "PyQt6", "qtpy"}}
        assert not qt, f"only galvo.gui may import Qt; {rel} imports {qt}"
    banned = FORBIDDEN.get(rel.parts[0], set())
    hits = {i for i in imports if any(i == b or i.startswith(b + ".") for b in banned)}
    assert not hits, f"{rel} must not import {hits}"
    assert not any(i.startswith("nidaqmx") for i in imports) or rel.name == "nidaqmx_backend.py"


def test_demo_runs_end_to_end(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    from galvo.demo import run

    results = run(tmp_path)
    assert [r.status for r in results] == ["completed"] * 4
    assert (tmp_path / "beam_path.png").stat().st_size > 10_000
    assert len(read_log(tmp_path / "exposures.jsonl")) == 4
