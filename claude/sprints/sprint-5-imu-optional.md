# Sprint 5 — IMU Logging (optional)

**Status:** Planned, awaiting your approval. Only start after Sprint 3 (or earlier if you choose. It needs only the Sprint 1 convention).
**Required for Phase 1:** No (stretch)
**Depends on:** Sprint 1 (timestamp convention), Sprint 3 (session runner)
**Effort:** S–M

## Goal

Log raw IMU data (accelerometer and gyroscope, plus whatever else the sensor provides) with timestamps in the shared convention.

## Scope (from the brief)

**In:**
- IMU sensor read + timestamped logging

**Out:**
- Sensor fusion, filtering, bias correction, orientation estimation, or any interpretation. **Raw values only.**

## Unknown that shapes the sprint

The brief doesn't name an IMU, and I don't know what you have. The interface (I2C vs SPI), register map, sample rate limits and whether it has a FIFO all depend on the part. **Task 1 is identifying it.** Until then, everything below is written to hold for any typical 6/9-axis MEMS IMU.

## Decisions this sprint makes (with my recommendation)

| Decision | Recommendation | Why |
|---|---|---|
| Sample rate | Start at 100 Hz and go up only if the sensor and Pi timing can hold it | Well above the vehicle dynamics you care about, and comfortable for I2C on the 3B. The measured achievable rate decides the final value. |
| Timestamping | Use the sensor's FIFO or data-ready interrupt if it has one, and timestamp on arrival, recording the polling-jitter bound. Otherwise poll on a fixed schedule and timestamp right after each read | Python polling on a busy Pi 3B has jitter. A FIFO or interrupt keeps samples from being lost, and the timestamp error can be bounded and documented. |
| What's stored | Raw integer counts exactly as the sensor reports, plus the configuration (full-scale range, sample rate, digital filter settings) in the manifest so counts can be converted later | Keeps Phase 1 free of interpretation but keeps the data usable. |
| Mounting frame | Record where and how the IMU is mounted (axes vs car axes) in the manifest, even if approximate | Without this, Phase 2 can't use the data. It's a description, not a calibration. |

## Tasks

- [ ] Identify the IMU: part number, breakout board, interface, voltage levels, and what's already wired. Document wiring and Pi config (I2C/SPI enable, bus speed)
- [ ] Bring up the sensor, read the WHO_AM_I or equivalent, and configure range and rate. Check the readings by tilting and shaking it by hand (gravity on the right axis, gyro responds to rotation)
- [ ] Measure the achievable sample rate and timestamp jitter on the 3B, solo and while frame capture and CAN logging are running
- [ ] Define `imu.csv` columns (timestamp, raw axes, temperature if available) and add them to the data contract
- [ ] Logger with the same writer/queue/flush pattern as the other streams. Own entry point, no dependency on any other stream
- [ ] Track missed samples: FIFO overflow flags or gaps in the sample counter, written to the manifest
- [ ] Session runner integration as an enabled/disabled stream
- [ ] Update `verify_session`
- [ ] Vehicle test: with all streams on, check the IMU shows plausible response to braking, acceleration and turns (spot-check by eye. No processing is built)

## Acceptance criteria

1. A session's `imu.csv` has raw timestamped samples at the documented rate, with missed samples counted.
2. Static and manual-motion checks give sensible raw values on the expected axes.
3. Running with all streams on doesn't reduce the camera's or CAN logger's measured performance beyond a documented amount. If it does, the numbers go to you before anything is changed.
4. IMU logging runs with every other stream off, and vice versa.
5. Config and mounting notes are in the manifest and the data contract is updated.

## Risks

- **I2C polling jitter and clock stretching** on the Pi 3B can cause irregular timing. FIFO/interrupt use is the mitigation, and SPI is better than I2C if the part supports it.
- **Vibration and mounting** affect the data quality but not the logging. Noted in the manifest. Fixing them is beyond this sprint.
- **Adding a third stream** to an already loaded Pi 3B. Measured in the sprint, since Sprint 3 only proved two.

## Open questions for you

1. Which IMU do you have, and how is it connected?
2. Where is it mounted and in what orientation?
3. Any minimum sample rate you need?
