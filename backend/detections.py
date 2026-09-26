"""Live 2D detection overlay: what the detector saw in one accepted phone frame.

Boxes are JPEG pixels of that exact frame. A position appears only when the same
capture's depth localized the box (`localize_all`); a box without reliable depth
stays 2D evidence. Nothing here fabricates a 3D point or a detection.
"""
import os

# Staged responder demo classes, all emitted by the stock COCO YOLO11 weights.
DEMO_CLASSES = ('person', 'backpack', 'chair', 'bottle')
MAX_OVERLAY = 32  # boxes per message, highest confidence first


def overlay_classes(requested, model_names=None) -> tuple[str, ...]:
    """The requested classes the configured model can emit; all of them when it cannot say."""
    if model_names is None:
        return tuple(requested)
    names = set(model_names)
    return tuple(name for name in requested if name in names)


def classes_from_env() -> tuple[str, ...]:
    """`GODSEYE_OVERLAY_CLASSES` (comma separated) or the staged demo set."""
    raw = os.environ.get('GODSEYE_OVERLAY_CLASSES')
    if not raw:
        return DEMO_CLASSES
    return tuple(dict.fromkeys(name.strip() for name in raw.split(',') if name.strip()))


def detections_message(result, sightings, classes) -> dict:
    """One `detections` /live message for a `FrameObjects` result and its stored sightings.

    `sightings` is `ObjectMemory.record`'s output, one per `result.found` in order.
    """
    placed = {id(item.detection): (item, sighting) for item, sighting in zip(result.found, sightings)}
    entries = []
    for box in sorted(result.boxes, key=lambda b: -b.confidence):
        if box.class_name not in classes:
            continue
        item, sighting = placed.get(id(box), (None, None))
        entries.append({
            'class': box.class_name,
            'confidence': round(box.confidence, 3),
            'box': [round(v, 1) for v in box.box],
            'position': None if item is None else [round(v, 3) for v in item.position],
            'depth_m': None if item is None else round(item.depth_m, 3),
            'object_id': None if sighting is None else sighting.object_id,
        })
    width, height = result.image_size
    return dict(version=1, type='detections', session_id=result.session_id, map_epoch=result.map_epoch,
                frame_id=result.frame_id, t_capture=result.t_capture, t_wall_ms=result.t_wall_ms,
                image=dict(width=width, height=height), source='backend_detector',
                classes=list(classes), detections=entries[:MAX_OVERLAY])
