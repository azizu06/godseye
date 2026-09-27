"""Compile/run real bridge logic on macOS/Linux. No hardware is accessed."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
headers = next((root / '.pio/libdeps').glob('*/ArduinoJson/src'), None)
if headers is None:
    raise SystemExit('Run `pio pkg install -d firmware/elegoo-ble` to fetch the pinned ArduinoJson dependency.')
with tempfile.TemporaryDirectory(prefix='godseye-ble-check-') as folder:
    for name in ('bridge_test', 'autonomy_guard_test', 'autonomy_bridge_test', 'inbox_test'):
        executable = str(Path(folder) / name)
        subprocess.run(['c++', '-std=c++11', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', '-g',
                        '-I', str(headers), '-I', str(root / 'src'),
                        str(root / f'test/{name}.cpp'), '-o', executable], check=True)
        subprocess.run([executable], check=True)
