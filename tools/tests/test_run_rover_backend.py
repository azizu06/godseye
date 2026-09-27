"""Launcher configuration checks; no server, hardware or provider calls."""
import asyncio
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from backend.app import create_app
from tools import run_rover_backend


class LauncherEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.env_file = self.folder / '.env'
        (self.folder / 'pairing-key').write_text('test-only-pairing-key-0123456789abcdef')
        self.environment = patch.dict(os.environ, {'GODSEYE_DB': ':memory:'}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def launch(self, *extra):
        argv = ['launcher', '--config-dir', str(self.folder), '--prototype',
                '--estimated-length-m', '0.3', '--estimated-width-m', '0.2', *extra]
        with patch('sys.argv', argv), patch('uvicorn.run') as serve, \
                patch('backend.app.create_app', wraps=create_app) as build, redirect_stdout(io.StringIO()):
            run_rover_backend.main()
        app = serve.call_args.args[0]

        async def status():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
                return (await client.get('/voice')).json()['status']
        return asyncio.run(status()), build.call_args.kwargs['weights']

    def write_voice_env(self):
        self.env_file.write_text('GODSEYE_VOICE_ENABLED=1\nELEVENLABS_API_KEY=test-eleven\n'
                                 'GODSEYE_ELEVENLABS_VOICE_ID=TestVoice\nGEMINI_API_KEY=test-gemini\n'
                                 'GODSEYE_GEMINI_MODEL=test-model\nGODSEYE_YOLO_WEIGHTS="file weights.pt"\n')

    def test_env_file_enables_served_voice_and_weights(self):
        self.write_voice_env()
        self.assertEqual(self.launch('--env-file', str(self.env_file)), ('ready', 'file weights.pt'))

    def test_process_environment_and_cli_weights_take_precedence(self):
        self.write_voice_env()
        os.environ.update(GODSEYE_VOICE_ENABLED='0', GODSEYE_YOLO_WEIGHTS='process.pt')
        self.assertEqual(self.launch('--env-file', str(self.env_file)), ('unavailable', 'process.pt'))
        self.assertEqual(self.launch('--env-file', str(self.env_file), '--weights', 'cli.pt'),
                         ('unavailable', 'cli.pt'))

    def test_default_env_is_repository_root_and_missing_file_is_optional(self):
        with patch.object(run_rover_backend, '__file__', str(self.folder / 'tools/run_rover_backend.py')):
            self.assertEqual(self.launch(), ('unavailable', None))
            self.write_voice_env()
            self.assertEqual(self.launch(), ('ready', 'file weights.pt'))

    def test_init_does_not_load_environment_or_start_server(self):
        self.write_voice_env()
        argv = ['launcher', '--init', '--config-dir', str(self.folder), '--env-file', str(self.env_file)]
        with patch('sys.argv', argv), patch('uvicorn.run') as serve, redirect_stdout(io.StringIO()):
            run_rover_backend.main()
        self.assertNotIn('GODSEYE_VOICE_ENABLED', os.environ)
        self.assertFalse(serve.called)
        self.assertTrue((self.folder / 'geometry.json').exists())
        self.assertEqual((self.folder / 'pairing-key').read_text(), 'test-only-pairing-key-0123456789abcdef')
