# Tests for the arranger conversion pipeline.
# Run from the repo root:  python -m tests.test_arranger
# Pure-Python, no dependencies needed.

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arranger import (
    ConversionSettings, convert, events_to_notes, notes_to_events,
    ZONE_RANGES, ABS_LOW, ABS_HIGH,
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

def note_ons(evs):
    return [e for e in evs if e['type'] == 'note_on']


# --- pairing ---------------------------------------------------------------

def test_pairing():
    evs = [on(0.0, 60), on(0.5, 62), off(1.0, 60), off(1.5, 62)]
    notes, sustains = events_to_notes(evs)
    check("pairing: two notes", len(notes) == 2)
    check("pairing: durations", abs(notes[0]['end'] - 1.0) < 1e-9 and abs(notes[1]['end'] - 1.5) < 1e-9)

    # Unclosed note gets closed at the last event time
    evs = [on(0.0, 60), on(1.0, 62), off(2.0, 62)]
    notes, _ = events_to_notes(evs)
    n60 = next(n for n in notes if n['note'] == 60)
    check("pairing: unclosed note closed at end", abs(n60['end'] - 2.0) < 1e-9)


# --- tempo / speed ---------------------------------------------------------

def test_speed_and_bpm():
    evs = [on(0.0, 60), off(1.0, 60), on(2.0, 62), off(3.0, 62)]

    out = convert(evs, ConversionSettings(speed=2.0), orig_bpm=120)
    ons = note_ons(out)
    check("speed 2.0 halves times", abs(ons[1]['time'] - 1.0) < 1e-6, f"got {ons[1]['time']}")

    out = convert(evs, ConversionSettings(bpm_override=60), orig_bpm=120)
    ons = note_ons(out)
    check("bpm 120->60 doubles times", abs(ons[1]['time'] - 4.0) < 1e-6, f"got {ons[1]['time']}")

    out = convert(evs, ConversionSettings(bpm_override=60, speed=2.0), orig_bpm=120)
    ons = note_ons(out)
    check("bpm+speed combine", abs(ons[1]['time'] - 2.0) < 1e-6, f"got {ons[1]['time']}")


# --- chord limiting --------------------------------------------------------

def test_max_chord():
    # 5-note chord at t=0
    chord = [60, 64, 67, 71, 74]
    evs = [on(0.0, n, vel=64 + i) for i, n in enumerate(chord)]
    evs += [off(1.0, n) for n in chord]

    out = convert(evs, ConversionSettings(max_chord_notes=3), orig_bpm=120)
    ons = note_ons(out)
    check("max chord 3 keeps 3", len(ons) == 3, f"got {len(ons)}")
    offs = [e for e in out if e['type'] == 'note_off']
    check("dropped notes lose their note_off too", len(offs) == 3, f"got {len(offs)}")
    # Highest velocities were 74(68), 71(67), 67(66)
    kept = sorted(e['note'] for e in ons)
    check("keeps loudest", kept == [67, 71, 74], f"got {kept}")

def test_prioritize_melody():
    # Top note is the QUIETEST: would normally be culled
    evs = [on(0.0, 60, vel=100), on(0.0, 64, vel=90), on(0.0, 84, vel=10)]
    evs += [off(1.0, 60), off(1.0, 64), off(1.0, 84)]

    out = convert(evs, ConversionSettings(max_chord_notes=2, prioritize_melody=True), orig_bpm=120)
    kept = sorted(e['note'] for e in note_ons(out))
    check("melody survives trim", 84 in kept, f"got {kept}")
    check("trim to 2", len(kept) == 2, f"got {kept}")

def test_cull_low_priority():
    evs = [on(0.0, 60, vel=100), on(0.0, 64, vel=95), on(0.0, 67, vel=10)]
    evs += [off(1.0, 60), off(1.0, 64), off(1.0, 67)]
    out = convert(evs, ConversionSettings(cull_low_priority=True), orig_bpm=120)
    kept = sorted(e['note'] for e in note_ons(out))
    check("quiet chord member culled", kept == [60, 64], f"got {kept}")


# --- thinning --------------------------------------------------------------

def test_thinning():
    # Machine-gun repeat: 4x same note 15ms apart
    evs = []
    t = 0.0
    for _ in range(4):
        evs.append(on(t, 60))
        evs.append(off(t + 0.010, 60))
        t += 0.015
    out = convert(evs, ConversionSettings(note_thinning=True), orig_bpm=120)
    check("repeats merged to one", len(note_ons(out)) == 1, f"got {len(note_ons(out))}")

    # Normal repeats far apart survive
    evs = [on(0.0, 60), off(0.4, 60), on(1.0, 60), off(1.4, 60)]
    out = convert(evs, ConversionSettings(note_thinning=True), orig_bpm=120)
    check("slow repeats survive", len(note_ons(out)) == 2, f"got {len(note_ons(out))}")


# --- range -----------------------------------------------------------------

def test_range_folding():
    evs = [on(0.0, 24), off(1.0, 24), on(2.0, 108), off(3.0, 108)]  # C1 and C8
    out = convert(evs, ConversionSettings(), orig_bpm=120)
    pitches = sorted(e['note'] for e in note_ons(out))
    check("all folded into playable range",
          all(ABS_LOW <= p <= ABS_HIGH for p in pitches), f"got {pitches}")
    check("folding preserves pitch class",
          all(p % 12 == 0 for p in pitches), f"got {pitches}")

def test_custom_range():
    evs = [on(0.0, 40), off(1.0, 40), on(2.0, 90), off(3.0, 90)]
    s = ConversionSettings(range_low=48, range_high=83)  # C3..B5 only
    out = convert(evs, s, orig_bpm=120)
    pitches = sorted(e['note'] for e in note_ons(out))
    check("custom range respected", all(48 <= p <= 83 for p in pitches), f"got {pitches}")

def test_proportional_remap():
    # Span of 5 octaves (36..96) compressed into C3..B5 (48..83)
    evs = [on(0.0, 36), off(0.5, 36), on(1.0, 66), off(1.5, 66), on(2.0, 96), off(2.5, 96)]
    s = ConversionSettings(proportional_remap=True, range_low=48, range_high=83)
    out = convert(evs, s, orig_bpm=120)
    pitches = [e['note'] for e in note_ons(out)]
    check("remap inside range", all(48 <= p <= 83 for p in pitches), f"got {pitches}")
    check("remap preserves order", pitches[0] < pitches[1] < pitches[2], f"got {pitches}")
    check("remap keeps extremes at edges", pitches[0] == 48 and pitches[2] == 83, f"got {pitches}")


# --- phrase gap shifting ---------------------------------------------------

def test_phrase_zones():
    # Phrase 1: high run (needs +1 zone), then a 0.5s gap, phrase 2: low run
    evs = []
    t = 0.0
    for n in (86, 88, 90, 55):     # 55 is the straggler that would force a toggle
        evs.append(on(t, n)); evs.append(off(t + 0.05, n)); t += 0.06
    t += 0.5
    for n in (40, 42, 44):
        evs.append(on(t, n)); evs.append(off(t + 0.05, n)); t += 0.06

    out = convert(evs, ConversionSettings(phrase_gap_shifting=True), orig_bpm=120)
    zones = [e for e in out if e['type'] == 'zone']
    check("one zone hint per phrase", len(zones) == 2, f"got {len(zones)}")
    z1, z2 = zones[0]['value'], zones[1]['value']
    check("phrase zones chosen (+1 then -1)", z1 == 1 and z2 == -1, f"got {z1},{z2}")

    # Every note in each phrase must fit its phrase's zone
    ons = note_ons(out)
    p1 = [e['note'] for e in ons[:4]]
    p2 = [e['note'] for e in ons[4:]]
    lo1, hi1 = ZONE_RANGES[z1]
    lo2, hi2 = ZONE_RANGES[z2]
    check("phrase 1 fits its zone", all(lo1 <= n <= hi1 for n in p1), f"got {p1}")
    check("phrase 2 fits its zone", all(lo2 <= n <= hi2 for n in p2), f"got {p2}")
    check("zone hint precedes notes", out[0]['type'] == 'zone')


# --- duet ------------------------------------------------------------------

def test_duet():
    evs = [on(0.0, 48), on(0.0, 72), off(1.0, 48), off(1.0, 72),
           {'time': 0.5, 'type': 'sustain', 'value': True, 'channel': 0}]
    out = convert(evs, ConversionSettings(duet_mode=True, duet_split_note=60), orig_bpm=120)
    low = [e for e in note_ons(out) if e['channel'] == 0]
    high = [e for e in note_ons(out) if e['channel'] == 1]
    check("duet: bass on ch0", len(low) == 1 and low[0]['note'] == 48)
    check("duet: melody on ch1", len(high) == 1 and high[0]['note'] == 72)
    sus = [e for e in out if e['type'] == 'sustain']
    check("duet: sustain duplicated to both parts",
          sorted(e['channel'] for e in sus) == [0, 1], f"got {sus}")


# --- event ordering --------------------------------------------------------

def test_event_ordering():
    # note_off must come before note_on at the same timestamp
    evs = [on(0.0, 60), off(1.0, 60), on(1.0, 62), off(2.0, 62)]
    out = convert(evs, ConversionSettings(), orig_bpm=120)
    at_1 = [e['type'] for e in out if abs(e['time'] - 1.0) < 1e-9]
    check("off before on at same t", at_1 == ['note_off', 'note_on'], f"got {at_1}")


# --- consistent windows ----------------------------------------------------

def test_consistent_windows():
    # Notes 10ms apart straddling a greedy boundary; consistent grid puts
    # t=0.000 and t=0.010 in the same fixed 30ms slot every time.
    evs = [on(0.000, 60, vel=100), on(0.010, 64, vel=90), on(0.020, 67, vel=80)]
    evs += [off(1.0, 60), off(1.0, 64), off(1.0, 67)]
    s = ConversionSettings(max_chord_notes=2, consistent_windows=True)
    out = convert(evs, s, orig_bpm=120)
    check("consistent windows trims slot to 2", len(note_ons(out)) == 2,
          f"got {len(note_ons(out))}")


def test_melody_lock_drops_conflict():
    print("[melody lock: drop conflicts]")
    # Sustained high melody C6 (84) over its whole duration, with a low
    # C3 (48) chord struck underneath. 84 and 48 span 36 semitones apart at
    # the edges of what a single zone allows — 48 sits below zone +1 (60..95),
    # so it must be dropped so the melody stays in the high zone.
    evs = [on(0.0, 84), off(2.0, 84),          # long melody note
           on(0.5, 48, vel=90), off(1.0, 48),  # low note during the melody
           on(0.5, 52, vel=90), off(1.0, 52)]
    out = convert(evs, ConversionSettings(melody_lock=True), orig_bpm=120)
    kept = sorted(e['note'] for e in note_ons(out))
    check("melody note kept", 84 in kept, f"got {kept}")
    check("conflicting low notes dropped", 48 not in kept and 52 not in kept, f"got {kept}")
    zones = [e for e in out if e['type'] == 'zone']
    check("locked into high zone (+1)", any(z['value'] == 1 for z in zones), f"got {zones}")

def test_melody_lock_keeps_when_fits():
    print("[melody lock: keep when it fits]")
    # Melody G5 (79) + accompaniment C4 (60): span 19 semitones, both fit
    # zone 0 (48..83). Nothing should be dropped.
    evs = [on(0.0, 79), off(1.0, 79), on(0.0, 60), off(1.0, 60)]
    out = convert(evs, ConversionSettings(melody_lock=True), orig_bpm=120)
    kept = sorted(e['note'] for e in note_ons(out))
    check("both notes kept (fit one zone)", kept == [60, 79], f"got {kept}")

def test_melody_lock_bass_plays_when_alone():
    print("[melody lock: low part plays when melody absent]")
    # First a low phrase alone, then a high phrase alone — each should play in
    # its own zone (nothing dropped, since they don't overlap in time).
    evs = [on(0.0, 40), off(0.5, 40), on(1.0, 90), off(1.5, 90)]
    out = convert(evs, ConversionSettings(melody_lock=True), orig_bpm=120)
    kept = sorted(e['note'] for e in note_ons(out))
    check("both lone notes kept", kept == [40, 90], f"got {kept}")

def chan_of(out):
    return {e['note']: e['channel'] for e in out if e['type'] == 'note_on'}

def test_auto_split_two():
    print("[auto-split: melody vs accompaniment]")
    # Chord C4/E4/G4 with a high melody C6 on top.
    evs = [on(0.0, 60), on(0.0, 64), on(0.0, 67), on(0.0, 84),
           off(1.0, 60), off(1.0, 64), off(1.0, 67), off(1.0, 84)]
    out = convert(evs, ConversionSettings(auto_split=True, auto_split_parts=2), orig_bpm=120)
    ch = chan_of(out)
    check("melody (top) -> channel 0", ch[84] == 0, f"got {ch}")
    check("accompaniment -> channel 1",
          ch[60] == 1 and ch[64] == 1 and ch[67] == 1, f"got {ch}")
    check("only two channels used", set(ch.values()) == {0, 1}, f"got {ch}")

def test_auto_split_three():
    print("[auto-split: melody / harmony / bass]")
    evs = [on(0.0, 40), on(0.0, 60), on(0.0, 64), on(0.0, 84),
           off(1.0, 40), off(1.0, 60), off(1.0, 64), off(1.0, 84)]
    out = convert(evs, ConversionSettings(auto_split=True, auto_split_parts=3), orig_bpm=120)
    ch = chan_of(out)
    check("top -> melody ch0", ch[84] == 0, f"got {ch}")
    check("bottom -> bass ch2", ch[40] == 2, f"got {ch}")
    check("middle -> harmony ch1", ch[60] == 1 and ch[64] == 1, f"got {ch}")

def test_auto_split_sustained_melody():
    print("[auto-split: sustained melody keeps its role]")
    # Melody 84 held across the whole bar; lower notes struck underneath later.
    evs = [on(0.0, 84), off(2.0, 84),
           on(0.5, 55), off(1.0, 55), on(0.5, 59), off(1.0, 59)]
    out = convert(evs, ConversionSettings(auto_split=True, auto_split_parts=2), orig_bpm=120)
    ch = chan_of(out)
    check("held melody stays ch0", ch[84] == 0, f"got {ch}")
    check("later lower notes -> ch1", ch[55] == 1 and ch[59] == 1, f"got {ch}")

def test_auto_split_sustain_duplicated():
    print("[auto-split: sustain reaches every part]")
    evs = [on(0.0, 84), on(0.0, 48), off(1.0, 84), off(1.0, 48),
           {'time': 0.5, 'type': 'sustain', 'value': True, 'channel': 0}]
    out = convert(evs, ConversionSettings(auto_split=True, auto_split_parts=2), orig_bpm=120)
    sus_ch = sorted(e['channel'] for e in out if e['type'] == 'sustain')
    check("sustain copied to both parts", sus_ch == [0, 1], f"got {sus_ch}")

def test_melody_lock_fold_mode():
    print("[melody lock: fold mode keeps notes]")
    evs = [on(0.0, 84), off(2.0, 84), on(0.5, 48), off(1.0, 48)]
    out = convert(evs, ConversionSettings(melody_lock=True, melody_lock_mode='fold'),
                  orig_bpm=120)
    ons = note_ons(out)
    check("fold keeps both notes", len(ons) == 2, f"got {len(ons)}")
    check("folded note pulled into high zone", all(60 <= e['note'] <= 95 for e in ons),
          f"got {[e['note'] for e in ons]}")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
