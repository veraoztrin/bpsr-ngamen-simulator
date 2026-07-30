# Regression tests for repeated notes collapsing into one long held note.
#
# The app performs by holding and releasing physical keys, so two hits on the
# same key only read as two notes if the game actually samples the key as UP
# in between. These tests pin down the two ways that used to fail:
#   1. quantised MIDI puts a note_off on the exact timestamp of the next
#      note_on, leaving zero key-up time;
#   2. release_note() re-derived the key from the CURRENT octave modifier, so
#      a zone change mid-note released the wrong key and stranded the real one;
#   3. changing Shift/Ctrl left already-held keys on their old physical row,
#      so a held C5 on Q turned into C6 instead of moving to A under Shift.
#
# Run from the repo root:  python -m tests.test_retrigger
# Pure-Python, no dependencies needed.

import sys
import os
import types
import ctypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arranger import (
    ConversionSettings, convert, convert_drum, enforce_retrigger_gaps,
    MIN_NOTE_LEN, DRUM_HIT_LEN,
)

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


def on(t, note, vel=64, ch=0):
    return {'time': t, 'type': 'note_on', 'note': note, 'velocity': vel, 'channel': ch}

def off(t, note, ch=0):
    return {'time': t, 'type': 'note_off', 'note': note, 'channel': ch}


def key_up_gaps(evs, note, ch=0):
    """Key-up durations between consecutive hits of one pitch, in seconds."""
    ons = sorted(e['time'] for e in evs
                 if e['type'] == 'note_on' and e['note'] == note and e['channel'] == ch)
    offs = sorted(e['time'] for e in evs
                  if e['type'] == 'note_off' and e['note'] == note and e['channel'] == ch)
    return [o - f for f, o in zip(offs, ons[1:])]


# --- install a fake windll so input_simulator imports off Windows -----------

ctypes.windll = types.SimpleNamespace(
    user32=types.SimpleNamespace(SendInput=lambda *a: 1,
                                 MapVirtualKeyW=lambda a, b: a))

import input_simulator as isim  # noqa: E402  (must follow the windll stub)
from config import KEY_MAP, VK_LSHIFT, VK_LCONTROL  # noqa: E402

VK_NAME = {v: k for k, v in KEY_MAP.items()}
VK_NAME[VK_LSHIFT] = 'LSHIFT'
VK_NAME[VK_LCONTROL] = 'LCTRL'


def make_sim(gap_ms=0):
    """A simulator that logs key events instead of sending them.

    gap_ms defaults to 0 so the timing backstop doesn't make the tests sleep;
    the arranger-level gap is asserted separately above.
    """
    log = []
    isim.press_key = lambda vk: log.append(('DOWN', VK_NAME.get(vk, hex(vk))))
    isim.release_key = lambda vk: log.append(('UP', VK_NAME.get(vk, hex(vk))))
    sim = isim.BPSRInputSimulator()
    sim.focus_guard_enabled = False
    sim.retrigger_gap_ms = gap_ms
    sim.shift_delay_ms = 0
    sim.shift_hold_ms = 0
    return sim, log


# --- arranger: gaps get carved out ----------------------------------------

def test_gapless_repeats_get_key_up_time():
    print("[gapless quantised repeats]")
    # Four back-to-back quarter notes: each note_off lands exactly on the next
    # note_on. This is the shape that used to play as one long note.
    evs = []
    for i in range(4):
        evs.append(on(i * 0.5, 60))
        evs.append(off((i + 1) * 0.5, 60))
    out = convert(evs, ConversionSettings(), orig_bpm=120)

    gaps = key_up_gaps(out, 60)
    check("all three retriggers get key-up time", len(gaps) == 3, f"got {len(gaps)}")
    check("each gap is the configured 25ms", all(abs(g - 0.025) < 1e-9 for g in gaps),
          f"got {gaps}")
    check("onsets are untouched",
          [e['time'] for e in out if e['type'] == 'note_on'] == [0.0, 0.5, 1.0, 1.5])
    check("the final note keeps its full length",
          any(abs(e['time'] - 2.0) < 1e-9 for e in out if e['type'] == 'note_off'))


def test_gap_is_configurable_and_disablable():
    print("[gap setting]")
    evs = [on(0.0, 60), off(0.5, 60), on(0.5, 60), off(1.0, 60)]
    wide = convert(evs, ConversionSettings(retrigger_gap=0.060), orig_bpm=120)
    check("a wider gap is honoured", abs(key_up_gaps(wide, 60)[0] - 0.060) < 1e-9,
          f"got {key_up_gaps(wide, 60)}")
    zero = convert(evs, ConversionSettings(retrigger_gap=0.0), orig_bpm=120)
    check("zero disables the pass entirely", abs(key_up_gaps(zero, 60)[0]) < 1e-9,
          f"got {key_up_gaps(zero, 60)}")


def test_overlapping_same_pitch_is_separated():
    print("[same pitch overlapping itself]")
    # A long C4 with a second C4 struck on top of it - one physical key, so the
    # first has to let go before the second can be heard as a new hit.
    evs = [on(0.0, 60), off(1.0, 60), on(0.4, 60), off(0.8, 60)]
    out = convert(evs, ConversionSettings(), orig_bpm=120)
    gaps = key_up_gaps(out, 60)
    check("overlap resolved into a real gap", gaps and gaps[0] >= 0.025 - 1e-9,
          f"got {gaps}")


def test_faster_than_the_gap_stays_pressable():
    print("[repeats faster than the gap]")
    # 10ms apart - closer than the 25ms gap. The note must still be long enough
    # to register as a press rather than collapsing to zero length.
    notes = [{'start': i * 0.010, 'end': i * 0.010 + 0.008, 'note': 60,
              'velocity': 64, 'channel': 0} for i in range(5)]
    enforce_retrigger_gaps(notes, 0.025)
    check("no note is inverted or zero-length",
          all(n['end'] > n['start'] for n in notes))
    # Every note that got shortened (all but the last, which has no successor
    # to make room for) must still be long enough to register as a press.
    check("each shortened note keeps the minimum audible length",
          all(n['end'] - n['start'] >= MIN_NOTE_LEN - 1e-9 for n in notes[:-1]),
          f"got {[round(n['end'] - n['start'], 4) for n in notes]}")
    check("the pass only ever shortens, never lengthens",
          abs(notes[-1]['end'] - notes[-1]['start'] - 0.008) < 1e-9,
          f"got {notes[-1]['end'] - notes[-1]['start']}")


def test_other_pitches_and_channels_untouched():
    print("[scope of the pass]")
    evs = [on(0.0, 60), off(0.5, 60), on(0.5, 62), off(1.0, 62)]
    out = convert(evs, ConversionSettings(), orig_bpm=120)
    check("a different pitch keeps its full length",
          any(abs(e['time'] - 0.5) < 1e-9 for e in out
              if e['type'] == 'note_off' and e['note'] == 60))

    # Same pitch on two channels = two different performers in duet/auto-split.
    notes = [{'start': 0.0, 'end': 0.5, 'note': 60, 'velocity': 64, 'channel': 0},
             {'start': 0.5, 'end': 1.0, 'note': 60, 'velocity': 64, 'channel': 1}]
    enforce_retrigger_gaps(notes, 0.025)
    check("cross-channel same pitch is left alone", abs(notes[0]['end'] - 0.5) < 1e-9,
          f"got {notes[0]['end']}")


def _drum_run(spacing):
    """Convert a GM percussion track hammering the kick at a fixed spacing."""
    evs = []
    for i in range(6):
        evs.append(on(i * spacing, 36, ch=9))
        evs.append(off(i * spacing + spacing * 0.9, 36, ch=9))
    out = convert_drum(evs, ConversionSettings(), orig_bpm=120)
    return out, out[0]['note']


def test_drum_hits_get_key_up_time():
    print("[drum retriggers]")
    out, voice = _drum_run(0.25)
    gaps = key_up_gaps(out, voice)
    check("kick retriggers are spaced", gaps and all(g >= 0.025 - 1e-9 for g in gaps),
          f"got {gaps}")
    check("hits still land at the original times",
          [round(e['time'], 3) for e in out if e['type'] == 'note_on']
          == [round(i * 0.25, 3) for i in range(6)])
    durations = [b - a for a, b in zip(
        sorted(e['time'] for e in out if e['type'] == 'note_on'),
        sorted(e['time'] for e in out if e['type'] == 'note_off'))]
    check("unhurried taps keep the full hit length",
          all(abs(d - DRUM_HIT_LEN) < 1e-9 for d in durations), f"got {durations}")


def test_dense_drum_taps_are_trimmed_for_the_gap():
    print("[drum retriggers, fast]")
    # 70ms apart: closer than DRUM_HIT_LEN, so each tap has to be cut short to
    # leave the key up before the next hit. This used to leave only 5ms.
    out, voice = _drum_run(0.070)
    gaps = key_up_gaps(out, voice)
    check("fast kick retriggers still get key-up time",
          gaps and all(g >= 0.025 - 1e-9 for g in gaps), f"got {gaps}")
    durations = [b - a for a, b in zip(
        sorted(e['time'] for e in out if e['type'] == 'note_on'),
        sorted(e['time'] for e in out if e['type'] == 'note_off'))]
    check("trimmed taps are still long enough to register",
          all(d >= MIN_NOTE_LEN - 1e-9 for d in durations), f"got {durations}")


# --- simulator: the right key comes back up -------------------------------

def test_zone_change_does_not_strand_a_key():
    print("[octave change mid-note]")
    sim, log = make_sim()
    sim.press_note(60)        # zone 0 -> presses the C4 key
    sim.press_note(84)        # forces zone +1 (L Shift)
    sim.release_note(84)
    sim.release_note(60)      # must release the C4 key, not re-derive to C3
    check("the key that was pressed is the key released",
          log.count(('DOWN', 'C4')) == 1 and log.count(('UP', 'C4')) == 1,
          f"got {log}")
    check("no key is left held", not sim.key_refs or
          all(v == 0 for v in sim.key_refs.values()), f"got {sim.key_refs}")
    check("no press record leaks", sim.note_keys == {}, f"got {sim.note_keys}")


def test_zone_change_remaps_held_note_to_preserve_pitch():
    print("[octave change remaps a held note]")
    sim, log = make_sim()
    sim.press_note(72)        # C5 = Q in the unshifted zone
    sim.press_note(84)        # C6 requires Shift; held C5 must move to A

    old_key_up = log.index(('UP', 'C5'))
    shift_down = log.index(('DOWN', 'LSHIFT'))
    remapped_down = log.index(('DOWN', 'C4'))
    check("held C5 is released before Shift changes its pitch",
          old_key_up < shift_down, f"got {log}")
    check("held C5 is re-pressed on A/C4 after Shift",
          shift_down < remapped_down, f"got {log}")
    check("C5 and C6 use distinct physical keys in the high zone",
          sim.note_keys.get(72) == [KEY_MAP['C4']]
          and sim.note_keys.get(84) == [KEY_MAP['C5']],
          f"got {sim.note_keys}")

    sim.release_note(84)
    sim.release_note(72)
    check("remapped notes release their replacement keys",
          log.count(('UP', 'C4')) == 1
          and log.count(('UP', 'C5')) == 2,
          f"got {log}")
    check("remapping leaves no held-key state behind",
          sim.note_keys == {}
          and all(refs == 0 for refs in sim.key_refs.values()),
          f"notes={sim.note_keys} refs={sim.key_refs}")


def test_incompatible_held_note_is_silenced_safely_on_zone_change():
    print("[incompatible held note at octave change]")
    sim, log = make_sim()
    sim.press_note(48)        # C3 cannot coexist with C6 in any one zone
    sim.press_note(84)        # choose the new note's high zone

    check("out-of-zone held note is released before the modifier changes",
          log.index(('UP', 'C3')) < log.index(('DOWN', 'LSHIFT')),
          f"got {log}")
    check("incompatible note keeps a harmless release placeholder",
          sim.note_keys.get(48) == [None], f"got {sim.note_keys}")
    before_release = list(log)
    sim.release_note(48)
    check("incompatible note-off does not release the high note's key",
          log == before_release, f"before={before_release} after={log}")
    sim.release_note(84)
    check("compatible high note still releases normally",
          log[-1] == ('UP', 'C5'), f"got {log}")


def test_repeats_under_a_held_high_note():
    print("[repeats while the zone is pinned]")
    sim, log = make_sim()
    sim.press_note(84)                     # pins zone +1
    for _ in range(3):
        sim.press_note(60)
        sim.release_note(60)
    sim.release_note(84)
    downs = [e for e in log if e == ('DOWN', 'C3')]
    ups = [e for e in log if e == ('UP', 'C3')]
    check("three distinct presses", len(downs) == 3, f"got {len(downs)}")
    check("three matching releases", len(ups) == 3, f"got {len(ups)}")
    check("everything ends up released",
          all(v == 0 for v in sim.key_refs.values()), f"got {sim.key_refs}")


def test_same_key_shared_by_two_notes_refcounts():
    print("[one key, two sounding notes]")
    sim, log = make_sim()
    sim.press_note(60)
    sim.press_note(60)        # second voice on the same key: retrigger
    sim.release_note(60)      # still one voice sounding -> key stays down
    check("key still held after the first release",
          sim.key_refs[KEY_MAP['C4']] == 1, f"got {sim.key_refs}")
    sim.release_note(60)
    check("key released once the last voice ends",
          sim.key_refs[KEY_MAP['C4']] == 0, f"got {sim.key_refs}")
    check("the retrigger let go before pressing again",
          log == [('DOWN', 'C4'), ('UP', 'C4'), ('DOWN', 'C4'), ('UP', 'C4')],
          f"got {log}")


def test_runtime_gap_is_enforced():
    print("[runtime key-up backstop]")
    import time
    sim, log = make_sim(gap_ms=20)
    sim.press_note(60)
    sim.release_note(60)
    t0 = time.perf_counter()
    sim.press_note(60)        # no gap in the event stream -> simulator waits
    elapsed = time.perf_counter() - t0
    check("press waits out the remaining key-up time", elapsed >= 0.015,
          f"waited {elapsed * 1000:.1f}ms")
    sim.release_note(60)


def test_focus_guard_blocks_key_down_outside_target():
    print("[foreground-window safety]")
    sim, log = make_sim()
    sim.focus_guard_enabled = True
    sim.target_window_text = "Blue Protocol"
    sim.foreground_window_title = lambda: "Notes - Personal"
    sim.press_note(60)
    check("wrong foreground window blocks input", log == [], f"got {log}")
    check("blocked press is not reference-counted", sim.key_refs == {})


def test_focus_guard_defers_sustain_off_without_tapping_wrong_window():
    print("[foreground-window sustain safety]")
    sim, log = make_sim()
    isim.tap_key = lambda vk, duration=0.01: log.append(('TAP', vk))
    sim.focus_guard_enabled = True
    sim.target_window_text = "Blue Protocol"
    sim.foreground_window_title = lambda: "Blue Protocol"
    sim.set_sustain(True)
    log.clear()

    sim.foreground_window_title = lambda: "Notes - Personal"
    sim.release_all()
    check("focus loss never taps Space into another app", not any(
        item[0] == 'TAP' for item in log), f"got {log}")
    check("pedal state remains truthful until it can be toggled off",
          sim.sustain_active and sim._pending_sustain_off)

    sim.foreground_window_title = lambda: "Blue Protocol"
    sim.flush_pending_sustain()
    check("deferred pedal-off is applied in the game",
          not sim.sustain_active and not sim._pending_sustain_off)


def test_temporary_focus_loss_cancels_deferred_sustain_off():
    print("[temporary focus-loss sustain restoration]")
    sim, log = make_sim()
    isim.tap_key = lambda vk, duration=0.01: log.append(('TAP', vk))
    sim.focus_guard_enabled = True
    sim.target_window_text = "Blue Protocol"
    sim.foreground_window_title = lambda: "Blue Protocol"
    sim.set_sustain(True)
    log.clear()
    sim.foreground_window_title = lambda: "Other"
    sim.release_all()
    sim.foreground_window_title = lambda: "Blue Protocol"
    sim.set_sustain(True)
    check("restoration keeps an already-active pedal without a second toggle",
          log == [] and sim.sustain_active and not sim._pending_sustain_off,
          f"got {log}")


def test_stray_note_off_is_safe():
    print("[note_off with no matching press]")
    sim, log = make_sim()
    sim.release_note(60)      # e.g. left over from before a seek
    check("no crash and nothing spuriously held",
          all(v == 0 for v in sim.key_refs.values()), f"got {sim.key_refs}")
    sim.press_note(60)
    sim.release_all()
    check("release_all clears the press records", sim.note_keys == {},
          f"got {sim.note_keys}")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
