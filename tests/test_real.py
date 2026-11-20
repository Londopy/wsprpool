from pathlib import Path

import numpy as np
import pytest

from wsprpool import Message, decode_slot

# 20 m, 2026-11-19 20:34 UTC, three GPS-locked KiwiSDRs in FM18, EM96 and FN33, as 375 Hz
# baseband windows starting one second before the even minute
SLOT = Path(__file__).parent / "data" / "slot-20261119-2034.npz"


@pytest.mark.slow
def test_real_slot_gives_spots_only_the_combination_decodes():
    data = np.load(SLOT)
    res = decode_slot({k: data[k].astype(complex) for k in ("fm18", "em96", "fn33")})
    alone = {s.message for spots in res.alone.values() for s in spots}
    combined = {s.message for s in res.combined}
    # both were reported to wspr.live by dozens of other stations in this slot
    for msg in (Message("K5EMI", "EM13", 33), Message("K7CMI", "DN57", 27)):
        assert msg in combined and msg not in alone
    assert Message("W6LPM", "CM97", 27) in alone & combined
