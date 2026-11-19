# wsprpool

[![CI](https://github.com/Londopy/wsprpool/actions/workflows/ci.yml/badge.svg)](https://github.com/Londopy/wsprpool/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/Londopy/wsprpool)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB)](pyproject.toml)

**Decode WSPR signals that no single receiver can hear, by pooling several of them.**

wsprpool records the 20 m WSPR segment from several KiwiSDRs at once, lines the streams up
with their GPS timestamps, and demodulates each one separately down to soft bits. It then
adds those log-likelihood ratios together before the error-correcting decoder runs. Phases
never have to match between sites, because only the confidence values are combined.

An excerpt from a real 8-minute session with three KiwiSDRs in the eastern US:

```console
$ wsprpool decode test2.londopy
test2.londopy: 3 receivers at 14097.1 kHz

UTC                     MHz    SNR     dt  metric  message              found by       receivers alone / wspr.live reporters
2026-11-19 20:36  14.097015  -14.9  -0.12   116.6  K8EB EN73 23         both           kiwisdr.k1ra.us-8073,kiwisdr.itfais.com-8073,sdr.k1vl.com-8077 / 88
2026-11-19 20:36  14.097171  -28.7   0.19    11.0  N5JUJ EM32 23        combined only  - / 89
2026-11-19 20:36  14.097116  -30.4  -0.22    39.0  W5KV DM79 23         combined only  - / 145
...
```

## Install

From source, until there is a release:

```console
git clone https://github.com/Londopy/wsprpool
cd wsprpool
python -m venv .venv
.venv\Scripts\activate          # or: source .venv/bin/activate
pip install -e .
```

Recording uses `kiwirecorder.py` from [kiwiclient](https://github.com/jks-prv/kiwiclient),
which isn't on PyPI. Clone it anywhere and point wsprpool at it:

```console
git clone https://github.com/jks-prv/kiwiclient
set WSPRPOOL_KIWIRECORDER=C:\path\to\kiwiclient\kiwirecorder.py
```

`sim` and `decode` don't need kiwiclient.

## Usage

### Measure the gain on synthetic signals

```console
wsprpool sim --receivers 1,2,4,8 --snr=-37:-27 --trials 20
wsprpool sim --receivers 1,2,4 --doppler 0.2
```

Every receiver hears the same message with its own noise, a small frequency and timing
error, and with `--doppler`, its own Rayleigh fading. The table shows how often the
combination decodes at each SNR, then the 50% threshold for each receiver count and the
gain over one receiver.

### Record

```console
wsprpool record kiwisdr.example.org:8073 other.example.net:8073 -o evening.londopy --minutes 10
```

Before connecting, wsprpool checks each receiver's `/status` page. It skips any that are
offline, full, or without a GPS fix, since a receiver without GPS timestamps can't be lined
up with the others. Recording starts just before the next even minute and runs for
`--minutes`. The default `--freq 14097.1` kHz centres the 200 Hz-wide 20 m WSPR segment.

The result is one `.londopy` file holding every receiver's GPS-stamped IQ wav, so a
session can be shared and decoded again later. It's a zip with a `londopy.toml` and a
`session.toml` inside.

### Decode

```console
wsprpool decode evening.londopy
```

Each 2-minute slot is decoded by every receiver alone and by the combination. Each spot is
marked:

- `both`: the combination and at least one receiver alone decoded it.
- `combined only`: only the combination decoded it. These are the interesting ones.
- `alone only`: a receiver alone decoded it but the combination didn't, usually because
  another receiver had interference on that frequency.

Every spot is looked up on [wspr.live](https://wspr.live), and the count of other stations
that reported the same transmitter in the same slot is printed. A `combined only` spot
that others also heard is very unlikely to be a false decode. Pass `--no-check` to skip
the lookup.

## Results so far

**Simulated, independent Rayleigh fading at each receiver** (0.2 Hz Doppler spread,
20 slots per point): the SNR where half the slots decode drops from -26.4 dB with one
receiver to -29.2 dB with two (2.8 dB gain) and -31.0 dB with four (4.6 dB).

**Simulated, plain noise** (20 slots per point): the 50% point moves from -29.3 dB with one
receiver to -31.4 dB with two, -33.1 dB with four and -34.7 dB with eight. That is 2.0, 3.8
and 5.4 dB of gain against an ideal of 3, 6 and 9 dB. The shortfall is expected: each
receiver demodulates noncoherently on its own, and noncoherent detection loses more the
weaker the signal is at each site.

**Real, 2026-11-19 20:30 to 20:38 UTC**, 20 m, three KiwiSDRs (in the FN33, FM18 and EM96 grid squares),
four slots: 54 spots were decoded by at least one receiver alone and 49 by the combination.
Two spots were decoded only by the combination: N5JUJ, and W5KV at -30.4 dB at its best
receiver. Both were reported by more than 80 other stations on wspr.live with the same grid
and power, as was every other spot in the session. Seven spots that one receiver decoded
alone were missed by the combination. In the cases examined, the other receivers were
hearing a different station less than a hertz away and in step with it, and their
confident bits for the wrong station outvoted the right one (see Limits).

## Be a good guest on other people's receivers

Each KiwiSDR serves only a handful of users, and the owners pay for them. Keep sessions
short, check the receiver's own page for time limits and rules, and don't leave long
recordings running on popular receivers. wsprpool connects with the user name
`wsprpool`, so owners can see what's connected.

## How it works

1. **Baseband.** Each recording is shifted and resampled to 375 Hz complex samples. Its
   GPS block stamps give the true sample rate and the UTC time of the first sample.
2. **Candidates.** Averaged spectra from all receivers are added together. Peaks of the
   4-tone-wide smoothed spectrum become candidates, so a signal too weak to see at any one
   site can still show up in the sum. Each receiver's own peaks are added too.
3. **Sync.** For each candidate, the 162-bit sync vector is correlated over a grid of
   frequencies (±1 Hz) and start times (0 to 5 s), and the scores are summed across
   receivers.
4. **Soft bits.** At the best frequency and time, each receiver computes the exact
   noncoherent 4-FSK log-likelihood ratio for every data bit. The noise level comes from
   the two tones the sync bit rules out, and the signal level is tracked over 15 symbols.
   A receiver in a fade, or one that hears nothing, therefore contributes almost nothing.
5. **Combine and decode.** The LLRs are added, deinterleaved and fed to a Fano sequential
   decoder for the K=32, rate 1/2 WSPR code. Adding true LLRs is the optimal combination
   for independent receivers.

The encoder is checked symbol for symbol against the WSJT-X reference for
`K1ABC FN42 37`.

## Limits

- Only type 1 messages (plain callsign, 4-character locator, power). Compound callsigns and
  6-character locators (types 2 and 3) are skipped.
- No drift search and no subtraction of strong signals, both of which WSJT-X's `wsprd`
  does. Strongly drifting transmitters are decoded worse.
- The decoder is pure Python. A slot with many signals and several receivers takes a
  minute or more.
- Two stations a fraction of a hertz apart break the "independent noise" assumption. A
  receiver that hears the other station loudly adds confident wrong bits. Subtracting
  decoded signals before a second pass would fix most of these; it isn't done yet.
- The combination uses one frequency for every receiver. In the real session, three
  GPS-locked Kiwis put the same strong signal up to 0.47 Hz apart, which costs some
  sensitivity. A small per-receiver search around the joint peak helped some decodes and
  hurt others in a quick test, so it isn't in yet.

## Building from source

```console
pip install -e ".[dev]"
ruff check .
mypy wsprpool
pytest -m "not slow"
pytest -m slow
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).
