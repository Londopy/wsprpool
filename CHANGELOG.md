# Changelog

All notable changes to wsprpool are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The section for a version is
what appears on its GitHub release page. Validated in CI by
[patchnotes](https://github.com/Londopy/patchnotes).

## [Unreleased]

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

[Unreleased]: https://github.com/Londopy/wsprpool/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Londopy/wsprpool/releases/tag/v0.1.0
