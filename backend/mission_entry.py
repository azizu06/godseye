"""Insert-once, map-scoped measured Explore entry; unrelated session config is retained."""
import json
import math


def valid_entry(value, session):
    if not isinstance(value, dict) or session is None:
        return None
    if (value.get('session_id'), value.get('map_epoch')) != session or value.get('basis') != 'explore_start':
        return None
    start = value.get('start')
    if not isinstance(start, list) or len(start) != 2:
        return None
    capture = value.get('t_capture')
    try:
        for number in [*start, capture]:
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                return None
    except OverflowError:
        return None
    if capture < 0:
        return None
    for key in ('map_epoch', 'frame_id', 'started_at_ms'):
        number = value.get(key)
        if type(number) is not int or number < 0:
            return None
    return dict(session_id=session[0], map_epoch=session[1], start=list(start),
                frame_id=value['frame_id'], t_capture=capture,
                started_at_ms=value['started_at_ms'], basis='explore_start')


def _configuration(db, session):
    if session is None:
        return None
    row = db.execute('SELECT configuration_json FROM sessions WHERE session_id=? AND map_epoch=?',
                     session).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except (ValueError, TypeError, RecursionError):
        return None
    return (row[0], value) if isinstance(value, dict) else None


def load_entry(db, session):
    config = _configuration(db, session)
    return None if config is None else valid_entry(config[1].get('mission_entry'), session)


def record_entry(db, session, entry):
    """First valid entry wins; corrupt/existing metadata is never silently replaced.

    The compare-and-update protects unrelated configuration even if another writer
    changes it between reading and persisting. No retry may substitute a later pose.
    """
    config = _configuration(db, session)
    validated = valid_entry(entry, session)
    if config is None or (entry is not None and validated is None):
        return None
    entry = validated
    previous, values = config
    if 'mission_entry' in values:
        return valid_entry(values['mission_entry'], session)
    values['mission_entry'] = entry
    try:
        encoded = json.dumps(values, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return None
    with db:
        changed = db.execute(
            'UPDATE sessions SET configuration_json=? WHERE session_id=? AND map_epoch=? AND configuration_json=?',
            (encoded, *session, previous)).rowcount
    return entry if changed else load_entry(db, session)
