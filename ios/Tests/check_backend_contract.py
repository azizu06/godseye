"""Compile Swift tests, then decode their actual binary output with the Python backend."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np
from backend.app import decode_frame
from backend.frame_bundle import parse_frame_bundle
from backend.rich_capture import decode_rich


def main():
    with tempfile.TemporaryDirectory(prefix="godseye-swift-") as directory:
        output = Path(directory) / "frame.bin"
        env = dict(os.environ, GODSEYE_WIRE_FIXTURE=str(output))
        subprocess.run(["xcrun", "swift", "test", "--package-path", str(ROOT / "ios")], env=env, check=True)
        payload = output.read_bytes()
        pose = json.loads(Path(str(output) + ".pose.json").read_text())
        transport = decode_frame(payload)
        frame = parse_frame_bundle(payload, session_id="swift-contract-fixture", map_epoch=3, pose=pose)
        assert transport.frame_id == frame.frame_id == 17
        assert frame.image.size == (4, 3)
        np.testing.assert_array_equal(frame.depth, np.arange(1, 13).reshape(3, 4))
        np.testing.assert_array_equal(frame.confidence, [[0, 1, 2, 2], [2, 2, 1, 0], [2, 2, 2, 2]])
        np.testing.assert_array_equal(frame.transform[:3, 3], [2, 1, -3])
        np.testing.assert_array_equal(frame.intrinsics, [[3, 0, 2], [0, 3, 1.5], [0, 0, 1]])
        print("Swift binary fixture accepted by both backend decoders; calibration, pixels, and identity match.")
        capture = decode_rich(Path(str(output) + '.capture').read_bytes())
        assert capture.header['session_id'] == 'swift-contract-fixture'
        assert capture.header['map_epoch'] == 3 and capture.header['frame_id'] == 18
        assert capture.header['metadata']['landmarks'] == [[None, 0.25]]
        assert capture.header['metadata']['motion']['timestamp'] == 12.69
        values = np.frombuffer(capture.section('raw_depth'), dtype='<f4')
        assert values[0] == 1 and np.isnan(values[1])
        assert capture.section('rgb').startswith(b'\xff\xd8')
        print('Full capture v2 fixture accepted: native JPEG, lossless depth including NaN, timestamps and missing landmarks match.')


if __name__ == "__main__":
    main()
