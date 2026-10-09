# Hardware contract

This file is the single human-readable source of truth for the rig. The single
*machine*-readable source is [`config/rig.toml`](config/rig.toml); the two must agree.
Anything marked **UNVERIFIED** has not been measured on the real rig.

## The rig

UV spot-photolysis on a commercial microscope (an **Olympus IX50** inverted microscope;
the beam enters at the back via a 45° fold, per Ian, 2026-10-09). Two galvanometer
mirrors steer the beam; the lasers are gated by TTL. Specified and built by Ian F Smith at
UC Irvine. The 45° fold and the camera path probably mirror the image relative to the
wall test, which would explain the original software's `−x`; the first calibration on
the microscope will show the true orientation.

- **Lasers.** Two diode laser modules: a **445 nm** guide laser and a **405 nm**
  uncaging laser, aligned to the same spot. (Remembered beforehand as a 488 nm guide;
  the module is stamped 445.) The guide laser excites Cal-520, so its spot shows in the
  camera image and marks where the 405 will land. See "Lasers" below.
- **Targeting display.** The original app was a see-through overlay window (adjustable
  opacity) placed over the imaging software's live view. There is no camera interface.

## Galvo system: Thorlabs GVS002 (traced 2026-10-09)

Identified from photos: **GVS002** two-axis scanner (motors labelled -X / -Y),
**GPS011** ±15 V power supply, two servo driver boards each under a **GCE001** cover
(boxes labelled "1" and "2"). Reference: Thorlabs manual HA0193T (GVS001/GVS002), rev.
22 Feb 2019.

### Command wiring, USB-6001 to driver board J7

Traced by hand by Ian, 2026-10-09.

| USB-6001 terminal | Driver board 1 J7 | Driver board 2 J7 |
|---|---|---|
| AO 0 | pin 1 (command +) | |
| AO 1 | | pin 1 (command +) |
| AO GND | pin 8 (ground) | pin 8 (ground) |
| AI GND | pin 2 (command −) | pin 2 (command −) |

- **AO 0 → board 1 → Y galvo; AO 1 → board 2 → X galvo** (motor cables traced; an
  initial reading of board 1 → X was corrected on the day).
- Command − goes to AI GND rather than AO GND. This is fine if the USB-6001's grounds
  are common inside the box: **UNVERIFIED** (check AI GND to AO GND continuity, unplugged).
- This matches Brett's code, which sends `(ao0, ao1) = (y, −x)`: his screen y drives the
  Y galvo and his screen x drives the X galvo, sign-inverted. Which way the spot moves
  on the image for each is still for the wall test.

### Driver board facts from the manual

- J7 accepts a **differential** command; with 0.8 V/° scaling, ±10 V gives ±12.5°
  mechanical (±25° optical).
- **JP7** sets the scaling: 0.8 V/° (factory default), 1 V/° or 0.5 V/°. At 0.5 V/° the
  input must stay within ±6.25 V.
- **Checked 2026-10-09: JP7 is open on both boards.** The cap is present but parked on
  one pin (not bridging two), which is the **1 V/°** setting: ±10 V gives ±10°
  mechanical (±20° optical). The ±10 V limits in rig.toml are safe.
- **J6 pin 1** outputs the measured mirror position (0.5 V/°; J6 pin 8 = ground).
  Wiring it to a USB-6001 analog input would allow settling time to be measured.
- Small-angle step response is specified at 300 µs.
- Thorlabs recommends ≥20 kS/s, 16-bit DACs for scanning. The USB-6001 (5 kS/s,
  14-bit) is adequate for move-and-hold targeting, not for fast raster scanning.

## Lasers (traced 2026-10-09)

Two generic, unbranded diode laser modules:

- **445 nm**: a small black tube module with its driver built in, labelled Class IIIb
  (≤500 mW; output-power box not legible). Three wires: red, black and yellow,
  presumably +supply, ground and TTL; the TTL comes from P0.0. Runs on **12 V**: its
  supply wires are soldered to the 405's before reaching the power supply, so both
  lasers shared one 12 V feed in the working rig.
- **405 nm**: a blue finned module with a rear fan and a separate constant-current
  driver board (blue trimmer sets power). The board's input capacitor is rated 25 V,
  consistent with a **12 V** supply.

Both are powered from a **B&K Precision 1670** triple-output bench supply at **12 V**,
through one shared +/− pair, with the USB-6001's D GND joined to the supply's (−).
Neutral-density filters are believed to sit in front of the lasers (certainly the
445) to set intensity at the sample: **UNVERIFIED**.

| Laser | USB-6001 line | Role | Status |
|---|---|---|---|
| 445 nm (stamped on module) | **P0.0** | aim / guide | line traced |
| 405 nm | **P0.2** | uncaging | line traced; wavelength believed, sticker unchecked |
| none | P0.1 | unused | traced: nothing connected |

Brett's saved settings put Laser 2 on line 1, which does not match the current wiring
(the wiring may have changed since 2017).

405 driver board connectors (from photo, traced 2026-10-09): **power in** at top left;
the TTL control is a **thin wire hand-soldered** onto the board beside its LM358 op-amp
(a modification, not a factory TTL input); **bottom-left red/black pair → fan**;
**bottom-right, thicker red/black pair → laser diode**. Never plug or unplug the diode
pair while the driver is powered.

Ground: the USB-6001's **D GND** is believed to be tied to the negative of the power
supply feeding both laser drivers, which gives the TTL signals their reference.
**UNVERIFIED**: check continuity, unplugged.

Electrical notes:
- USB-6001 digital outputs are 3.3 V logic (high = 2.8–3.6 V, ±4 mA). Most TTL driver
  inputs accept this; confirmed in practice once a laser switches.
- At power-on, and whenever the USB-6001 is unplugged, its digital lines are inputs
  with a 47.5 kΩ pull-down.
- **Tested 2026-10-09, USB-6001 unplugged, 12 V on: the 405 comes ON; the 445 stays
  off.** The 405's hand-added TTL input does not default to off. Until fixed, the 405
  emits whenever its supply is on and nothing actively drives P0.2, which is both an
  eye hazard and unlogged photolysis.
- **Tested 2026-10-09 in NI MAX test panel (Dev4, port0 all outputs):** all lines
  driven low → both lasers off. **P0.2 high → 405 on** (active-high, as the software
  assumes). P0.0 high did not light the 445 at first; later the same day the 445
  switched on and off from P0.0 as expected.
- **2026-10-09: the full chain works on the rig.** Targeting window on the real
  USB-6001 (Dev4): aiming with the 445, uncaging laser control, and calibration.
- Power class unknown; such modules are often hundreds of mW to watts (Class 3B/4).
  Goggles rated for 405 and 445 nm for any open-beam work.

## DAQ: NI USB-6001

From the NI datasheet (`reference/NI_USB6001_Specs.pdf`):

| Item | Value |
|---|---|
| Analog outputs | 2 (`ao0`, `ao1`), 14-bit, ±10 V |
| AO max update rate | 5 kS/s simultaneous per channel, **hardware-timed** |
| AO FIFO | 2,047 samples |
| AO trigger sources | software, PFI0, PFI1 |
| Digital I/O | P0.0–7, P1.0–3, P2.0. **Static (software-timed) only** |
| PFI0 / PFI1 | P2.0 / P1.1: digital trigger or counter source |
| AO power-on state | 0 V, with a −7 V startup glitch for 10 µs |

Consequences for the design:

- Mirror movements can be hardware-timed (5 kS/s). **Laser gates cannot**: they are
  software-timed, about 1 ms on Windows. Ian has confirmed 1 ms is fast enough.
- Trigger-out to the camera can only be a software-timed digital line.
- Trigger-in is read by polling a digital input (also about 1 ms).
- Only 2 AO channels exist, and both drive the galvos.

## Contract taken from the original software

| Item | Value | Status |
|---|---|---|
| Galvo channels | `Dev1/ao0`, `Dev1/ao1` | from source; on the new rig PC the device is **Dev4** (NI Device Monitor, 2026-10-09) |
| Voltage range | −10 V to +10 V on both axes | from source |
| Laser gates | `Dev1/port0/line0:7` | from source |
| Laser 1 → line 0, Laser 2 → line 1 | from saved settings | Current wiring differs: traced as 445 → P0.0, 405 → P0.2 |
| Axis convention | sent as `(ao0, ao1) = (y, −x)` | Wall test 2026-10-09, facing the wall: **+ao0 → spot down, +ao1 → spot right**, i.e. `(y, +x)`. Camera-image orientation **UNVERIFIED** |
| Settling time | not in original; placeholder 1 ms in `rig.toml` | **UNVERIFIED** |
| Trigger-in / trigger-out wiring | not in original | **UNVERIFIED** (not configured) |

## Quirks of the original, and what we do instead

1. **`(y, −x)` axis swap.** In this codebase it is a 2×2 matrix in `rig.toml`
   (`[axes] matrix`) and nowhere else. Once a calibration exists, it absorbs any
   rotation or flip, so an axis error cannot misplace a calibrated shot. The matrix still
   matters for the uncalibrated default view and for making "right" mean right.
2. **`[y, y, −x, −x]` double write.** Goes with `bufferSize = 2` and a commented-out
   sample-clock line. Without a clock the task writes on demand, so this is almost
   certainly leftover. Not copied. To confirm in Phase 3.
3. **Pickle settings.** Replaced by TOML (rig) and JSON (calibration).
4. **Global `ui` object.** Replaced by one-way module dependencies; see `CLAUDE.md`.
5. **Silent "debug mode" when the DAQ library is missing.** Replaced by an explicit
   backend choice. The simulator is never used without being asked for, and every log
   record states which backend ran.

## Measurement plan (rig visit)

1. ~~Axis direction~~: wall test done 2026-10-09 with NI MAX (+ao0 down, +ao1 right).
   Still to confirm against the camera image on the microscope.
2. **Settling time.** Method to agree on together; a galvo position-feedback output read
   on AI0 would measure it directly.
3. ~~Laser lines~~: traced 2026-10-09 (445 → P0.0, 405 → P0.2).
4. **405/445 alignment.** For example, bleach a mark on a fluorescent slide with the 405
   and check it sits under the 445 spot.
5. **Video** of the original app working.

Safety: goggles rated for 405 and 445 nm, lowest power, and a matte target at short
range for any open-beam test.
