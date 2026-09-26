"""Isolated real backend and calibrated binary phone fixture for browser tests."""
import base64
import json
from pathlib import Path
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import uvicorn
import numpy as np
from PIL import Image, ImageDraw
import io
from backend.app import create_app
from backend.tests.test_mapping import bundle, grids

listener = socket.socket()
listener.bind(('127.0.0.1', 0))
depth, confidence = grids()
if '--dense' in sys.argv:
    depth = np.full((192, 256), 2., dtype='<f4')
    confidence = np.full(depth.shape, 2, dtype='u1')
color = None
if '--textured' in sys.argv:
    depth = np.full((192, 256), 2., dtype='<f4')
    confidence = np.full(depth.shape, 2, dtype='u1')
    confidence[56:, 155:205] = 0  # Observed opening: never fill this with a wall.
    picture = Image.new('RGB', (80, 60), (180, 165, 145))
    draw = ImageDraw.Draw(picture)
    draw.rectangle((5, 7, 41, 43), fill=(30, 30, 30))
    draw.rectangle((8, 10, 24, 26), fill=(230, 45, 35))
    draw.rectangle((25, 10, 38, 26), fill=(35, 180, 70))
    draw.rectangle((8, 27, 24, 40), fill=(40, 75, 240))
    draw.rectangle((25, 27, 38, 40), fill=(240, 210, 30))
    draw.text((9, 0), 'TOP', fill=(20, 20, 20))
    draw.text((6, 46), 'BOTTOM', fill=(20, 20, 20))
    output = io.BytesIO()
    picture.save(output, format='JPEG', quality=100, subsampling=0)
    color = output.getvalue()
payload = bundle(depth, confidence, jpeg_bytes=color, session='browser-pipeline')
print(json.dumps(dict(port=listener.getsockname()[1],
                     bundle=base64.b64encode(payload).decode())), flush=True)
backend = create_app(':memory:', capture_directory='')


async def app(scope, receive, send):
    if '--legacy' in sys.argv and scope['type'] == 'http' and scope['path'] == '/capture/surface.bin':
        await send({'type': 'http.response.start', 'status': 404,
                    'headers': [(b'access-control-allow-origin', b'*')]})
        await send({'type': 'http.response.body', 'body': b''})
        return
    if '--legacy' in sys.argv and scope['type'] == 'websocket' and scope['path'] == '/live':
        # Match older servers: accept the socket without selecting a subprotocol
        # and publish the original JSON points. Chromium rejects the first dense
        # handshake; the dashboard must reconnect without offering a protocol.
        scope = {**scope, 'subprotocols': []}
    await backend(scope, receive, send)


server = uvicorn.Server(uvicorn.Config(app,
                                     log_level='error', ws_max_size=8388608))
server.run(sockets=[listener])
