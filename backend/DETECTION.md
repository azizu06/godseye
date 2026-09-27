# Detection and localization integration

`docs/INTERFACES.md` is the wire/coordinate authority. These modules do not own
FastAPI routes, database records, object IDs, association, or vehicle commands;
`app.py` and `objects.py` wire them into `/phone`, SQLite and `/live` (see
README "Live objects").

```python
from backend.frame_bundle import parse_frame_bundle, FrameValidationError
from backend.detector import MPSDetector

# Once per detector worker; explicit existing weights prevent downloads.
detector = MPSDetector('~/.venvs/godseye/yolo11n.pt')
# Use the active hello's identity, never values taken from the incoming bundle.
frame = parse_frame_bundle(message, session_id=active_session_id, map_epoch=active_epoch)
# Run in a worker thread, not directly inside an async WebSocket handler.
observations = detector.localize(frame)
```

`FrameValidationError` rejects malformed bundles, wrong sessions/epochs, unsupported
versions/orientation, inconsistent lengths/dimensions, bad JPEGs, invalid matrices,
and non-normal tracking. An optional `pose=` reference must match frame ID, capture
time, session, epoch, tracking and transform. Without an external reference, the
sender's same-ARFrame guarantee cannot be independently verified; the bundle's
transform is used, never a separately cached latest pose. Routing must recheck the
active session/epoch when consuming completed inference, since it can reset while
inference is running; `app.py` does, and drops results after disconnect or reset.

`localize_detection` is the pure geometry seam. It selects depth sample centers
inside JPEG xyxy edges using independent width/height scales, takes the median of
finite positive depth with confidence **2**, and projects the JPEG box-center ray.
Defaults require at least three usable samples and 25% usable box coverage; callers
can tune these thresholds. Invalid or low-confidence boxes raise
`LocalizationError`; `localize_all` (used by the detector adapter) skips them. The result is a representative
surface point, not a physical object center. Depth is optical-axis Z in meters.
Optical `(x,y,z)` becomes ARKit camera `(x,-y,-z)` before camera-to-world projection.
No screen rotation or second scaling of the already JPEG-scaled intrinsics occurs.

Install dependencies into the existing environment if needed:
`numpy`, `Pillow`, `ultralytics`, `torch` (the supplied environment already has them).
Pure tests use only numpy/Pillow and never import torch/Ultralytics:

```sh
$HOME/.venvs/godseye/bin/python -m unittest discover -s backend/tests -p 'test_localization.py' -v
```

The accuracy fixture uses a 90-degree camera rotation, nonzero translation, unequal
JPEG/depth sizes, low-confidence background and a high-confidence depth outlier.
The independent expected world point is `(-1, 1.5, 2)` meters, with error < 0.05 m.

## Indoor class filter

With real YOLO weights the detector keeps only COCO classes plausible inside a school
building (`backend/detector.py` `INDOOR_CLASSES`: people, bags, furniture, electronics,
books, bottles/cups, kitchen fixtures). Animals, food, vehicles and outdoor fixtures are
dropped before localization, so they never become stored objects, overlay boxes or voice
grounding. Stored objects of other classes (recorded earlier) are hidden from snapshots.
`GODSEYE_DETECTOR_CLASSES` overrides the set (comma list) or `all` disables the filter.
Injected test detectors keep every class.
