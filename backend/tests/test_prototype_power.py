"""All prototype directions obey the operator ceiling; no physical actuation."""
import unittest
from backend.prototype import PrototypeActuation
from backend.rover_relay import RelayCar
from backend.motion import DriveCommand


class PrototypePowerTests(unittest.TestCase):
    def test_every_relay_move_obeys_power_cap_including_forward_arcs(self):
        for ceiling, variable in ((cap, var) for cap in (30, 60, 120, 180) for var in (False, True)):
            with self.subTest(ceiling=ceiling, variable=variable):
                profile = PrototypeActuation(max_pwm=ceiling, variable_arc_pwm=variable)
                car = RelayCar('TEST_KEY_NOT_REAL_01234567890123456789', profile, clock=lambda: 10.)
                car.identity = lambda: ('capture', 1)
                car.attach()
                car.control = None  # fake completed Stop handshake, before injecting an armed session
                session = 'a' * 32
                car.armed_session = session.upper()
                seq = 0
                for v in (0., .05, .15, .2):
                    for w in (-.5, -.15, 0., .15, .5):
                        seq += 1
                        car.receive(dict(version=1, type='status', seq=seq, session_id='capture', map_epoch=1,
                                         permit=f'{seq:016X}', uno_age_ms=100., enabled=True))
                        car.send(DriveCommand(session_id=session, seq=seq, v_mps=v, yaw_rate_rps=w,
                                              issued_at_ms=10000, valid_for_ms=250))
                        packet = car.next_message()
                        self.assertLessEqual(packet['power'], ceiling)
                        self.assertGreaterEqual(packet['power'], 0)
                        self.assertEqual(packet['lease_ms'], 1500)
                        if not variable:
                            self.assertNotIn('inner_power', packet)
                        elif 'inner_power' in packet:
                            self.assertLessEqual(packet['inner_power'], ceiling)
                        if v == w == 0:
                            self.assertEqual((packet['direction'], packet['power']), (0, 0))
                        elif v and abs(w) >= .15:
                            self.assertEqual(packet['direction'], 5 if w > 0 else 6)

    def test_invalid_cap_is_rejected_and_default_profile_remains_compatible(self):
        for cap in (0, -1, 181, 60.5, True):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                PrototypeActuation(max_pwm=cap)
        self.assertEqual(PrototypeActuation().command(.2, .5).pwm, 180)

    def test_cli_wires_the_all_direction_ceiling_without_starting_a_server(self):
        from contextlib import redirect_stdout
        import io
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from tools.run_rover_backend import main
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'pairing-key').write_text('TEST_KEY_NOT_REAL_01234567890123456789')
            args = ['run_rover_backend', '--config-dir', directory, '--prototype',
                    '--estimated-length-m', '.24', '--estimated-width-m', '.14', '--prototype-max-pwm', '60']
            with patch('sys.argv', args), patch('backend.app.create_app') as create, patch('uvicorn.run') as run, redirect_stdout(io.StringIO()):
                main()
                profile = create.call_args.kwargs['car'].actuation
                self.assertEqual(profile.max_pwm, 60)
                self.assertEqual(profile.command(.2, .5).pwm, 60)
                run.assert_called_once()  # mocked: no port, backend, or real adapter was started
