# Changelog

All notable changes to wsprpool are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The section for a version is
what appears on its GitHub release page. Validated in CI by
[patchnotes](https://github.com/Londopy/patchnotes).

## [Unreleased]

## [0.3.0] - 2026-11-23

### Added

- `wsprpool ui`: a local web page to pick KiwiSDRs on a map or in a list, record a session
  with a countdown to the slot and each receiver's progress, decode it slot by slot into a
  table that highlights combined-only spots and their wspr.live confirmations, and run the
  simulator as a chart. It follows the system light or dark setting, listens on 127.0.0.1
  only, and needs a per-run key on every API request. A busy `--port` fails with a clear
  message instead of sharing the port with another program on Windows.
- `wsprpool sim --weighting snr|equal` and `--snr-spread`: measure the plain LLR sum
  against SNR-weighted and equal-weighted combining on receivers of unequal strength.

### Changed

- `decode` and `sim` share their logic with the page; their output is unchanged.
- A simulation stopped from the page stops after the current slot, not the current point.
- `record` drops a receiver that keeps the connection but stops sending samples, keeping
  what it sent, and says when a recorder quits on its own well before the end.
- Picking the same receiver twice records it once.

## [0.2.0] - 2026-11-19

### Added

- Each receiver's own frequency offset: in the combination, a receiver that clearly sees
  the signal is demodulated at its own frequency instead of the joint one, with a second
  try at one shared frequency in case its own peak was a neighbour. In simulation with
  three receivers 0.4 Hz apart, the decode rate near threshold went from 21 to 38 out
  of 40.
- `wsprpool sim --shared`: a share of noise common to every receiver, like interference
  that reaches all the sites at once. `--freq-spread` sets how far apart the receivers'
  oscillators are.
- `wsprpool record` skips receivers whose owners allow no recording programs, and drops a
  receiver that connects but sends nothing within 45 seconds, so it doesn't hold a slot.
- A real 2-minute slot from three KiwiSDRs is kept as a test fixture.
- `wsprpool decode` measures how correlated the receivers' LLR errors are at combined
  spots, which shows how much shared noise costs.
- `wsprpool sim` prints one standard error for every threshold and gain.

### Changed

- The README explains why LLRs are added without a separate SNR weight, says which
  combined-only spots are confirmed, and adds prior art and a section on rules. It also
  has simulations with slow fading, shared noise and 10 and 16 receivers, and an hour
  recorded from three KiwiSDRs in Australia and New Zealand.

### Fixed

- `wsprpool record` reconnected when a receiver ended the session at its owner's time
  limit, and kept recording past it. It now stops that receiver and keeps what was
  recorded. A full receiver was also retried forever; now it is skipped.

## [0.1.0] - 2026-11-19

### Added

- WSPR type 1 encoder, a soft-decision 4-FSK demodulator and a Fano decoder.
- Decoding of one 2-minute slot from several receivers alone and with their log-likelihood
  ratios added together.
- `wsprpool sim`: decode rate and combining gain on synthetic signals, with optional fading.
- `wsprpool record`: simultaneous GPS-timestamped IQ recordings from several KiwiSDRs, saved as
  one `.londopy` session file.
- `wsprpool decode`: decodes a session alone and combined, and looks every spot up on
  wspr.live.
- A second decode pass: decoded signals are fitted, subtracted from each receiver, and the
  rest is decoded again, which finds weak stations half a hertz or more from strong ones.
  Each receiver alone, and the combination, only subtract what they decoded themselves.
- The Fano metric is printed with every spot. The default cutoff is 10.
- Simulated fading follows the Watterson model's Gaussian Doppler spectrum.
- All three WSPR message types: compound calls like PJ4/K1ABC (type 2) and 6-character
  locators with a hashed call (type 3). A hashed call is named from any other message in
  the session, or from a wspr.live report whose call has the same hash.
- A drift search of up to 4 Hz over the transmission, with the drift printed for each spot.
- `wsprpool decode --csv` writes the spots to a CSV file as well.

[Unreleased]: https://github.com/Londopy/wsprpool/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Londopy/wsprpool/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Londopy/wsprpool/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Londopy/wsprpool/releases/tag/v0.1.0
