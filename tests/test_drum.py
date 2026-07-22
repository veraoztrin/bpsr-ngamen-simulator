# Tests for arranger.convert_drum() - the "Drum" instrument's beat-generation
# conversion mode (see config.DRUM_NOTES / arranger.convert_drum's docstring
# for why this doesn't do a 1:1 pitch mapping like the other instruments).
# Run from the repo root:  python tests\test_drum.py

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


def test_empty_input_is_safe():
    from arranger import ConversionSettings, convert_drum
    out = convert_drum([], ConversionSettings(), orig_bpm=120)
    check("no events in -> no events out", out == [], f"got {out}")


def test_melodic_onbeat_alternates_kick_snare():
    from arranger import ConversionSettings, convert_drum, DRUM_KICK, DRUM_SNARE
    # 120 BPM -> quarter note = 0.5s. Four quarter notes squarely on the grid,
    # far enough apart that none collide with the chord window or min-gap.
    events = []
    for i, pitch in enumerate([60, 64, 67, 72]):
        events += _note(i * 0.5, 0.05, pitch)
    events.sort(key=lambda e: e['time'])

    out = convert_drum(events, ConversionSettings(), orig_bpm=120)
    hits = _hits(out)
    check("4 on-grid quarter notes produce 4 hits", len(hits) == 4, f"got {hits}")
    voices = [n for _, n in hits]
    check("on-grid quarters alternate kick/snare starting on kick",
          voices == [DRUM_KICK, DRUM_SNARE, DRUM_KICK, DRUM_SNARE], f"got {voices}")


def test_offbeat_note_becomes_hat():
    from arranger import ConversionSettings, convert_drum, DRUM_HH_CLOSED
    # 120 BPM: an 8th-note offbeat (t=0.25) sits well outside the on-grid
    # tolerance around the surrounding quarter notes.
    events = _note(0.25, 0.05, 64)
    out = convert_drum(events, ConversionSettings(), orig_bpm=120)
    hits = _hits(out)
    check("syncopated note produces exactly 1 hit", len(hits) == 1, f"got {hits}")
    check("off-grid onset maps to the closed hi-hat voice",
          hits[0][1] == DRUM_HH_CLOSED, f"got {hits}")


def test_chord_collapses_to_one_hit():
    from arranger import ConversionSettings, convert_drum
    # A 3-note chord at the same instant must not become 3 separate hits -
    # there's only one key for whichever voice it resolves to.
    events = []
    for pitch in (60, 64, 67):
        events += _note(0.0, 0.05, pitch)
    events.sort(key=lambda e: e['time'])
    out = convert_drum(events, ConversionSettings(), orig_bpm=120)
    hits = _hits(out)
    check("a simultaneous chord collapses to a single hit", len(hits) == 1, f"got {hits}")


def test_gm_drum_track_maps_by_bucket():
    from arranger import ConversionSettings, convert_drum, DRUM_KICK, DRUM_SNARE, DRUM_HH_CLOSED
    # A real GM percussion track (channel 9): kick(36), snare(38), closed
    # hihat(42), spread out in time so none collide with the chord window.
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
    # One hit per major GM voice, spaced 0.4s apart so none collide, in time
    # order. Verifies the expanded GM table routes toms, crashes and the open
    # hi-hat to their own in-game voices instead of collapsing to one bucket.
    #   36 kick, 38 snare, 42 closed hh, 46 open hh, 41 low floor tom,
    #   45 low tom, 48 hi-mid tom, 49 crash 1, 57 crash 2
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
    check("all 9 distinct GM voices survive as 9 hits", len(voices) == 9, f"got {voices}")


def test_gm_ride_folds_to_closed_hat():
    from arranger import ConversionSettings, convert_drum, DRUM_HH_CLOSED
    # The game has no ride cymbal; GM ride (51) folds onto the closed hi-hat.
    events = _note(0.0, 0.03, 51, channel=9)
    out = convert_drum(events, ConversionSettings(), orig_bpm=120)
    hits = _hits(out)
    check("GM ride(51) folds to closed hi-hat",
          hits == [(0.0, DRUM_HH_CLOSED)], f"got {hits}")


def test_gm_track_ignores_other_channels():
    from arranger import ConversionSettings, convert_drum, DRUM_KICK
    # When a real drum track is present, the melody on other channels must
    # NOT also generate hits - the real drum part is the source of truth.
    events = (_note(0.0, 0.03, 36, channel=9)
              + _note(0.1, 0.05, 72, channel=0)   # melody note - should be ignored
              + _note(0.2, 0.05, 74, channel=1))  # another melody note - ignored
    events.sort(key=lambda e: e['time'])
    out = convert_drum(events, ConversionSettings(), orig_bpm=120)
    hits = _hits(out)
    check("only the GM drum channel contributes hits", hits == [(0.0, DRUM_KICK)], f"got {hits}")


def test_retrigger_floor_drops_rapid_same_voice_hits():
    from arranger import ConversionSettings, convert_drum, DRUM_MIN_GAP
    # Two notes 0.04s apart: far enough that group_chords keeps them as two
    # separate onsets (default chord_window is 0.03s), but both land near
    # beat 0 -> both classify as kick, and 0.04s < DRUM_MIN_GAP (0.06s), so
    # the second must be dropped rather than flooding the same key.
    events = _note(0.0, 0.02, 60) + _note(0.04, 0.02, 61)
    events.sort(key=lambda e: e['time'])
    out = convert_drum(events, ConversionSettings(), orig_bpm=120)
    hits = _hits(out)
    check("hit inside the per-voice retrigger floor is dropped", len(hits) == 1,
          f"got {hits}, DRUM_MIN_GAP={DRUM_MIN_GAP}")


def test_speed_scales_the_beat_grid():
    from arranger import ConversionSettings, convert_drum, DRUM_KICK, DRUM_SNARE
    # Same 4 on-grid quarter notes as the alternation test, but at half speed
    # (settings.speed=0.5 doubles every timestamp) - the grid must scale with
    # it, so the same notes should still land on-grid and alternate the same
    # way, just twice as far apart in the output.
    events = []
    for i, pitch in enumerate([60, 64, 67, 72]):
        events += _note(i * 0.5, 0.05, pitch)
    events.sort(key=lambda e: e['time'])

    out = convert_drum(events, ConversionSettings(speed=0.5), orig_bpm=120)
    hits = _hits(out)
    check("4 hits survive at half speed", len(hits) == 4, f"got {hits}")
    check("half speed doubles the hit spacing",
          abs(hits[1][0] - hits[0][0] - 1.0) < 0.01, f"got {hits}")
    voices = [n for _, n in hits]
    check("alternation is preserved under speed scaling",
          voices == [DRUM_KICK, DRUM_SNARE, DRUM_KICK, DRUM_SNARE], f"got {voices}")


def test_output_notes_are_short_taps_not_sustained():
    from arranger import ConversionSettings, convert_drum, DRUM_HIT_LEN
    # A long, sustained melodic note (2 seconds) must still become a short
    # drum tap, not a 2-second held note - drums aren't sustained.
    events = _note(0.0, 2.0, 60)
    out = convert_drum(events, ConversionSettings(), orig_bpm=120)
    on = next(e for e in out if e['type'] == 'note_on')
    off = next(e for e in out if e['type'] == 'note_off')
    check("drum hit duration matches DRUM_HIT_LEN, not the source note's length",
          abs((off['time'] - on['time']) - DRUM_HIT_LEN) < 1e-6,
          f"got {off['time'] - on['time']}")


def test_output_has_no_sustain_or_zone_events():
    from arranger import ConversionSettings, convert_drum
    # Drum output should never touch the sustain pedal or octave-shift zones -
    # the game never needs to hold Space or press Shift/Ctrl for a drum beat.
    events = [
        {'time': 0.0, 'type': 'note_on', 'note': 60, 'velocity': 64, 'channel': 0},
        {'time': 0.1, 'type': 'note_off', 'note': 60, 'channel': 0},
        {'time': 0.05, 'type': 'sustain', 'value': True, 'channel': 0},
    ]
    out = convert_drum(events, ConversionSettings(), orig_bpm=120)
    check("no sustain events in drum output",
          all(e['type'] != 'sustain' for e in out), f"got {out}")
    check("no zone events in drum output",
          all(e['type'] != 'zone' for e in out), f"got {out}")


if __name__ == "__main__":
    test_empty_input_is_safe()
    test_melodic_onbeat_alternates_kick_snare()
    test_offbeat_note_becomes_hat()
    test_chord_collapses_to_one_hit()
    test_gm_drum_track_maps_by_bucket()
    test_gm_full_kit_maps_to_distinct_voices()
    test_gm_ride_folds_to_closed_hat()
    test_gm_track_ignores_other_channels()
    test_retrigger_floor_drops_rapid_same_voice_hits()
    test_speed_scales_the_beat_grid()
    test_output_notes_are_short_taps_not_sustained()
    test_output_has_no_sustain_or_zone_events()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
