"""Generate an explicit synthetic protocol fixture, solely for tests/benchmarks."""
import argparse
import struct
from pathlib import Path

def header(kind, locate=1, timestamp=0):
    return kind.encode() + struct.pack('>HH', locate, 0) + timestamp.to_bytes(6, 'big')

def frames(cycles=1):
    yield header('S', 0) + b'O'
    yield header('S', 0) + b'Q'
    yield header('H') + b'TEST    ' + b'T' + b' ' + b'    '
    reference = 1
    for n in range(cycles):
        time = n * 1_000_000
        yield header('A', timestamp=time) + struct.pack('>QcI8sI', reference, b'B', 100, b'TEST    ', 1_000_000)
        yield header('A', timestamp=time) + struct.pack('>QcI8sI', reference + 1, b'S', 100, b'TEST    ', 1_000_200)
        yield header('E', timestamp=time) + struct.pack('>QIQ', reference, 30, n + 1)
        yield header('D', timestamp=time) + struct.pack('>Q', reference)
        yield header('D', timestamp=time) + struct.pack('>Q', reference + 1)
        reference += 2

def write(path, cycles=1):
    with Path(path).open('wb') as output:
        for message in frames(cycles):
            output.write(struct.pack('>H', len(message)))
            output.write(message)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output')
    parser.add_argument('--cycles', type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.cycles <= 1_000_000:
        parser.error('cycles must be 1..1000000')
    write(args.output, args.cycles)
