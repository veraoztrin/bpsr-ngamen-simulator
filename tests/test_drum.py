# Tests for arranger.convert_drum() - the "Drum" instrument conversion mode.
# Two paths are covered:
#   * a real GM percussion track (channel 9) mapped onto the 9 in-game voices
#   * a melodic MIDI turned into a section-aware kit groove (_generate_groove)
# See config.DRUM_NOTES / convert_drum's docstring for why this isn't a 1:1
# pitch mapping. Run from the repo root:  python tests\test_drum.py

import sys
import os

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


def _note(time, dur, note, channel=0, velocity=64):
    return [
        {'time': time, 'type': 'note_on', 'note': note, 'velocity': velocity, 'channel': channel},
        {'time': time + dur, 'type': 'note_off', 'note': note, 'channel': channel},
    ]


def _hits(events):
    """Collapse an event list back to a sorted (time, note) hit list."""
    return sorted((round(e['time'], 4), e['note']) for e in events if e['type'] == 'note_on')


def _melody(bars, notes_per_bar, beat_len=0.5, beats=4, base=60, start_bar=0):
    """`notes_per_bar` evenly-spaced notes in each of `bars` bars (120 BPM 4/4)."""
    bar_len = beat_len * beats
    evs = []
    for bar in range(bars):
        if notes_per_bar <= 0:
            continue
        step = bar_len / notes_per_bar
        for i in range(notes_per_bar):
            t = (start_bar + bar) * bar_len + i * step
            evs += _note(t, min(step * 0.8, 0.1), base + (i % 5))
    evs.sort(key=lambda e: e['time'])
    return evs


# ---------------------------------------------------------------------------
# Basic safety
# ---------------------------------------------------------------------------

def test_empty_input_is_safe():
    from arranger import ConversionSettings, convert_drum
    out = convert_drum([], ConversionSettings(), orig_bpm=120)
    check("no events in -> no events out", out == [], f"got {out}")


def test_output_notes_are_short_taps_not_sustained():
    from arranger import ConversionSettings, convert_drum, DRUM_HIT_LEN
    events = _note(0.0, 2.0, 60) + _note(0.5, 2.0, 64) + _note(1.0, 2.0, 67)
    out = convert_drum(events, ConversionSettings(), orig_bpm=120)
    durs = []
    for on in [e for e in out if e['type'] == 'note_on']:
        off = next(o for o in out if o['type'] == 'note_off'
                   and o['note'] == on['note'] and o['time'] >= on['time'])
        durs.append(off['time'] - on['time'])
    check("there is drum output for a melodic MIDI", len(durs) > 0, f"got {len(durs)}")
    check("no drum hit is longer than DRUM_HIT_LEN",
          all(d <= DRUM_HIT_LEN + 1e-6 for d in durs), f"max {max(durs) if durs else 0}")


def test_output_has_no_sustain_or_zone_events():
    from arranger import ConversionSettings, convert_drum
    events = _melody(2, 4) + [{'time': 0.05, 'type': 'sustain', 'value': True, 'channel': 0}]
    events.sort(key=lambda e: e['time'])
    out = convert_drum(events, ConversionSettings(), orig_bpm=120)
    check("no sustain events in drum output", all(e['type'] != 'sustain' for e in out), "found sustain")
    check("no zone events in drum output", all(e['type'] != 'zone' for e in out), "found zone")


# ---------------------------------------------------------------------------
# GM percussion path (a real drum track) - preserved, not re-invented
# ---------------------------------------------------------------------------

def test_gm_drum_track_maps_by_bucket():
    from arranger import ConversionSettings, convert_drum, DRUM_KICK, DRUM_SNARE, DRUM_HH_CLOSED
    events = (_note(0.00, 0.03, 36, channel=9)
              + _note(0.40, 0.03, 38, channel=9)
              + _note(0.80, 0.03, 42, channel=9))
    events.sort(key=lambda e: e['time'])
    out = convert_drum(events, ConversionSettings(), orig_bpm=120)
    hits = _hits(out)
    check("3 distinct GM drum hits produce 3 hits", len(hits) == 3, f"got {hits}")
    check("GM kick(36) -> DRUM_KICK", hits[0][1] == DRUM_KICK, f"got {hits}")
    check("GM snare(38) -> DRUM_SNARE", hits[1][1] == DRUM_SNARE, f"got {hits}")
    check("GM closed hihat(42) -> DRUM_HH_CLOSED", hits[2][1] == DRUM_HH_CLOSED, f"got {hits}")


def test_gm_full_kit_maps_to_distinct_voices():
    from arranger import (ConversionSettings, convert_drum, DRUM_KICK, DRUM_SNARE,
                          DRUM_FLOOR_TOM, DRUM_TOM_1, DRUM_TOM_2, DRUM_CRASH_1,
                          DRUM_CRASH_2, DRUM_HH_CLOSED, DRUM_HH_OPEN)
    plan = [36, 38, 42, 46, 41, 45, 48, 49, 57]
    expected = [DRUM_KICK, DRUM_SNARE, DRUM_HH_CLOSED, DRUM_HH_OPEN,
                DRUM_FLOOR_TOM, DRUM_TOM_2, DRUM_TOM_1, DRUM_CRASH_1, DRUM_CRASH_2]
    events = []
    for i, gm_note in enumerate(plan):
        events += _note(i * 0.4, 0.03, gm_note, channel=9)
    events.sort(key=lambda e: e['time'])
    out = convert_drum(events, ConversionSettings(), orig_bpm=120)
    voices = [n for _, n in _hits(out)]
    check("each GM voice maps to its own in-game voice", voices == expected,
          f"got {voices}, expected {expected}")


def test_gm_ride_folds_to_closed_hat():
    from arranger import ConversionSettings, convert_drum, DRUM_HH_CLOSED
    out = convert_drum(_note(0.0, 0.03, 51, channel=9), ConversionSettings(), orig_bpm=120)
    check("GM ride(51) folds to closed hi-hat", _hits(out) == [(0.0, DRUM_HH_CLOSED)], f"got {_hits(out)}")


def test_gm_track_ignores_other_channels():
    from arranger import ConversionSettings, convert_drum, DRUM_KICK
    events = (_note(0.0, 0.03, 36, channel=9)
              + _note(0.1, 0.05, 72, channel=0)
              + _note(0.2, 0.05, 74, channel=1))
    events.sort(key=lambda e: e['time'])
    out = convert_drum(events, ConversionSettings(), orig_bpm=120)
    check("only the GM drum channel contributes hits", _hits(out) == [(0.0, DRUM_KICK)], f"got {_hits(out)}")


def test_gm_retrigger_floor_drops_rapid_same_voice_hits():
    from arranger import ConversionSettings, convert_drum, DRUM_MIN_GAP
    events = _note(0.0, 0.02, 36, channel=9) + _note(0.04, 0.02, 36, channel=9)
    events.sort(key=lambda e: e['time'])
    out = convert_drum(events, ConversionSettings(), orig_bpm=120)
    check("hit inside the per-voice retrigger floor is dropped", len(_hits(out)) == 1,
          f"got {_hits(out)}, DRUM_MIN_GAP={DRUM_MIN_GAP}")


# ---------------------------------------------------------------------------
# Section-aware groove path (a melodic MIDI, no drum channel)
# ---------------------------------------------------------------------------

def test_melodic_midi_generates_backbone_groove():
    from arranger import ConversionSettings, convert_drum, DRUM_KICK, DRUM_SNARE, DRUM_HH_CLOSED
    out = convert_drum(_melody(4, 4), ConversionSettings(), orig_bpm=120, beats_per_measure=4)
    voices = set(n for _, n in _hits(out))
    check("groove has a kick", DRUM_KICK in voices, f"got {sorted(voices)}")
    check("groove has a snare", DRUM_SNARE in voices, f"got {sorted(voices)}")
    check("groove has a closed hi-hat", DRUM_HH_CLOSED in voices, f"got {sorted(voices)}")


def test_busy_song_is_busier_than_sparse_song():
    from arranger import ConversionSettings, convert_drum
    sparse = convert_drum(_melody(8, 1), ConversionSettings(), orig_bpm=120)
    busy = convert_drum(_melody(8, 16), ConversionSettings(), orig_bpm=120)
    n_sparse = len([e for e in sparse if e['type'] == 'note_on'])
    n_busy = len([e for e in busy if e['type'] == 'note_on'])
    check("a busy melody yields a denser drum track than a sparse one",
          n_busy > n_sparse * 1.5, f"sparse={n_sparse} busy={n_busy}")


def test_busy_song_uses_toms_and_crash():
    from arranger import (ConversionSettings, convert_drum, DRUM_TOM_1, DRUM_TOM_2,
                          DRUM_FLOOR_TOM, DRUM_CRASH_1, DRUM_CRASH_2)
    out = convert_drum(_melody(8, 16), ConversionSettings(), orig_bpm=120, beats_per_measure=4)
    voices = set(n for _, n in _hits(out))
    check("busy song produces a tom fill", bool(voices & {DRUM_TOM_1, DRUM_TOM_2, DRUM_FLOOR_TOM}),
          f"got {sorted(voices)}")
    check("busy song produces a crash", bool(voices & {DRUM_CRASH_1, DRUM_CRASH_2}), f"got {sorted(voices)}")


def test_sparse_song_stays_minimal():
    from arranger import (ConversionSettings, convert_drum, DRUM_TOM_1, DRUM_TOM_2, DRUM_FLOOR_TOM)
    out = convert_drum(_melody(8, 1), ConversionSettings(), orig_bpm=120, beats_per_measure=4)
    voices = set(n for _, n in _hits(out))
    check("sparse song has no tom fills", not (voices & {DRUM_TOM_1, DRUM_TOM_2, DRUM_FLOOR_TOM}),
          f"got {sorted(voices)}")


def test_rest_bars_produce_no_drums():
    from arranger import ConversionSettings, convert_drum
    bar = 0.5 * 4  # 2.0s at 120 BPM 4/4
    events = []
    for i in range(8):
        events += _note(i * (bar / 8), 0.1, 60)            # bar 0 busy
    for i in range(8):
        events += _note(4 * bar + i * (bar / 8), 0.1, 60)  # bar 4 busy
    events.sort(key=lambda e: e['time'])
    out = convert_drum(events, ConversionSettings(), orig_bpm=120, beats_per_measure=4)
    ons = [e['time'] for e in out if e['type'] == 'note_on']
    in_rest = [t for t in ons if bar + 0.05 < t < 4 * bar - 0.05]
    check("silent bars stay drumless", len(in_rest) == 0, f"got hits in rest at {in_rest[:5]}")


def test_groove_scales_with_speed():
    from arranger import ConversionSettings, convert_drum
    def span(out):
        ons = [e['time'] for e in out if e['type'] == 'note_on']
        return (max(ons) - min(ons)) if ons else 0.0
    normal = convert_drum(_melody(4, 4), ConversionSettings(), orig_bpm=120)
    half = convert_drum(_melody(4, 4), ConversionSettings(speed=0.5), orig_bpm=120)
    check("half speed roughly doubles the groove's time span",
          abs(span(half) - 2 * span(normal)) < 0.3 * span(normal) + 0.1,
          f"normal={span(normal):.2f} half={span(half):.2f}")


def test_varied_song_uses_all_nine_voices():
    from arranger import ConversionSettings, convert_drum, DRUM_NOTES
    # Distinct sections: sparse+low, medium+mid, busy+high (a drop), busy+mid.
    # Across ballad/backbeat/four/drive styles + fills + crashes the groove
    # should touch every one of the 9 in-game voices.
    events = (_melody(4, 1, base=46, start_bar=0)      # sparse, low register
              + _melody(4, 4, base=60, start_bar=4)    # medium, mid
              + _melody(4, 18, base=79, start_bar=8)   # busy, high  -> a "drop"
              + _melody(4, 18, base=60, start_bar=12))  # busy, mid
    events.sort(key=lambda e: e['time'])
    out = convert_drum(events, ConversionSettings(), orig_bpm=120, beats_per_measure=4)
    voices = set(n for _, n in _hits(out))
    missing = set(DRUM_NOTES) - voices
    check("all 9 in-game drum voices are used across a varied song",
          not missing, f"missing {sorted(missing)}; used {sorted(voices)}")


def test_style_switches_between_sections():
    from arranger import ConversionSettings, convert_drum, DRUM_HH_OPEN
    # A quiet section followed by a busy high section (a "drop") must produce
    # audibly different drumming - denser, and leaning on the open hi-hat.
    quiet = _melody(4, 3, base=55, start_bar=0)
    loud = _melody(4, 18, base=79, start_bar=4)
    events = sorted(quiet + loud, key=lambda e: e['time'])
    out = convert_drum(events, ConversionSettings(), orig_bpm=120, beats_per_measure=4)
    bar = 0.5 * 4
    early = [n for t, n in _hits(out) if t < 4 * bar]
    late = [n for t, n in _hits(out) if t >= 4 * bar]
    check("the busy section is denser than the quiet one",
          len(late) > len(early) * 1.5, f"early={len(early)} late={len(late)}")
    check("the busy 'drop' section brings the open hi-hat",
          DRUM_HH_OPEN in late, f"late voices {sorted(set(late))}")


if __name__ == "__main__":
    for fn in [
        test_empty_input_is_safe,
        test_output_notes_are_short_taps_not_sustained,
        test_output_has_no_sustain_or_zone_events,
        test_gm_drum_track_maps_by_bucket,
        test_gm_full_kit_maps_to_distinct_voices,
        test_gm_ride_folds_to_closed_hat,
        test_gm_track_ignores_other_channels,
        test_gm_retrigger_floor_drops_rapid_same_voice_hits,
        test_melodic_midi_generates_backbone_groove,
        test_busy_song_is_busier_than_sparse_song,
        test_busy_song_uses_toms_and_crash,
        test_sparse_song_stays_minimal,
        test_rest_bars_produce_no_drums,
        test_groove_scales_with_speed,
        test_varied_song_uses_all_nine_voices,
        test_style_switches_between_sections,
    ]:
        fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
