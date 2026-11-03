"""Synthetic FIFO execution fixture for tests only; never user/research data."""
import argparse
import struct
from pathlib import Path
from make_fixture import header

def frames():
    yield header('S', 0) + b'O'
    yield header('S', 0) + b'Q'
    yield header('H') + b'TEST    T     '
    def add(reference, side, time):
        price = 1_000_000 if side == b'B' else 1_000_200
        return header('A', timestamp=time) + struct.pack('>QcI8sI', reference, side, 100, b'TEST    ', price)
    def execute(reference, quantity, match, time):
        return header('E', timestamp=time) + struct.pack('>QIQ', reference, quantity, match)
    yield add(1, b'B', 1_000_000)
    yield add(2, b'S', 1_000_000)
    # A non-displayed print advances virtual time, but is never a private fill.
    yield header('P', timestamp=3_000_000) + struct.pack('>QcI8sIQ', 900, b'B', 1, b'TEST    ', 1_000_100, 1000)
    yield add(3, b'B', 4_000_000)
    yield execute(1, 100, 2001, 5_000_000)  # consumes queue ahead only
    yield execute(3, 30, 2002, 6_000_000)  # later order -> eligible buy fill
    yield add(4, b'S', 8_000_000)
    yield execute(2, 100, 2003, 9_000_000)  # consumes queue ahead only
    yield execute(4, 30, 2004, 10_000_000)  # later order -> eligible sell fill

def write(path):
    with Path(path).open('wb') as output:
        for frame in frames():
            output.write(struct.pack('>H', len(frame)))
            output.write(frame)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output')
    write(parser.parse_args().output)
