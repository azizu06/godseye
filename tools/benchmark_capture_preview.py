"""Measure a local recording's compact preview without uploading or replaying it."""
import argparse
from pathlib import Path
import statistics
import sys
import time
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.rich_capture import decode_rich
from backend.surface_preview import SURFACE_SECTIONS, has_surface, surface_payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path, help='A recorded v2 frame .capture file')
    args = parser.parse_args()
    packet = decode_rich(args.capture.read_bytes())
    if packet.header['kind'] != 'frame' or not has_surface(packet):
        parser.error('Frame must contain tracked, calibrated RGB and raw depth/confidence')
    timings = []
    for index in range(35):
        started = time.perf_counter()
        payload = surface_payload(packet)
        if index >= 5:
            timings.append((time.perf_counter() - started) * 1000)
    preview = decode_rich(payload)
    for name in SURFACE_SECTIONS:
        assert preview.section(name) == packet.section(name), f'{name} changed'
    print(json.dumps(dict(
        original_bytes=len(packet.data), preview_bytes=len(payload),
        reduction_percent=round((1 - len(payload) / len(packet.data)) * 100, 2),
        pack_p50_ms=round(statistics.median(timings), 3),
        pack_p95_ms=round(sorted(timings)[int(len(timings) * .95)], 3),
        render_sections_identical=True), indent=2))


if __name__ == '__main__':
    main()
