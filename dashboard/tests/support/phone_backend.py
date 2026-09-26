"""Isolated real backend and calibrated binary phone fixture for browser tests."""
import base64
import json
from pathlib import Path
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import uvicorn
from backend.app import create_app
from backend.tests.test_mapping import bundle, grids

listener = socket.socket()
listener.bind(('127.0.0.1', 0))
payload = bundle(*grids(), session='browser-pipeline')
print(json.dumps(dict(port=listener.getsockname()[1],
                     bundle=base64.b64encode(payload).decode())), flush=True)
server = uvicorn.Server(uvicorn.Config(create_app(':memory:', capture_directory=''),
                                     log_level='error', ws_max_size=8388608))
server.run(sockets=[listener])
