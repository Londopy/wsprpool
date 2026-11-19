# Changelog

All notable changes to wsprpool are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The section for a version is
what appears on its GitHub release page. Validated in CI by
[patchnotes](https://github.com/Londopy/patchnotes).

## [Unreleased]

### Added

- WSPR type 1 encoder, a soft-decision 4-FSK demodulator and a Fano decoder.
- Decoding of one 2-minute slot from several receivers alone and with their log-likelihood
  ratios added together.
- `wsprpool sim`: decode rate and combining gain on synthetic signals, with optional fading.
- `wsprpool record`: simultaneous GPS-timestamped IQ recordings from several KiwiSDRs, saved as
  one `.londopy` session file.
- `wsprpool decode`: decodes a session alone and combined, and looks every spot up on
  wspr.live.

[Unreleased]: https://github.com/Londopy/wsprpool/commits/main
