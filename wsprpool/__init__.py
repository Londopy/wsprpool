"""Decode WSPR by adding the soft decisions of several receivers.

::

    from wsprpool import decode_slot
    slot = decode_slot({"rx1": window1, "rx2": window2})
    for spot in slot.combined:
        print(spot.message, spot.snr)
"""

from ._decode import Slot, Spot, decode_slot
from ._version import __version__
from ._wspr import Message, pack, symbols, unpack

__all__ = ["__version__", "Message", "Slot", "Spot", "decode_slot", "pack", "symbols", "unpack"]
