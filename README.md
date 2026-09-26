# GodsEye

A ShellHacks indoor mapping rover prototype, designed around iPhone sensing, a Mac backend, a browser dashboard, and car integration.

The repository currently contains a backend skeleton, standalone detection/localization modules, and synthetic development sources. The iOS app, dashboard, and firmware are still placeholders; detection/localization is not wired into the backend app. Mapping, navigation, and hardware control remain future work. The drive adapter only logs, and the rover cannot arm or drive.

## Local quick start

Use Python 3.10+ and run from the repository root. This demo needs no phone, car, credentials, or model weights.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements-test.txt -r tools/requirements.txt numpy
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8765 --ws-max-size 8388608
```

In another terminal, send synthetic phone data:

```sh
.venv/bin/python tools/fake_phone.py --url ws://127.0.0.1:8765/phone
```

Inspect health at <http://127.0.0.1:8765/health> or REST schemas at <http://127.0.0.1:8765/docs>. The backend reports phone health/pose; object and path snapshots are empty. It has no authentication, so keep this demo local. Stop each process with Ctrl-C.

Run the hardware-free tests:

```sh
.venv/bin/python -m unittest discover -s backend/tests
.venv/bin/python -m unittest discover -s tools/tests
```

## Authoritative docs

- [Frozen wire and coordinate contract](docs/INTERFACES.md)
- [Backend setup, implemented routes, and safety limits](backend/README.md)
- [Detection/localization integration and accuracy tests](backend/DETECTION.md)
- [Synthetic phone/live sources and offline validation](tools/README.md)

The interface contract describes intended integration; the backend and tools docs describe what works today.
