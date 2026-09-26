"""Compact wall rectangles from classified ARKit planes; no guessed surfaces."""
import math

import numpy as np

MAX_WALLS = 128
WALL_DISTANCE = .02


def non_wall_mask(points, walls):
    """Keep borders and objects farther than 2 cm from the classified plane."""
    keep = np.ones(len(points), dtype=bool)
    for wall in walls:
        corners = np.asarray(wall['corners']).reshape(4, 3)
        u, v = corners[1] - corners[0], corners[3] - corners[0]
        width, height = np.linalg.norm(u), np.linalg.norm(v)
        u, v = u / width, v / height
        normal = np.cross(u, v)
        relative = points - corners[0]
        across, up = relative @ u, relative @ v
        on_wall = ((np.abs(relative @ normal) <= WALL_DISTANCE) &
                   (across >= WALL_DISTANCE) & (across <= width - WALL_DISTANCE) &
                   (up >= WALL_DISTANCE) & (up <= height - WALL_DISTANCE))
        keep &= ~on_wall
    return keep


def vector(value, size):
    if (not isinstance(value, list) or len(value) != size or
            any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1e6 for v in value)):
        raise ValueError('invalid plane coordinates')
    return np.array(value, dtype=float)


def wall_rectangles(metadata):
    """Plane-local XZ extent -> extent Y rotation -> center -> anchor-to-world.

    ARKit planeExtent does not rotate the anchor transform on iOS 16+. Apply
    rotationOnYAxis to the extent itself, never to the anchor-local center.
    """
    anchors = metadata.get('anchors', [])
    if not isinstance(anchors, list):
        return []
    walls, seen = [], set()
    for anchor in anchors[:8192]:
        if not isinstance(anchor, dict) or anchor.get('type') != 'plane':
            continue
        classified = anchor.get('is_wall', anchor.get('classification') == 'wall')
        if classified is not True or anchor.get('alignment') != 1:
            continue
        try:
            identifier = anchor['id']
            if not isinstance(identifier, str) or not 0 < len(identifier) <= 128 or identifier in seen:
                continue
            center = vector(anchor['center'], 3)
            transform = vector(anchor['transform'], 16).reshape(4, 4, order='F')
            rotation = transform[:3, :3]
            if (not np.allclose(transform[3], [0, 0, 0, 1], atol=1e-5) or
                    not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-4) or
                    not np.isclose(np.linalg.det(rotation), 1, atol=1e-4) or
                    abs(transform[1, 1]) > .2):  # Anchor +Y is plane normal; walls are vertical.
                continue
            extent = anchor['extent']
            width, height, angle = (extent[k] for k in ('width', 'height', 'rotation_y_rad'))
            if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (width, height, angle)) or
                    not .05 <= width <= 100 or not .05 <= height <= 100 or abs(angle) > 2 * math.pi):
                continue
            c, s = math.cos(angle), math.sin(angle)
            extent_rotation = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
            corners = np.array([[-width/2, 0, -height/2], [width/2, 0, -height/2],
                                [width/2, 0, height/2], [-width/2, 0, height/2]])
            local = corners @ extent_rotation.T + center
            world = local @ rotation.T + transform[:3, 3]
            if np.max(np.abs(world)) > 1e6:
                continue
            walls.append(dict(id=identifier, corners=np.round(world, 4).ravel().tolist()))
            seen.add(identifier)
            if len(walls) == MAX_WALLS:
                break
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return walls
