# CLAUDE.md

Control software for Ian F Smith's UV spot-photolysis rig (galvo beam steering, NI USB-6001).

## Attribution: get this right, always

Ian cites this repository in job applications and will not overstate what is Ian's own work.

- Ian **specified and built the original hardware** at UC Irvine.
- The **original software** was written by **Brett Settle**, an undergraduate working for
  Ian: https://github.com/BrettJSettle/GalvoController. Credit Brett by name.
- **This codebase is a new implementation by Ian.** It is not a port.
- Brett's `setup.py` declares `license='MIT'` and `author='Brett Settle'`. Don't claim
  the original had no license, and don't make ownership claims about it in public docs.
- When reusing one of Brett's approaches, say so in a comment ("Kept from the original: ...").

## The reference code

`reference/` is a clone of Brett's repo: **read-only** specification material, gitignored.
Never edit it, import from it, or copy code from it wholesale.

## Platforms

- Development: Ian's Apple Silicon Mac. The app and full test suite **must** run natively
  there against the simulator.
- Shipping: a **Windows-only** installer (Phase 4). No Mac installer.
- Hardware: NI-DAQmx is Windows/Linux x86 only. `NIDAQmxBackend` is imported lazily;
  hardware tests skip cleanly when no DAQ is present.

## Architecture rules

```
src/galvo/
  config.py       rig.toml loader. The ONLY place rig facts enter the code
  geometry.py     Affine2D transforms (pure numpy)
  patterns.py     Point / Line / Region targets -> ordered beam positions
  calibration.py  image pixels -> logical galvo volts, fitted from clicked points
  hw/             DaqBackend interface (channel volts + line masks only)
                  SimulatedBackend (default), NIDAQmxBackend (Phase 3)
  sequencer.py    plan_exposure (pure) -> execute (thin); safety interlocks
  explog.py       append-only JSONL exposure log
  session.py      one session at the rig: runs exposures on a worker thread (no Qt)
  simfield.py     simulated "camera": where the simulated beam lands in the image
  demo.py         headless end-to-end demo
  gui/            PySide6 window. The ONLY package allowed to import Qt
```

- Dependencies point one way: `gui -> session -> sequencer -> {patterns, calibration, geometry, hw, explog} -> config`.
  `tests/test_architecture.py` enforces this.
- Coordinate chain: **image px** (calibration) -> **logical galvo volts** (`[axes] matrix`)
  -> **channel volts (ao0, ao1)**. Each arrow is one named transform. Never rotate or flip
  axes anywhere else.
- Rig facts (axis matrix, settling time, laser lines, sync wiring) live **only** in
  `config/rig.toml`, with a `verified` flag. Keep `HARDWARE.md` in step with it.
- Out-of-range voltages are **rejected**, never silently clamped.
- Gates are closed whenever the mirrors jump, and always closed on exit or error.
- No silent fallback to the simulator. Every log record names its backend.

## Conventions

Python 3.12, type hints throughout, `uv`, `ruff`, `mypy --strict`, `pytest`.

```bash
uv sync --extra demo
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

## Working with Ian

PhD cell biologist (fluorescence microscopy, calcium signalling). Directs and reviews
rather than writing code. Keep explanations short and concrete, in microscopy terms. Say
plainly when something Ian asks for is a bad idea. Ask few questions, only ones that change
the design.
