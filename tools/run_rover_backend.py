"""Run the paired iPhone rover backend with local, measured calibration files.

--init creates an unguessable local key and ALL-NULL measurement templates, then
exits. Nothing here commands motion. The ordinary backend still defaults to logging.
"""
import argparse
import os
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', type=Path, default=Path.home() / '.local/share/godseye/autonomy')
    parser.add_argument('--init', action='store_true')
    parser.add_argument('--prototype', action='store_true', help='Uncalibrated prototype; never auto-arms')
    parser.add_argument('--estimated-length-m', type=float)
    parser.add_argument('--estimated-width-m', type=float)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--weights', default=os.environ.get('GODSEYE_YOLO_WEIGHTS'))
    args = parser.parse_args()
    folder = args.config_dir.expanduser().resolve()
    if args.init:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        templates = Path(__file__).resolve().parents[1] / 'docs/calibration'
        values = [('pairing-key', secrets.token_urlsafe(32)),
                  ('geometry.json', (templates / 'rover-geometry.example.json').read_text()),
                  ('actuation.json', (templates / 'rover-actuation.example.json').read_text())]
        for name, value in values:
            try:
                descriptor = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                print(f'Preserved existing {name}')
                continue
            with os.fdopen(descriptor, 'w') as out: out.write(value + '\n')
            print(f'Created {name}')
        print(f'Local configuration: {folder}. Fill only measured values; null fields prevent driving.')
        return
    from backend.actuation import load_actuation
    from backend.calibration import load_calibration
    from backend.rover_relay import RelayCar
    from backend.app import create_app
    import uvicorn
    if args.prototype:
        if args.estimated_length_m is None or args.estimated_width_m is None:
            parser.error('--prototype requires explicit estimated length and width in meters')
        from backend.prototype import PrototypeActuation, prototype_geometry
        actuation = PrototypeActuation()
        geometry = prototype_geometry(args.estimated_length_m, args.estimated_width_m)
        print('UNCALIBRATED PROTOTYPE: PWM 60–180 proportional forward/arc power, PWM 60 pivot; actual speed unmeasured.', flush=True)
    else:
        actuation = load_actuation(folder / 'actuation.json')
        geometry = load_calibration(folder / 'geometry.json')
    car = RelayCar((folder / 'pairing-key').read_text().strip(), actuation)
    app = create_app(car=car, calibration=geometry, weights=args.weights)
    uvicorn.run(app, host=args.host, port=args.port, ws_max_size=8388608)


if __name__ == '__main__': main()
