# Tests for MidiPlayer.seek() - the click-to-seek progress bar feature.
# Run from the repo root:  python tests\test_seek.py
# Uses the same MockSimulator pattern as test_parser_player.py so no real
# input/game window is needed.

import sys
import os
import time

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


def _events():
    # Sustain goes down at t=1, a zone shift happens at t=2, notes scattered
    # throughout, all well inside a 6s song - long enough that seek() has
    # plenty of "past" state to reconstruct and plenty of "future" left to
    # actually play through.
    return [
        {'time': 0.0, 'type': 'note_on', 'note': 60, 'channel': 0},
        {'time': 0.2, 'type': 'note_off', 'note': 60, 'channel': 0},
        {'time': 1.0, 'type': 'sustain', 'value': True},
        {'time': 1.5, 'type': 'note_on', 'note': 62, 'channel': 0},
        {'time': 2.0, 'type': 'zone', 'value': 1},
        {'time': 2.1, 'type': 'note_off', 'note': 62, 'channel': 0},
        {'time': 4.0, 'type': 'note_on', 'note': 64, 'channel': 0},
        {'time': 4.1, 'type': 'note_off', 'note': 64, 'channel': 0},
        {'time': 6.0, 'type': 'note_on', 'note': 65, 'channel': 0},
    ]


def test_seek_reconstructs_state_while_stopped():
    from player import MidiPlayer
    player = MidiPlayer()
    player.simulator = MockSimulator()
    player.load_events(_events(), active_channels=[0])

    # Seek to t=3.0: sustain (set at t=1) should be on, zone (set at t=2)
    # should be 1, and current_event_idx should land right after the
    # note_off at t=2.1 (index 5), not re-pressing note 62 which was
    # already "sounding" before the seek point.
    player.seek(3.0)

    log = player.simulator.log
    check("release_all fired before replaying state", ('release_all',) in log, f"log={log}")
    check("sustain replayed as True", ('sustain', True) in log, f"log={log}")
    check("zone replayed as 1", ('zone', 1) in log, f"log={log}")
    check("note 62 (already sounding pre-seek) was not re-pressed",
          ('press', 62) not in log, f"log={log}")
    check("current_event_idx lands after the t=2.1 note_off",
          player.current_event_idx == 6, f"got {player.current_event_idx}")

    player.stop()


def test_seek_clamps_range():
    from player import MidiPlayer
    player = MidiPlayer()
    player.simulator = MockSimulator()
    player.load_events(_events(), active_channels=[0])
    total = player.get_total_time()

    player.seek(-5.0)
    check("negative seek clamps to 0", abs(player.get_current_time() - 0.0) < 0.05,
          f"got {player.get_current_time()}")

    player.seek(999.0)
    check("out-of-range seek clamps to total duration",
          abs(player.get_current_time() - total) < 0.05,
          f"got {player.get_current_time()} vs total {total}")
    player.stop()


def test_seek_preserves_paused_state():
    from player import MidiPlayer
    player = MidiPlayer()
    player.simulator = MockSimulator()
    player.load_events(_events(), active_channels=[0])

    player.play()
    time.sleep(0.05)
    player.pause()
    check("setup: player is paused", player.is_paused and not player.is_playing)

    player.seek(3.0)
    check("seeking while paused stays paused",
          player.is_paused and not player.is_playing,
          f"is_paused={player.is_paused} is_playing={player.is_playing}")
    check("get_current_time reflects the new position while still paused",
          abs(player.get_current_time() - 3.0) < 0.05, f"got {player.get_current_time()}")

    # Resuming should continue from the seeked position, not from wherever
    # it was paused originally.
    player.play()
    time.sleep(0.05)
    check("resuming after a paused seek continues from the new position",
          player.get_current_time() >= 3.0, f"got {player.get_current_time()}")
    player.stop()


def test_seek_preserves_playing_state():
    from player import MidiPlayer
    player = MidiPlayer()
    player.simulator = MockSimulator()
    player.load_events(_events(), active_channels=[0])

    player.play()
    time.sleep(0.05)
    check("setup: player is playing", player.is_playing and not player.is_paused)

    player.simulator.log.clear()
    player.seek(5.5)  # just before the final note_on at t=6.0
    check("seeking while playing stays playing",
          player.is_playing and not player.is_paused,
          f"is_playing={player.is_playing} is_paused={player.is_paused}")

    deadline = time.time() + 2.0
    while player.is_playing and time.time() < deadline:
        time.sleep(0.01)

    check("playback continues past the seek point and reaches the final note",
          ('press', 65) in player.simulator.log, f"log={player.simulator.log}")
    player.stop()


def test_seek_noop_without_events():
    from player import MidiPlayer
    player = MidiPlayer()
    player.simulator = MockSimulator()
    player.seek(5.0)  # nothing loaded - must not raise
    check("seeking with no events loaded is a safe no-op", player.current_event_idx == 0)


if __name__ == "__main__":
    test_seek_reconstructs_state_while_stopped()
    test_seek_clamps_range()
    test_seek_preserves_paused_state()
    test_seek_preserves_playing_state()
    test_seek_noop_without_events()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
