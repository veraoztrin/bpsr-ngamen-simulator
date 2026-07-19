# Verification: sustain pedal support, raw pass-through at default settings,
# and pipeline/playback performance.
# Run from the repo root:  python tests\test_passthrough_perf.py

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arranger import ConversionSettings, convert, ABS_LOW, ABS_HIGH

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

def sus(t, val, ch=0):
    return {'time': t, 'type': 'sustain', 'value': val, 'channel': ch}


# --- 1. Sustain pedal support ----------------------------------------------

def test_sustain():
    print("[sustain pedal]")
    evs = [on(0.0, 60), sus(0.2, True), off(0.5, 60), on(0.6, 62),
           sus(0.8, False), off(1.0, 62)]

    out = convert(evs, ConversionSettings(), orig_bpm=120)
    sustains = [e for e in out if e['type'] == 'sustain']
    check("sustain events survive default conversion", len(sustains) == 2)
    check("sustain values preserved in order",
          [s['value'] for s in sustains] == [True, False])
    check("sustain times untouched at defaults",
          abs(sustains[0]['time'] - 0.2) < 1e-9 and abs(sustains[1]['time'] - 0.8) < 1e-9)

    out = convert(evs, ConversionSettings(speed=2.0), orig_bpm=120)
    sustains = [e for e in out if e['type'] == 'sustain']
    check("sustain times scale with speed",
          abs(sustains[0]['time'] - 0.1) < 1e-9 and abs(sustains[1]['time'] - 0.4) < 1e-9)

    # Heavy feature mix must not eat pedal events
    s = ConversionSettings(note_thinning=True, cull_low_priority=True,
                           prioritize_melody=True, phrase_gap_shifting=True,
                           max_chord_notes=2)
    out = convert(evs, s, orig_bpm=120)
    check("sustain survives all features enabled",
          len([e for e in out if e['type'] == 'sustain']) == 2)


# --- 2. Raw pass-through at default settings -------------------------------

def test_passthrough():
    print("[raw pass-through]")
    # A realistic in-range sequence: chords, overlaps, repeats, 3 channels
    evs = []
    t = 0.0
    import random
    rng = random.Random(42)
    for i in range(300):
        ch = i % 3
        note = rng.randint(ABS_LOW, ABS_HIGH)
        vel = rng.randint(20, 127)
        dur = rng.choice([0.1, 0.25, 0.5])
        evs.append(on(round(t, 6), note, vel, ch))
        evs.append(off(round(t + dur, 6), note, ch))
        if i % 20 == 0:
            evs.append(sus(round(t, 6), i % 40 == 0, ch))
        t += rng.choice([0.0, 0.05, 0.12])  # includes simultaneous starts
    evs.sort(key=lambda e: e['time'])

    out = convert(evs, ConversionSettings(), orig_bpm=120)

    def key_set(events):
        ks = []
        for e in events:
            if e['type'] == 'note_on':
                ks.append(('on', round(e['time'], 6), e['note'], e['velocity'], e['channel']))
            elif e['type'] == 'note_off':
                ks.append(('off', round(e['time'], 6), e['note'], e['channel']))
            elif e['type'] == 'sustain':
                ks.append(('sus', round(e['time'], 6), e['value'], e['channel']))
        return sorted(ks)

    check("default settings = byte-identical event content",
          key_set(evs) == key_set(out),
          f"in={len(evs)} out={len(out)}")
    check("no zone hints injected at defaults",
          not any(e['type'] == 'zone' for e in out))

    # Out-of-range notes: folded by octave (pitch class kept), not dropped.
    # (The old code dropped notes outside MIDI 36-95 entirely; folding is
    #  the same policy the simulator already used for 36-47 / 84-95.)
    evs2 = [on(0.0, 24), off(1.0, 24)]  # C1, below playable range
    out2 = convert(evs2, ConversionSettings(), orig_bpm=120)
    ons = [e for e in out2 if e['type'] == 'note_on']
    check("out-of-range note folded, not dropped",
          len(ons) == 1 and ons[0]['note'] == 36, f"got {ons}")


# --- 3. Performance --------------------------------------------------------

def test_performance():
    print("[performance]")
    import random
    rng = random.Random(7)

    # Build a big song: ~8000 notes / ~16500 events across 4 channels
    evs = []
    t = 0.0
    for i in range(8000):
        ch = i % 4
        note = rng.randint(20, 110)  # includes out-of-range notes
        vel = rng.randint(10, 127)
        evs.append(on(round(t, 6), note, vel, ch))
        evs.append(off(round(t + rng.uniform(0.05, 0.8), 6), note, ch))
        if i % 50 == 0:
            evs.append(sus(round(t, 6), (i // 50) % 2 == 0, ch))
        t += rng.choice([0.0, 0.0, 0.02, 0.05, 0.1])
    evs.sort(key=lambda e: e['time'])
    n_ev = len(evs)

    t0 = time.perf_counter()
    convert(evs, ConversionSettings(), orig_bpm=120)
    dt_default = (time.perf_counter() - t0) * 1000

    heavy = ConversionSettings(
        bpm_override=100, speed=1.25, max_chord_notes=3,
        note_thinning=True, cull_low_priority=True, prioritize_melody=True,
        consistent_windows=True, voice_aware=True,
        phrase_gap_shifting=True, duet_mode=True,
    )
    t0 = time.perf_counter()
    out = convert(evs, heavy, orig_bpm=120)
    dt_heavy = (time.perf_counter() - t0) * 1000

    print(f"      {n_ev} events: defaults {dt_default:.1f} ms | all features {dt_heavy:.1f} ms")
    check("default conversion under 250 ms", dt_default < 250, f"{dt_default:.1f} ms")
    check("full-feature conversion under 1000 ms", dt_heavy < 1000, f"{dt_heavy:.1f} ms")
    check("heavy output still valid & sorted",
          all(out[i]['time'] <= out[i+1]['time'] for i in range(len(out) - 1)))

    # Playback timing accuracy: 100 events at 20 ms spacing, measure jitter
    from player import MidiPlayer

    class TimingSim:
        def __init__(self):
            self.stamps = []
            self.shift_delay_ms = 0
            self.shift_hold_ms = 0
        def press_note(self, n): self.stamps.append(time.perf_counter())
        def release_note(self, n): pass
        def set_sustain(self, v): pass
        def set_octave_shift(self, z): pass
        def release_all(self): pass

    events = []
    for i in range(100):
        events.append(on(i * 0.02, 60))
        events.append(off(i * 0.02 + 0.01, 60))
    events.sort(key=lambda e: (e['time'], e['type'] == 'note_on'))

    player = MidiPlayer()
    player.simulator = TimingSim()
    player.load_events(events, active_channels=[0])
    player.play()
    deadline = time.time() + 10
    while player.is_playing and time.time() < deadline:
        time.sleep(0.01)
    player.stop()

    stamps = player.simulator.stamps
    check("all 100 timed notes fired", len(stamps) == 100, f"got {len(stamps)}")
    if len(stamps) == 100:
        base = stamps[0]
        errors = sorted(abs((s - base) - i * 0.02) * 1000 for i, s in enumerate(stamps))
        mean_err = sum(errors) / len(errors)
        p95_err = errors[94]
        max_err = errors[-1]
        print(f"      playback jitter: mean {mean_err:.2f} ms, p95 {p95_err:.2f} ms, max {max_err:.2f} ms")
        # p95 rather than absolute max: a single OS scheduling hiccup
        # shouldn't fail the suite.
        check("mean timing error under 5 ms", mean_err < 5, f"{mean_err:.2f} ms")
        check("p95 timing error under 10 ms", p95_err < 10, f"{p95_err:.2f} ms")


if __name__ == "__main__":
    test_sustain()
    test_passthrough()
    test_performance()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
