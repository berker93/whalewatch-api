"""How a ticker is spelled here, wherever one comes in.

One spelling per ticker, so that a ticker from OpenFIGI, one typed into
``resolve-cusip`` and one in a request path all compare equal: upper case,
with a share class after a dot. OpenFIGI writes ``BRK/B``, which cannot go in a
path (``/v1/stocks/BRK/B`` is a different route), and many clients write
``BRK-B``. Both are read as ``BRK.B``.
"""


def normalise_ticker(ticker: str) -> str:
    """``brk/b``, ``BRK-B`` and `` brk.b `` are all ``BRK.B``."""
    return ticker.strip().upper().replace("/", ".").replace("-", ".")
