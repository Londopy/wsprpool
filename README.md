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
2026-11-19 20:30  14.097149  -22.5   0.18   -0.5    91.3  <K0CFW> EN34OQ 23    combined only  - / 117
2026-11-19 20:30  14.097153  -29.4   0.15   -1.0    20.4  N0STP EN34 30        combined only  - / 93
2026-11-19 20:34  14.097102  -29.0   1.34   -1.0    13.5  K5EMI EM13 33        combined only  - / 43
2026-11-19 20:36  14.097015  -14.8  -0.12    0.0   120.5  K8EB EN73 23         both           kiwisdr.k1ra.us-8073,kiwisdr.itfais.com-8073,sdr.k1vl.com-8077 / 88
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
error, and with `--doppler`, its own Rayleigh fading. `--shared 0.3` makes 30% of every
receiver's noise power the same waveform at all of them, like interference that reaches
every site. The table shows how often the combination decodes at each SNR, then the 50%
threshold for each receiver count and the gain over one receiver, each with one standard
error worked out from the number of trials. There is only one station per slot, so `sim`
skips the subtraction pass.

### Record

```console
wsprpool record kiwisdr.example.org:8073 other.example.net:8073 -o evening.londopy --minutes 10
```

Before connecting, wsprpool checks each receiver's `/status` page. It skips any that are
offline, full, without a GPS fix (a receiver without GPS timestamps can't be lined up with
the others), or whose owner allows no recording programs. Once recording, it drops a
receiver that connects but sends nothing within 45 seconds, and stops one as soon as the
receiver ends the session, usually at the owner's time limit, keeping what it recorded.
kiwiclient on its own would reconnect and carry on past the limit; wsprpool doesn't. Recording starts just before the next even minute and runs for
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
that reported the same transmitter in the same slot is printed. wspr.live is a
third-party public database that copies the spots reported to WSPRnet. It is not WSPRnet
itself, but it holds the same spots and has a query API. A `combined only` spot that others
also heard is very unlikely to be a false decode. Pass `--no-check` to skip the lookup,
and `--csv spots.csv` to also get the table as a CSV file.

The summary ends with the correlation between receivers' LLR errors at the combined spots.
Each receiver's LLRs are signed by the decoded bits and their 15-symbol moving average
taken out, which leaves that receiver's noise; only pairs of receivers that both hear the
spot count. Independent receivers give about 0. Interference that reaches
several of them at once pushes it up, and with correlation c, N receivers are worth only
N / (1 + (N - 1) c) independent ones.

All three WSPR message types decode. Type 2 carries a compound call like `PJ4/K1ABC` and
no locator. Type 3 carries a 6-character locator and only a 15-bit hash of the call; it
shows as `<K1ABC> FN42AB 37` once the hash is matched to a call heard in another message
of the session, or to a wspr.live report from that locator, and as `<...>` until then.

## Results so far

Every simulated point is 20 slots. The 50% thresholds carry one standard error from the
binomial spread of those 20 (`wsprpool sim` prints it); the 90% points read off the same
tables are rougher.

**Simulated, plain noise.** The SNR where half the slots decode:

| Receivers | 50% threshold | Gain | Ideal |
|---|---|---|---|
| 1 | -29.0 ± 0.3 dB | | |
| 2 | -31.5 ± 0.2 dB | 2.5 dB | 3 dB |
| 4 | -33.2 ± 0.2 dB | 4.2 dB | 6 dB |
| 8 | -34.7 ± 0.2 dB | 5.2 dB (against -29.5 for one in that run) | 9 dB |
| 10 | -35.4 ± 0.2 dB | 6.5 dB (against -28.9 in that run) | 10 dB |
| 16 | -35.8 ± 0.2 dB | 6.9 dB | 12 dB |

The gain flattens as receivers are added, because each one demodulates noncoherently on
its own and noncoherent detection loses more the weaker the signal is at each site. With
16 receivers each one sits near -36 dB, where that loss is large.

**Simulated, independent Rayleigh fading at each receiver** (Watterson model). How much
fading adds depends on how slow it is:

| Doppler spread | Receivers | 50% point | 90% point |
|---|---|---|---|
| 0.2 Hz (fast) | 1 | -28.2 ± 0.3 dB | -26.3 dB |
| | 2 | -30.6 ± 0.3 dB | -29.2 dB |
| | 4 | -33.0 ± 0.2 dB | -31.5 dB |
| 0.005 Hz (slow) | 1 | about -28.7 dB | -17 dB or worse |
| | 2 | -31.5 ± 1.1 dB | -22 dB |
| | 4 | -34.3 ± 0.3 dB | -30 dB |

With fast fading a transmission rides through many short fades on its own, since WSPR's
interleaving and code already spread each message over the whole 2 minutes, so a second
site adds about what it adds in plain noise. With slow fading one fade can last a whole
transmission, and a single receiver still misses more than one slot in ten 10 dB above
its 50% point. A second receiver is rarely in the same fade: at 90% reliability two
receivers gain about 5 dB and four about 13 dB, far more than the 3 and 6 dB that equal
receivers in plain noise would give. That is the extra gain diversity brings on HF.

**Simulated, shared noise.** With 30% of every receiver's noise power common to all of
them (`--shared 0.3`), four receivers gain 3.8 ± 0.3 dB over one, against 4.2 dB with
independent noise. The real sessions below measure how correlated real receivers are.

**Real, 2026-11-19 20:30 to 20:38 UTC**, 20 m, three KiwiSDRs (in the FN33, FM18 and EM96 grid squares),
four slots: 72 spots were decoded by at least one receiver alone and 75 by the combination.
Nine spots were decoded only by the combination, and 8 of the 9 are confirmed: wspr.live
has 43 or more other stations reporting each one with the same grid and power (KN2M,
N0STP, N5UTV, WD5TX, K5EMI, K7CMI, VE9XYZ, and K0CFW's hashed type 3 message). The
ninth, `7F3NQR JH81 57` at metric 11.4, was reported by nobody and is almost certainly a
false decode; a real spot in the same session scored 13.5, so no metric cutoff separates
the two (see Limits). Every other spot was reported by at least 11 other stations. The
receivers' LLR errors at combined spots correlate by 0.09 on average over 113 pairs, so
these three sites behave as nearly independent. Six spots that one receiver decoded alone
were missed by the combination. In the cases examined, the other receivers were hearing a different station
close by in frequency and in step with it, and their confident bits for the wrong station
outvoted the right one.

**Real, 2026-11-20 04:48 to 05:48 UTC**, 20 m, an hour from three KiwiSDRs in Australia
and New Zealand (QF12 near Ararat, PF95 in South Australia, RF81 at Rotorua). Two of them
end every session at their owners' 30-minute limit, so the first 30 minutes have all
three receivers and the second only the Ararat one, which turned out to hear almost
nothing on 20 m that night (6 spots in the whole hour). In practice this is a two-receiver
combination for 15 slots. 118 spots were decoded by at least one receiver alone and 125
by the combination. Nine were decoded only by the combination, and 7 of the 9 are
confirmed on wspr.live by 1 to 37 other stations (KH6TZ, KO4UST, HK3LRB, KI6DS twice,
TI4JWC and HP2DFA). The other two, `CT9/DL7VXX 33` at metric 53.1 and `TC4FST JD71 30`
at 14.1, were reported by nobody on 20 m within the hour and are probably false decodes;
the first shows that a wrong message can score well above the cutoff. The LLR error
correlation was 0.00 over 122 receiver pairs. Decoding took about 12 minutes.

The US session, decoded with fewer features:

| Decoder | Alone | Combined | Combined only |
|---|---|---|---|
| Type 1 only, one pass, no drift search | 54 | 49 | 2 |
| Plus subtraction and a second pass | 60 | 58 | 5 |
| Plus type 2 and 3 messages | 67 | 64 | 4 |
| Plus the drift search (0.1.0) | 72 | 72 | 6 |
| Plus each receiver's own frequency (this release) | 72 | 75 | 9 |

The combined-only count can drop as the decoder improves, because a better single-receiver
decode moves a spot from "combined only" to "both". Decoding the four slots took about
6 minutes.

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
4. **Each receiver's frequency.** Kiwis that are all GPS-locked still put the same signal
   at different frequencies: in one slot of the US session, two of them disagreed by about
   1 Hz on both of the stations they both heard well. Every receiver runs the sync search on its own as well, and
   where its own score clearly shows the signal (6 or more, where noise peaks at about 4)
   within 1 Hz and 64 samples of the joint peak, the combination demodulates that receiver
   at its own frequency. A receiver that can't see the signal keeps the joint frequency,
   since the best of many frequencies picked from noise only adds noise. An own peak can
   also belong to a neighbouring station, so each such hypothesis is decoded a second time
   with every receiver on the joint frequency, and the better decode is kept.
   In simulation, three receivers 40 trials each, compared with the rule switched off:

   | Receiver offsets | SNR | One joint frequency | Own frequencies |
   |---|---|---|---|
   | -0.4, 0, +0.4 Hz | -31.5 dB | 21 of 40 | 38 of 40 |
   | -0.9, 0, +0.9 Hz | -30.5 dB | 11 of 40 | 37 of 40 |
   | all agree | -31.5 dB | 38 of 40 | 38 of 40 |

   `wsprpool sim --freq-spread` sets how far apart the simulated receivers' oscillators
   are.
5. **Soft bits.** At its frequency and the joint start time, each receiver computes the
   noncoherent 4-FSK log-likelihood ratio for every data bit from its own estimates of
   signal and noise. The noise level comes from the two tones the sync bit rules out, and
   the signal level is tracked over 15 symbols.
6. **Combine and decode.** The LLRs are added, deinterleaved and fed to a Fano sequential
   decoder for the K=32, rate 1/2 WSPR code. There is no separate SNR weight, on purpose:
   an LLR already carries the receiver's measured SNR, because it is built from that
   receiver's own signal and noise estimates. A receiver 6 dB weaker gets 5 to 9 times
   less say (more than the 4 times a linear SNR weight would give, since noncoherent
   detection loses more as SNR drops), and one in a fade, or hearing nothing, adds almost
   nothing. For independent receivers whose LLRs are calibrated, adding them is the
   optimal combination, and multiplying by SNR again would count each receiver's SNR
   twice. In simulation they are close to calibrated in plain noise and somewhat
   overconfident in fast fading.
7. **Subtract and decode again.** Each decoded message is re-encoded, and its tones are
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

- The decoder is pure Python. A slot with 18 signals and three receivers takes about a
  minute and a half, nearly twice as long as without the second pass.
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
- Decoding is offline: record first, decode after. A live version would need the decoder
  to keep up with the 2-minute cycle, which pure Python doesn't with many receivers.
- A receiver's own frequency is only used once it clearly sees the signal, so receivers
  that are each far below the noise still share one frequency.

## Prior art

- **KiwiSDR TDoA.** The KiwiSDR network already combines receivers: its [TDoA
  feature](https://www.rtl-sdr.com/kiwisdr-tdoa-direction-finding-now-freely-available-for-public-use/)
  pulls GPS-timestamped IQ from several Kiwis and cross-correlates them to locate a
  transmitter. It finds where a signal comes from rather than what it says, and works on
  signals each receiver already hears. wsprpool relies on the same GPS timestamps.
- **wsprdaemon.** [wsprdaemon](https://wsprdaemon.readthedocs.io/en/master/description/overview.html)
  runs many receivers and can merge them into one virtual receiver that reports, for each
  spot, the copy with the best SNR. That is selection combining: it can only report what
  at least one receiver decoded on its own. We found no published WSPR decoder that adds
  soft decisions across receivers.
- **Diversity combining.** Maximal-ratio combining (D. G. Brennan, "Linear diversity
  combining techniques", Proc. IRE, 1959) weights and adds branches before detection.
  Arrays in space links do this coherently: for Voyager 2 at Neptune in 1989, NASA added
  the Very Large Array's antennas to the Deep Space Network's after phasing them in
  real time. Combining soft decisions after noncoherent detection, as here, needs no phase
  agreement between sites, which is what makes receivers thousands of kilometres apart
  usable. Patents describe the same for noncoherent links in general, for example US
  10,243,696 (diversity combining of noncoherently modulated LDPC codes).

## Rules

Receiving needs no license in the US, and wsprpool only receives, through KiwiSDRs whose
owners make them public. Follow each owner's terms; see the section above. Other
countries' rules on receiving, and on recording and processing what you receive, differ,
and wsprpool doesn't try to cover them: check your own country's before you rely on it.

A natural next test is to run your own low-power WSPR beacon on a schedule and see how
far its power can be turned down while the combination still decodes it. That needs an
amateur license for the band, so it is left to a licensed operator; wsprpool doesn't
transmit.

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
