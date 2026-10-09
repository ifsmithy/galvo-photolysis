"""Headless end-to-end demo on the simulated rig.

Calibrates against a made-up "true" rig, runs a point, a line and a region, writes an
exposure log, and draws where the simulated beam went. Needs the ``demo`` extra
(matplotlib).

    uv run galvo-demo [--out demo_output]
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np

from galvo.calibration import Calibration, calibrate
from galvo.config import RigConfig, load_config
from galvo.explog import ExposureLog
from galvo.geometry import Affine2D, Points
from galvo.hw import FakeClock, SimulatedBackend
from galvo.patterns import LineTarget, PointTarget, RegionTarget
from galvo.sequencer import ExposureRequest, ExposureResult, execute, plan_exposure

VIEW = (512, 512)


def simulated_calibration(rng: np.random.Generator) -> Calibration:
    """Pretend to aim the guide laser at 4 positions and click the spots, slightly off."""
    angle = math.radians(3.0)  # camera slightly rotated relative to the mirrors
    scale = 16.0 / VIEW[0]  # the view covers 16 V of mirror travel
    rot = scale * np.array(
        [[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]]
    )
    true_px_to_galvo = Affine2D.from_linear(rot, (-8.0, -8.0))

    galvo_points = np.array([[-6.0, -6.0], [6.0, -6.0], [6.0, 6.0], [-6.0, 6.0]])
    clicks = true_px_to_galvo.inverse().apply(galvo_points) + rng.normal(0, 0.7, (4, 2))
    return calibrate(clicks, galvo_points, view_rect=(0, 0, *VIEW))


def demo_requests(config: RigConfig) -> list[ExposureRequest]:
    aim, uncage = config.lasers[0].name, config.lasers[-1].name
    u_shape = (
        (300, 300),
        (460, 300),
        (460, 460),
        (420, 460),
        (420, 340),
        (340, 340),
        (340, 460),
        (300, 460),
    )
    return [
        ExposureRequest(PointTarget((120, 110)), aim, dwell_s=0.05, note="aim check"),
        ExposureRequest(PointTarget((120, 110)), uncage, dwell_s=0.1, note="point uncage"),
        ExposureRequest(
            LineTarget((80, 300), (240, 420), 20), uncage, dwell_s=0.002, note="line across process"
        ),
        ExposureRequest(
            RegionTarget(u_shape, spacing=10), uncage, dwell_s=0.001, note="concave region"
        ),
    ]


def run(out_dir: Path) -> list[ExposureResult]:
    config = load_config()
    calibration = simulated_calibration(np.random.default_rng(0))
    out_dir.mkdir(parents=True, exist_ok=True)
    calibration.save(out_dir / "calibration.json")
    log_path = out_dir / "exposures.jsonl"
    log_path.unlink(missing_ok=True)
    log = ExposureLog(log_path)

    backend = SimulatedBackend(config, FakeClock())
    with backend:
        results = [
            execute(plan_exposure(req, config, calibration), backend, log)
            for req in demo_requests(config)
        ]
    render(backend, config, calibration, results, out_dir / "beam_path.png")
    return results


def render(
    backend: SimulatedBackend,
    config: RigConfig,
    calibration: Calibration,
    results: list[ExposureResult],
    path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t_cmd, _ = backend.commands()
    times = np.arange(0.0, t_cmd[-1] + 0.01, 0.5 / config.daq.sample_rate_hz)
    px = _ao_to_px(backend.mirror_position(times), config, calibration)
    lines = backend.lines_at(times)

    fig, ax = plt.subplots(figsize=(7, 7))
    ax.plot(px[:, 0], px[:, 1], color="0.6", lw=0.6, label="mirror path (gates closed)")
    colours = ["tab:cyan", "tab:purple", "tab:orange", "tab:green"]
    for i, laser in enumerate(config.lasers):
        on = (lines >> laser.do_line) & 1 == 1
        # Earlier lasers drawn bigger, so an aim shot stays visible under an uncage shot.
        size = 3 + 12 * (len(config.lasers) - 1 - i)
        ax.scatter(px[on, 0], px[on, 1], s=size, color=colours[i % 4], label=f"{laser.name} on")
    for r in results:
        for seg in r.plan.segments_px:
            ax.scatter(seg[:, 0], seg[:, 1], s=30, facecolors="none", edgecolors="k", lw=0.4)
    ax.scatter(
        calibration.pixel_points[:, 0],
        calibration.pixel_points[:, 1],
        marker="+",
        s=80,
        color="red",
        label=f"calibration clicks (rms {calibration.rms_px:.2f} px)",
    )

    ax.set(
        xlim=(0, VIEW[0]),
        ylim=(VIEW[1], 0),
        aspect="equal",
        xlabel="image x (px)",
        ylabel="image y (px)",
    )
    unverified = ", ".join(config.unverified())
    ax.set_title(f"Simulated rig: where the beam went\nunverified: {unverified}", fontsize=9)
    ax.legend(loc="upper center", fontsize=7, markerscale=3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _ao_to_px(ao: Points, config: RigConfig, calibration: Calibration) -> Points:
    galvo = ao @ np.linalg.inv(config.axes.as_array()).T
    return calibration.px_to_galvo.inverse().apply(galvo)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=Path("demo_output"))
    args = parser.parse_args()
    results = run(args.out)
    for r in results:
        rec = r.to_record()
        print(
            f"{rec['status']:9} {rec['laser']['name']:8} {rec['pattern']['type']:6} "
            f"{rec['n_points']:4} spots  gate open {len(rec['gate_open_intervals_s'])}x  "
            f"{rec['duration_s'] * 1000:7.1f} ms  ({rec['note']})"
        )
    print(f"\nLog:     {args.out / 'exposures.jsonl'}\nPicture: {args.out / 'beam_path.png'}")


if __name__ == "__main__":
    main()
