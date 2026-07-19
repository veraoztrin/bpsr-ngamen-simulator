# Tests for midi_parser (tempo extraction, event filtering) and an
# end-to-end playback smoke test with a mock input simulator.
# Run from the repo root:  python -m tests.test_parser_player
# Uses a stub 'mido' module so no dependencies are needed.

import sys
import os
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name} {detail}")


# --- stub mido so midi_parser can be imported without the real library -----

class StubMsg:
    def __init__(self, type, time=0.0, **kw):
        self.type = type
        self.time = time
        for k, v in kw.items():
            setattr(self, k, v)

class StubMidiFile:
    script = []  # list of StubMsg, set by each test
    def __init__(self, path):
        self.path = path
    def __iter__(self):
        return iter(StubMidiFile.script)

stub_mido = types.ModuleType("mido")
stub_mido.MidiFile = StubMidiFile
sys.modules['mido'] = stub_mido

import midi_parser  # noqa: E402  (must come after the stub)


def test_parser():
    StubMidiFile.script = [
        StubMsg('set_tempo', time=0.0, tempo=500000),          # 120 BPM
        StubMsg('time_signature', time=0.0, numerator=3, denominator=4),
        StubMsg('note_on', time=0.0, note=60, velocity=64, channel=0),
        StubMsg('note_on', time=1.0, note=60, velocity=0, channel=0),   # vel-0 = off
        StubMsg('control_change', time=0.5, control=64, value=100, channel=0),
        StubMsg('control_change', time=0.5, control=64, value=0, channel=0),
        StubMsg('note_off', time=0.0, note=99, channel=1),
    ]
    parsed = midi_parser.parse_midi_full("fake.mid")
    check("bpm extracted", abs(parsed['bpm'] - 120.0) < 1e-6, f"got {parsed['bpm']}")
    check("time signature extracted", parsed['beats_per_measure'] == 3)

    evs = parsed['events']
    types_seq = [e['type'] for e in evs]
    check("velocity-0 becomes note_off", types_seq.count('note_off') == 2, f"got {types_seq}")
    check("sustain on+off captured",
          [e['value'] for e in evs if e['type'] == 'sustain'] == [True, False])
    check("times accumulate", abs(evs[-1]['time'] - 2.0) < 1e-6, f"got {evs[-1]['time']}")

    # Anti-stack filter: duplicate note_on at the same instant
    StubMidiFile.script = [
        StubMsg('note_on', time=0.0, note=60, velocity=64, channel=0),
        StubMsg('note_on', time=0.0, note=60, velocity=64, channel=0),
        StubMsg('note_on', time=0.001, note=60, velocity=64, channel=0),
    ]
    evs = midi_parser.parse_midi("fake.mid")
    check("stacked duplicates removed", len(evs) == 1, f"got {len(evs)}")

    # Defaults when no tempo/timesig present
    StubMidiFile.script = [StubMsg('note_on', time=0.0, note=60, velocity=64, channel=0)]
    parsed = midi_parser.parse_midi_full("fake.mid")
    check("default bpm 120", parsed['bpm'] == 120.0)
    check("default 4 beats", parsed['beats_per_measure'] == 4)


# --- end-to-end playback with mock simulator -------------------------------

class MockSimulator:
    def __init__(self):
        self.log = []
        self.shift_delay_ms = 0
        self.shift_hold_ms = 0
    def press_note(self, n): self.log.append(('press', n))
    def release_note(self, n): self.log.append(('release', n))
    def set_sustain(self, v): self.log.append(('sustain', v))
    def set_octave_shift(self, z): self.log.append(('zone', z))
    def release_all(self): self.log.append(('release_all',))


def test_playback():
    from player import MidiPlayer
    from arranger import ConversionSettings, convert

    raw = [
        {'time': 0.00, 'type': 'note_on', 'note': 86, 'velocity': 64, 'channel': 0},
        {'time': 0.05, 'type': 'note_off', 'note': 86, 'channel': 0},
        {'time': 0.10, 'type': 'note_on', 'note': 88, 'velocity': 64, 'channel': 0},
        {'time': 0.15, 'type': 'note_off', 'note': 88, 'channel': 0},
    ]
    events = convert(raw, ConversionSettings(phrase_gap_shifting=True, speed=4.0), orig_bpm=120)

    player = MidiPlayer()
    player.simulator = MockSimulator()
    player.load_events(events, active_channels=[0])
    player.play()
    deadline = time.time() + 3.0
    while player.is_playing and time.time() < deadline:
        time.sleep(0.01)
    player.stop()

    log = player.simulator.log
    check("zone hint fired before first press",
          ('zone', 1) in log and log.index(('zone', 1)) < log.index(('press', 86)),
          f"log={log}")
    presses = [e for e in log if e[0] == 'press']
    releases = [e for e in log if e[0] == 'release']
    check("both notes pressed", [p[1] for p in presses] == [86, 88], f"got {presses}")
    check("both notes released", len(releases) == 2, f"got {releases}")
    check("release_all on finish", ('release_all',) in log)


if __name__ == "__main__":
    test_parser()
    test_playback()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
