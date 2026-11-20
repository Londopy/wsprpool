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

UTC                     MHz    SNR     dt  drift  metric  message              found by       receivers alone / wspr.live reporters
2026-11-19 20:30  14.097149  -21.2   0.18   -0.5    58.7  <K0CFW> EN34OQ 23    combined only  - / 117
2026-11-19 20:30  14.097153  -29.4   0.15   -1.0    20.4  N0STP EN34 30        combined only  - / 93
2026-11-19 20:34  14.097102  -29.0   1.34   -1.0    11.6  K5EMI EM13 33        combined only  - / 43
2026-11-19 20:36  14.097014  -21.1  -0.12   -1.5   118.9  K8EB EN73 23         both           kiwisdr.k1ra.us-8073,kiwisdr.itfais.com-8073,sdr.k1vl.com-8077 / 88
...
```

## Install

With Python 3.11 or newer:

```console
pip install wsprpool
```

The wheel and sdist are also on the [latest release](https://github.com/Londopy/wsprpool/releases/latest)
with checksums and torrents. Or from source:

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
gain over one receiver. There is only one station per slot, so `sim` skips the
subtraction pass.

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
the lookup, and `--csv spots.csv` to also get the table as a CSV file.

All three WSPR message types decode. Type 2 carries a compound call like `PJ4/K1ABC` and
no locator. Type 3 carries a 6-character locator and only a 15-bit hash of the call; it
shows as `<K1ABC> FN42AB 37` once the hash is matched to a call heard in another message
of the session, or to a wspr.live report from that locator, and as `<...>` until then.

## Results so far

With 20 slots per point, each threshold below is good to about half a decibel.

**Simulated, independent Rayleigh fading at each receiver** (Watterson model, 0.2 Hz
Doppler spread): the SNR where half the slots decode drops from -28.2 dB with one
receiver to -30.6 dB with two (2.4 dB gain) and -33.0 dB with four (4.8 dB). Each site
fades at different times, so the combination rides through each one's fades.

**Simulated, plain noise:** the 50% point moves from -29.0 dB with one receiver to
-31.5 dB with two and -33.2 dB with four, a gain of 2.5 and 4.2 dB against an ideal of 3
and 6 dB. In a separate run eight receivers reached -34.7 dB against -29.5 dB for one,
a 5.2 dB gain against an ideal of 9. The shortfall is expected: each receiver
demodulates noncoherently on its own, and noncoherent detection loses more the weaker the
signal is at each site.

**Real, 2026-11-19 20:30 to 20:38 UTC**, 20 m, three KiwiSDRs (in the FN33, FM18 and EM96 grid squares),
four slots: 72 spots were decoded by at least one receiver alone and 72 by the combination.
Six spots were decoded only by the combination. Five of them were reported by 43 or more
other stations on wspr.live with the same grid and power: KN2M, N0STP, K5EMI, K7CMI, and
K0CFW's hashed type 3 message. The sixth, `7F3NQR JH81 57` at metric 11.4, was reported
by nobody and is almost certainly a false decode; a real spot in the same session scored
11.6, so no metric cutoff separates the two (see Limits). Every other spot was reported by
at least 11 other stations. Six spots that one receiver decoded alone were missed by the
combination. In the cases examined, the other receivers were hearing a different station
close by in frequency and in step with it, and their confident bits for the wrong station
outvoted the right one.

The same session, decoded with fewer features:

| Decoder | Alone | Combined | Combined only |
|---|---|---|---|
| Type 1 only, one pass, no drift search | 54 | 49 | 2 |
| Plus subtraction and a second pass | 60 | 58 | 5 |
| Plus type 2 and 3 messages | 67 | 64 | 4 |
| Plus the drift search (this release) | 72 | 72 | 6 |

The combined-only count can drop as the decoder improves, because a better single-receiver
decode moves a spot from "combined only" to "both". Decoding the four slots took about
8 minutes.

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
   frequencies (±1 Hz), start times (0 to 5 s) and drifts (±4 Hz over the transmission),
   and the scores are summed across receivers. Symbol powers are computed once on the
   frequency grid; a drifting signal is a path across that grid, so each drift costs
   only a lookup.
4. **Soft bits.** At the best frequency and time, each receiver computes the exact
   noncoherent 4-FSK log-likelihood ratio for every data bit. The noise level comes from
   the two tones the sync bit rules out, and the signal level is tracked over 15 symbols.
   A receiver in a fade, or one that hears nothing, therefore contributes almost nothing.
5. **Combine and decode.** The LLRs are added, deinterleaved and fed to a Fano sequential
   decoder for the K=32, rate 1/2 WSPR code. Adding true LLRs is the optimal combination
   for independent receivers.
6. **Subtract and decode again.** Each decoded message is re-encoded, and its tones are
   fitted to every receiver's samples, tracking amplitude and phase over one-second
   windows so fading is followed. A fit only counts if the known tone beats the tone for
   the opposite data bit well above chance, so a neighbour can't be subtracted by mistake.
   The fitted signals are removed and the whole search runs again on what's left. Each
   receiver alone and the combination subtract only what they decoded themselves.

The encoder is checked symbol for symbol against the WSJT-X reference for
`K1ABC FN42 37`, and against values worked out from the WSJT-X sources for type 2 and 3
messages. The callsign hash matches the hashes real type 3 transmissions carried in the
session above.

## Limits

- The decoder is pure Python. A slot with 18 signals and three receivers takes about two
  minutes, nearly twice as long as without the second pass.
- Two stations close in frequency break the "independent noise" assumption: a receiver
  that hears the other station loudly adds confident wrong bits. Subtraction recovers the
  weaker one from about 0.5 Hz of separation up, but at 0.2 Hz the two can't be told
  apart well enough to subtract.
- The metric column is the Fano path metric. Decodes below 10 are dropped, but in
  simulation a weak signal near the decode threshold occasionally came out as a wrong
  message scoring above 10, and the real session above has one such decode. Accepting
  all three message types and searching drift both widen what noise can pass for, so
  treat a `combined only` spot as real only when wspr.live confirms it.
- A hashed call shares its 15-bit hash with roughly one in 30,000 other calls, so a
  type 3 message named in-session can name the wrong one. A wspr.live report with a
  matching hash, locator and power overrides it.
- `wsprd` also decodes a rare type 3 form that carries a 4-character locator (a
  compound call with a numbered suffix, like `<PA0SKT/2> JO33 40`). wsprpool drops it.
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
