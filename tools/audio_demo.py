"""Synthetic stored rescan + offline tone playback, bound to loopback. No ElevenLabs calls."""
import math
from pathlib import Path
import sqlite3
import struct
import tempfile

from backend.app import create_app
from backend.changes import ChangeTracker
from backend.localization import Detection, LocalizedDetection
from backend.objects import ObjectMemory


class DemoToneProvider:
    identity = 'offline-demo-tone-v1'
    async def synthesize(self, text):
        # Deliberately a tone, not a claim of natural speech or sponsor API usage.
        return b''.join(struct.pack('<h', int(4000 * math.sin(2 * math.pi * 440 * i / 16000)))
                        for i in range(8000))


def seed(path):
    db = sqlite3.connect(path)
    db.executescript(Path('backend/schema.sql').read_text())
    session = ('synthetic-audio-demo', 1)
    db.execute('INSERT INTO sessions VALUES(?,?,0,NULL,\'{}\',\'{}\')', session)
    memory = ObjectMemory(db)
    changes = ChangeTracker(db, memory)
    changes.activate(session)
    refs = [('cup', (0., 0., 0.)), ('chair', (2., 0., 0.))]
    for frame in range(1, 7):
        if frame == 4:
            changes.start(session, 3000)
        position = (1., 0., 1.) if frame <= 3 else (1., 0., 3.)
        db.execute("INSERT INTO frames(session_id,map_epoch,frame_id,t_capture,t_wall_ms,transform_json,tracking) "
                   "VALUES(?,?,?,?,?,'[]','normal')", (*session, frame, float(frame), frame * 1000))
        found = [LocalizedDetection(Detection((0, 0, 1, 1), name, .9), pos, 2., 6,
                                    *session, frame, float(frame)) for name, pos in refs + [('backpack', position)]]
        sightings = memory.record(session, frame, float(frame), found)
        watch = changes.watching
        views = None if watch is None else (watch[0], tuple((oid, 'clear') for oid, _ in watch[1]
            if changes.rescan.baseline[oid].class_name == 'backpack'))
        changes.observe(float(frame), float(frame), sightings, views)
    db.commit()
    db.close()


def main():
    import uvicorn
    with tempfile.TemporaryDirectory(prefix='godseye-audio-') as folder:
        path = str(Path(folder) / 'scene.db')
        seed(path)
        print('OFFLINE SYNTHETIC TONE DEMO: GET /events, POST /events/{id}/audio, GET returned audio_url.')
        print('Dashboard: external backend http://127.0.0.1:8876; enable API actions; Recent Activity.')
        uvicorn.run(create_app(path, audio_provider=DemoToneProvider()), host='127.0.0.1', port=8876)


if __name__ == '__main__':
    main()
