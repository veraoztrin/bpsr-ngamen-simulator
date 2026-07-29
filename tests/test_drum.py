# Tests for arranger.convert_drum() - the "Drum" instrument conversion mode.
# Two paths are covered:
#   * a real GM percussion track (channel 9) mapped onto the 9 in-game voices
#   * a melodic MIDI turned into a section-aware, melody-following kit groove
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


def _note_beat(time, dur, note, beat, end_beat, channel=0, velocity=64):
    return [
        {'time': time, 'beat': beat, 'type': 'note_on', 'note': note,
         'velocity': velocity, 'channel': channel},
        {'time': time + dur, 'beat': end_beat, 'type': 'note_off',
         'note': note, 'channel': channel},
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
# Section-aware, melody-following groove path (a melodic MIDI, no drum channel)
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


def test_closed_hats_are_not_sixteenth_spammed():
    from arranger import ConversionSettings, convert_drum, DRUM_HH_CLOSED
    # In a busy section closed hi-hats should sit on the 8th-note grid, not a
    # wall of 16ths. At 180 BPM an 8th is 0.167s and a 16th is 0.083s; the
    # TYPICAL (median) gap between consecutive closed hats must be around an
    # 8th - short 16th flutters as lead-ins are still allowed.
    out = convert_drum(_melody(8, 16, base=79), ConversionSettings(), orig_bpm=180, beats_per_measure=4)
    times = sorted(t for t, n in _hits(out) if n == DRUM_HH_CLOSED)
    gaps = sorted(b - a for a, b in zip(times, times[1:]))
    median_gap = gaps[len(gaps) // 2] if gaps else 0
    check("closed hi-hats sit on the 8th grid, not spammed 16ths", median_gap >= 0.12,
          f"median closed-hat gap {median_gap:.3f}s (8th=0.167, 16th=0.083)")


def test_kick_follows_the_bassline():
    from arranger import ConversionSettings, convert_drum, DRUM_KICK
    # Two 8-bar songs at the same density but different bass rhythms must yield
    # different kick patterns - the kick follows the melody, it isn't a fixed
    # template. Song A: bass on beats. Song B: bass pushed onto the off-beats.
    beat, beats = 0.5, 4
    bar = beat * beats
    def build(offsets):
        evs = []
        for b in range(8):
            for off in offsets:
                evs += _note(b * bar + off, 0.1, 43)  # low note = bass
                evs += _note(b * bar + off, 0.1, 72)  # a high note too (density)
        evs.sort(key=lambda e: e['time'])
        return evs
    a = convert_drum(build([0.0, 1.0, 2.0, 3.0]), ConversionSettings(), orig_bpm=120, beats_per_measure=4)
    b = convert_drum(build([0.25, 1.25, 2.25, 3.25]), ConversionSettings(), orig_bpm=120, beats_per_measure=4)
    ka = sorted(round(t, 3) for t, n in _hits(a) if n == DRUM_KICK)
    kb = sorted(round(t, 3) for t, n in _hits(b) if n == DRUM_KICK)
    check("different basslines produce different kick patterns", ka != kb,
          f"identical kick times ({len(ka)})")


def test_kick_follows_low_voice_not_high_melody():
    from arranger import ConversionSettings, convert_drum, DRUM_KICK
    # Keep the same pitches and note count, but swap which voice is syncopated.
    # Only the low voice should place a kick on the first off-beat (0.25 sec).
    def build(low_offset, high_offset):
        events = []
        for bar in range(4):
            base = bar * 2.0
            for beat in range(4):
                events += _note(base + beat * 0.5 + low_offset, 0.08, 43)
                events += _note(base + beat * 0.5 + high_offset, 0.08, 76)
        return sorted(events, key=lambda event: event['time'])

    settings = ConversionSettings(
        drum_style='ballad', drum_fill_frequency=0)
    low_sync = convert_drum(
        build(0.25, 0.0), settings, orig_bpm=120, beats_per_measure=4)
    high_sync = convert_drum(
        build(0.0, 0.25), settings, orig_bpm=120, beats_per_measure=4)
    low_kicks = {round(t % 2.0, 3) for t, note in _hits(low_sync)
                 if note == DRUM_KICK}
    high_kicks = {round(t % 2.0, 3) for t, note in _hits(high_sync)
                  if note == DRUM_KICK}
    check("kick syncopation follows the low voice, not the high melody",
          0.25 in low_kicks and 0.25 not in high_kicks,
          f"low={sorted(low_kicks)} high={sorted(high_kicks)}")


def test_melody_syncopation_changes_snare_answers():
    from arranger import ConversionSettings, convert_drum, DRUM_SNARE
    def build(melody_offset):
        events = []
        for bar in range(6):
            base = bar * 2.0
            for beat in range(4):
                events += _note(base + beat * 0.5, 0.08, 43)
                events += _note(
                    base + beat * 0.5 + melody_offset, 0.08, 76,
                    velocity=105)
        return sorted(events, key=lambda event: event['time'])

    settings = ConversionSettings(
        drum_style='ballad', drum_fill_frequency=0)
    straight = convert_drum(
        build(0.0), settings, orig_bpm=120, beats_per_measure=4)
    syncopated = convert_drum(
        build(0.25), settings, orig_bpm=120, beats_per_measure=4)
    straight_snares = [t for t, note in _hits(straight)
                       if note == DRUM_SNARE]
    sync_snares = [t for t, note in _hits(syncopated)
                   if note == DRUM_SNARE]
    check("syncopated melody earns extra ghost-snare answers",
          len(sync_snares) > len(straight_snares),
          f"straight={len(straight_snares)} syncopated={len(sync_snares)}")


def test_generated_groove_is_varied_but_repeatable():
    from arranger import ConversionSettings, convert_drum, DRUM_KICK
    events = []
    for bar in range(9):
        base = bar * 2.0
        for beat in range(4):
            events += _note(base + beat * 0.5, 0.08, 43, velocity=90)
            events += _note(base + beat * 0.5, 0.08, 76, velocity=100)
    events.sort(key=lambda event: event['time'])
    settings = ConversionSettings(drum_style='pop', drum_fill_frequency=0)
    first = convert_drum(
        events, settings, orig_bpm=120, beats_per_measure=4)
    second = convert_drum(
        events, settings, orig_bpm=120, beats_per_measure=4)
    kick_shapes = []
    hits = _hits(first)
    for bar in range(9):
        kick_shapes.append(tuple(
            round(t - bar * 2.0, 3)
            for t, note in hits
            if note == DRUM_KICK and bar * 2.0 <= t < (bar + 1) * 2.0
        ))
    check("identical input produces an identical generated performance",
          first == second)
    check("repeated musical bars receive stable groove variations",
          len(set(kick_shapes)) >= 2, f"shapes={kick_shapes}")


def test_note_dynamics_shape_drum_energy():
    from arranger import ConversionSettings, convert_drum
    def build(velocity):
        events = []
        for bar in range(6):
            for slot in range(8):
                events += _note(
                    bar * 2.0 + slot * 0.25, 0.07, 60 + slot % 5,
                    velocity=velocity)
        return sorted(events, key=lambda event: event['time'])

    settings = ConversionSettings(drum_fill_frequency=0)
    soft = convert_drum(
        build(30), settings, orig_bpm=120, beats_per_measure=4)
    loud = convert_drum(
        build(120), settings, orig_bpm=120, beats_per_measure=4)
    check("louder source dynamics raise the generated drum energy",
          len(_hits(loud)) > len(_hits(soft)),
          f"soft={len(_hits(soft))} loud={len(_hits(loud))}")


def test_rest_bars_produce_no_drums():
    from arranger import ConversionSettings, convert_drum
    bar = 0.5 * 4
    events = []
    for i in range(8):
        events += _note(i * (bar / 8), 0.1, 60)
    for i in range(8):
        events += _note(4 * bar + i * (bar / 8), 0.1, 60)
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
    events = (_melody(4, 1, base=46, start_bar=0)
              + _melody(4, 4, base=60, start_bar=4)
              + _melody(4, 18, base=79, start_bar=8)
              + _melody(4, 18, base=60, start_bar=12))
    events.sort(key=lambda e: e['time'])
    out = convert_drum(events, ConversionSettings(), orig_bpm=120, beats_per_measure=4)
    voices = set(n for _, n in _hits(out))
    missing = set(DRUM_NOTES) - voices
    check("all 9 in-game drum voices are used across a varied song",
          not missing, f"missing {sorted(missing)}; used {sorted(voices)}")


def test_fills_are_varied():
    from arranger import (ConversionSettings, convert_drum, DRUM_TOM_1, DRUM_TOM_2,
                          DRUM_FLOOR_TOM, DRUM_SNARE)
    # Over a long busy song there should be several fills, and they should not
    # all be the identical voice sequence - collect the fill "shapes" and check
    # for variety. A fill is a burst of tom/snare hits between backbone hits.
    out = convert_drum(_melody(24, 16, base=72), ConversionSettings(), orig_bpm=120, beats_per_measure=4)
    tom_voices = {DRUM_TOM_1, DRUM_TOM_2, DRUM_FLOOR_TOM}
    seq = [(round(t, 3), n) for t, n in _hits(out) if n in tom_voices or n == DRUM_SNARE]
    # group into fills by time-gap
    fills = []
    cur = []
    for i, (t, n) in enumerate(seq):
        if cur and t - cur[-1][0] > 0.4:
            fills.append(tuple(v for _, v in cur))
            cur = []
        cur.append((t, n))
    if cur:
        fills.append(tuple(v for _, v in cur))
    fills = [f for f in fills if len(f) >= 3]  # real fills, not stray snares
    check("multiple fills occur over a long song", len(fills) >= 3, f"got {len(fills)} fills")
    check("fills are not all identical", len(set(fills)) >= 2,
          f"{len(set(fills))} distinct of {len(fills)} fills")


def test_style_switches_between_sections():
    from arranger import ConversionSettings, convert_drum, DRUM_HH_OPEN
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


def test_pickup_does_not_move_the_bar_grid():
    from arranger import ConversionSettings, convert_drum, DRUM_HH_CLOSED
    events = []
    for index in range(9):
        beat = 0.5 + index
        events += _note_beat(beat * 0.5, 0.1, 60 + beat % 3,
                             float(beat), beat + 0.2)
    events.sort(key=lambda event: event['time'])
    out = convert_drum(
        events, ConversionSettings(drum_hat_density='quarter'),
        orig_bpm=120, beats_per_measure=4)
    hats = [time for time, voice in _hits(out) if voice == DRUM_HH_CLOSED]
    check("a pickup does not redefine where the next bar starts",
          hats and all(abs((time / 0.5) - round(time / 0.5)) < 1e-6
                       for time in hats),
          f"hat times {hats}")


def test_sustained_music_keeps_later_bars_active():
    from arranger import ConversionSettings, convert_drum
    events = _note_beat(0.0, 8.0, 60, 0.0, 16.0)
    out = convert_drum(events, ConversionSettings(), orig_bpm=120)
    later = [event for event in out
             if event['type'] == 'note_on' and event['time'] >= 6.0]
    check("a sustained passage keeps accompaniment active in later bars",
          bool(later), "no hits in the fourth bar")


def test_chord_size_is_not_rhythm_density():
    from arranger import ConversionSettings, convert_drum
    one_note = []
    ten_notes = []
    for beat in (0.0, 4.0, 8.0, 12.0):
        one_note += _note_beat(beat * 0.5, 0.1, 60, beat, beat + 0.2)
        for pitch in range(50, 60):
            ten_notes += _note_beat(
                beat * 0.5, 0.1, pitch, beat, beat + 0.2)
    one_note.sort(key=lambda event: event['time'])
    ten_notes.sort(key=lambda event: event['time'])
    settings = ConversionSettings(drum_fill_frequency=0)
    simple = len(_hits(convert_drum(one_note, settings, orig_bpm=120)))
    chord = len(_hits(convert_drum(ten_notes, settings, orig_bpm=120)))
    check("a large chord counts as one rhythmic onset",
          chord == simple, f"single={simple}, chord={chord}")


def test_drum_source_modes_are_distinct():
    from arranger import ConversionSettings, convert_drum, DRUM_KICK
    source = (_note_beat(0.0, 0.03, 36, 0.0, 0.06, channel=9)
              + _note_beat(2.0, 0.03, 36, 4.0, 4.06, channel=9))
    source.sort(key=lambda event: event['time'])
    preserved = _hits(convert_drum(
        source, ConversionSettings(drum_source_mode='preserve')))
    augmented = _hits(convert_drum(
        source, ConversionSettings(drum_source_mode='augment')))
    generated = _hits(convert_drum(
        source, ConversionSettings(drum_source_mode='generate')))
    check("Preserve keeps the authored percussion",
          preserved == [(0.0, DRUM_KICK), (2.0, DRUM_KICK)],
          f"got {preserved}")
    check("Augment fills missing kit roles", len(augmented) > len(preserved),
          f"preserve={len(preserved)}, augment={len(augmented)}")
    check("Generate replaces the source pattern", generated != preserved,
          f"got {generated}")


def test_tempo_map_controls_generated_hit_timing():
    from arranger import ConversionSettings, convert_drum, DRUM_HH_CLOSED
    events = _note_beat(0.0, 6.0, 60, 0.0, 8.0)
    out = convert_drum(
        events, ConversionSettings(drum_hat_density='quarter'),
        orig_bpm=120,
        tempo_map=[{'beat': 0.0, 'bpm': 120.0},
                   {'beat': 4.0, 'bpm': 60.0}])
    hats = [time for time, voice in _hits(out) if voice == DRUM_HH_CLOSED]
    check("tempo changes move the musical grid in real time",
          3.0 in hats, f"hat times {hats}")


def test_three_four_meter_uses_three_beat_bars():
    from arranger import ConversionSettings, convert_drum, DRUM_CRASH_1
    events = _note_beat(0.0, 3.0, 60, 0.0, 6.0)
    out = convert_drum(
        events, ConversionSettings(drum_hat_density='quarter'),
        orig_bpm=120, beats_per_measure=3,
        time_signature_map=[
            {'beat': 0.0, 'numerator': 3, 'denominator': 4}])
    crashes = [time for time, voice in _hits(out) if voice == DRUM_CRASH_1]
    all_times = [time for time, _voice in _hits(out)]
    check("3/4 conversion stays inside its six-beat source",
          all(time <= 3.0 + 1e-6 for time in all_times), f"times {all_times}")
    check("3/4 starts on its real downbeat", crashes and crashes[0] == 0.0,
          f"crashes {crashes}")


def test_unknown_gm_percussion_is_ignored():
    from arranger import ConversionSettings, convert_drum
    out = convert_drum(
        _note(0.0, 0.03, 10, channel=9),
        ConversionSettings(drum_source_mode='preserve'))
    check("unknown GM percussion does not become a fake hi-hat",
          _hits(out) == [], f"got {_hits(out)}")


def test_user_minimum_spacing_is_honoured():
    from arranger import ConversionSettings, convert_drum
    events = (_note(0.0, 0.02, 36, channel=9)
              + _note(0.1, 0.02, 36, channel=9))
    events.sort(key=lambda event: event['time'])
    out = convert_drum(
        events,
        ConversionSettings(drum_source_mode='preserve',
                           drum_min_spacing=0.15))
    check("custom drum spacing filters physically impossible repeats",
          len(_hits(out)) == 1, f"got {_hits(out)}")


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
        test_closed_hats_are_not_sixteenth_spammed,
        test_kick_follows_the_bassline,
        test_kick_follows_low_voice_not_high_melody,
        test_melody_syncopation_changes_snare_answers,
        test_generated_groove_is_varied_but_repeatable,
        test_note_dynamics_shape_drum_energy,
        test_rest_bars_produce_no_drums,
        test_groove_scales_with_speed,
        test_varied_song_uses_all_nine_voices,
        test_fills_are_varied,
        test_style_switches_between_sections,
        test_pickup_does_not_move_the_bar_grid,
        test_sustained_music_keeps_later_bars_active,
        test_chord_size_is_not_rhythm_density,
        test_drum_source_modes_are_distinct,
        test_tempo_map_controls_generated_hit_timing,
        test_three_four_meter_uses_three_beat_bars,
        test_unknown_gm_percussion_is_ignored,
        test_user_minimum_spacing_is_honoured,
    ]:
        fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
