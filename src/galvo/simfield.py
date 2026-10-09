"""Where the simulated beam lands in the image: the simulator's stand-in for the camera.

On the real rig the camera shows where the 488 spot actually is. In simulation, this
module plays that role with a made-up "true" mapping from image pixels to galvo volts:
slightly rotated, offset and not filling the full mirror range, as a real rig would be.
Before calibration the simulated spot therefore misses the crosshair; after calibrating
on it, it lands on target. That makes the calibration step meaningful in simulation.
"""

from __future__ import annotations

import math

import numpy as np

from galvo.config import RigConfig
from galvo.geometry import Affine2D, Points


def simulated_truth(width: float, height: float, config: RigConfig) -> Affine2D:
    """Image px -> logical galvo volts for a view of ``width`` x ``height`` pixels."""
    angle = math.radians(3.0)
    span = 0.8 * (config.daq.ao_max_v - config.daq.ao_min_v)  # the view sees 80% of travel
    scale = span / max(width, height)
    rot = scale * np.array(
        [[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]]
    )
    centre = np.array([width / 2, height / 2])
    offset = np.array([0.6, -0.4]) - rot @ centre  # optical axis slightly off the view centre
    return Affine2D.from_linear(rot, offset)


def channel_volts_to_px(ao: Points, truth: Affine2D, config: RigConfig) -> Points:
    """Channel volts (ao0, ao1) -> where the spot appears in the simulated image."""
    galvo = ao @ np.linalg.inv(config.axes.as_array()).T
    return truth.inverse().apply(galvo)
