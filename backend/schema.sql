PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT NOT NULL, map_epoch INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL, calibration_version TEXT,
    configuration_json TEXT NOT NULL DEFAULT '{}', software_versions_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (session_id, map_epoch)
);
CREATE TABLE IF NOT EXISTS frames (
    session_id TEXT NOT NULL, map_epoch INTEGER NOT NULL, frame_id INTEGER NOT NULL,
    t_capture REAL NOT NULL, t_wall_ms INTEGER NOT NULL, transform_json TEXT NOT NULL,
    tracking TEXT NOT NULL, intrinsics_json TEXT, payload_refs_json TEXT,
    PRIMARY KEY (session_id, map_epoch, frame_id),
    FOREIGN KEY (session_id, map_epoch) REFERENCES sessions
);
CREATE TABLE IF NOT EXISTS objects (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, map_epoch INTEGER NOT NULL,
    class TEXT NOT NULL, position_json TEXT, uncertainty_json TEXT, identity_confidence REAL,
    first_seen REAL, last_seen REAL, observations INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL CHECK (state IN ('present','last_seen','moved','not_found_on_rescan')),
    FOREIGN KEY (session_id, map_epoch) REFERENCES sessions
);
CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, map_epoch INTEGER NOT NULL, frame_id INTEGER NOT NULL,
    class TEXT NOT NULL, box_mask_json TEXT, position_json TEXT, uncertainty_json TEXT,
    detector_confidence REAL, object_id TEXT REFERENCES objects(id),
    FOREIGN KEY (session_id, map_epoch, frame_id) REFERENCES frames
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, object_id TEXT NOT NULL REFERENCES objects(id),
    kind TEXT NOT NULL CHECK (kind IN ('new','moved','possible_move','not_found')),
    baseline_observation_id TEXT REFERENCES observations(id), new_observation_id TEXT REFERENCES observations(id),
    old_position_json TEXT, new_position_json TEXT, displacement_m REAL, confidence REAL, t REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS health_events (
    id INTEGER PRIMARY KEY, t_wall_ms INTEGER NOT NULL, session_id TEXT, map_epoch INTEGER,
    component TEXT NOT NULL, reason TEXT NOT NULL, mode TEXT NOT NULL, armed INTEGER NOT NULL,
    FOREIGN KEY (session_id, map_epoch) REFERENCES sessions
);
CREATE TABLE IF NOT EXISTS rescans (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, map_epoch INTEGER NOT NULL,
    started_at_ms INTEGER NOT NULL, after_t_capture REAL NOT NULL, baseline_json TEXT NOT NULL,
    FOREIGN KEY (session_id, map_epoch) REFERENCES sessions
);
CREATE TABLE IF NOT EXISTS rescan_events (
    rescan_id TEXT NOT NULL REFERENCES rescans(id), event_id TEXT NOT NULL UNIQUE REFERENCES events(id),
    kind TEXT NOT NULL, object_id TEXT NOT NULL,
    PRIMARY KEY (rescan_id, kind, object_id)
);
