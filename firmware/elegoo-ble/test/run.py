"""Compile/run real bridge logic on macOS/Linux. No hardware is accessed."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
headers = next((root / '.pio/libdeps').glob('*/ArduinoJson/src'), None)
if headers is None:
    raise SystemExit('Run `pio pkg install -d firmware/elegoo-ble` to fetch the pinned ArduinoJson dependency.')
with tempfile.TemporaryDirectory(prefix='godseye-ble-check-') as folder:
    executable = str(Path(folder) / 'bridge-check')
    subprocess.run(['c++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-g',
                    '-I', str(headers), '-I', str(root / 'src'),
                    str(root / 'test/bridge_test.cpp'), '-o', executable], check=True)
    subprocess.run([executable], check=True)
