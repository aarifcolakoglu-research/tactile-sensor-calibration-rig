# Tactile sensor calibration rig (ATI_App) — version 1.0

> **This is version 1.0 (February 2026), kept for reproducibility.** It is the version
> that recorded the sensor calibrations of the article below (sensor set S1_MatC,
> 31 January – 2 February 2026). For the current version, the full documentation and the
> firmware, see the latest release of this repository and its README.

> Colakoglu A. A. *et al.* **Evaluation of an Instrumented Helmet for Measurement of
> Orthosis–Head Interface Forces in Pediatric Craniosynostosis.**
> *(manuscript under review; citation will be updated on publication)*

Control and acquisition software for a three-axis rig that calibrates soft tactile force
sensors against an ATI Nano17 force/torque transducer. It drives three Thorlabs
KDC101 / MTS50-Z8 stages, reads the transducer and the Z encoder through an NXP FRDM-K64F
board (Ethernet, `192.168.5.101:7851`) and the sensor under test through a Teensy (USB
serial), and records everything to `output/<sequence name>_<YYYY-MM-DD-HH-MM-SS>.csv`.

## Run

Windows, Python 3.11 or 3.12 (64-bit), Thorlabs Kinesis (64-bit):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements_acq.txt
python main.py
```

The application needs all three stages to start.

## Calibration protocol used in the article

`sequences/C_Z1p8_H10_C5_R20.txt`: after the touch-probe routine has set the
zero-indentation reference, the Z stage advances in 0.1 mm steps at 0.5 mm/s to 1.8 mm
with a 10 s hold after every step, unloads in the same steps, 5 cycles, 20 s rest between
cycles.

The touch-probe routine of this version approaches at 0.5 mm/s over at most 2.5 mm,
accepts contact at 0.015 N held for 50 ms, stops at 0.08 N or 1.0 N/s, presses 0.2 mm
further, retracts 0.8 mm and returns to the contact position in 0.02 mm steps.

## Output columns

`real_time, device_time_us, t_rel_us, dt_us, fs_hz, px, py, pz, fx, fy, fz, tx, ty, tz,
raw_bx, raw_by, raw_bz, status_byte, n_bx, n_by, n_bz` — the same columns as the stream
file of later versions. For the calibration pipeline in
[sensor-tracking-studio](https://github.com/aarifcolakoglu-research/sensor-tracking-studio)
the files are renamed `<YYYY-MM-DD-HH-MM-SS>_<sequence>_<set>_<position>.csv`.

## Acknowledgements

Adapted from the needle-insertion test system by Will Stokes
([NeedleInsertionApp](https://github.com/WillEStokes/NeedleInsertionApp); K64F firmware:
[Needle_controller-mbed](https://github.com/WillEStokes/Needle_controller-mbed)).

## Licence

[GNU General Public License v3.0](https://www.gnu.org/licenses/gpl-3.0.html) (GPL-3.0-only). See `LICENSE`.
