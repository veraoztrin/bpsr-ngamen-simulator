from arranger import ConversionSettings, convert


def _note(start, end, pitch, velocity=80, channel=0):
    return [
        {"time": start, "type": "note_on", "note": pitch,
         "velocity": velocity, "channel": channel},
        {"time": end, "type": "note_off", "note": pitch,
         "channel": channel},
    ]


def _note_ons(events):
    return [event for event in events if event["type"] == "note_on"]


def _pitches(events):
    return sorted(event["note"] for event in _note_ons(events))


def test_single_piano_melody_prefers_lower_octave_copy():
    events = _note(0.0, 1.0, 72, velocity=91, channel=3)
    output = convert(
        events, ConversionSettings(double_melody_octave=True))
    notes = _note_ons(output)

    assert [note["note"] for note in notes] == [60, 72]
    assert {note["velocity"] for note in notes} == {91}
    assert {note["channel"] for note in notes} == {3}
    assert {note["time"] for note in notes} == {0.0}
    assert {event["time"] for event in output
            if event["type"] == "note_off"} == {1.0}


def test_low_boundary_uses_upper_octave_and_narrow_range_stays_single():
    events = _note(0.0, 1.0, 36)
    doubled = convert(
        events, ConversionSettings(double_melody_octave=True))
    narrow = convert(events, ConversionSettings(
        double_melody_octave=True, range_low=36, range_high=47))

    assert _pitches(doubled) == [36, 48]
    assert _pitches(narrow) == [36]


def test_chords_existing_octaves_and_max_one_are_not_doubled():
    chord = _note(0.0, 1.0, 60) + _note(0.0, 1.0, 64)
    octave_pair = _note(0.0, 1.0, 60) + _note(0.0, 1.0, 72)
    single = _note(0.0, 1.0, 72)

    assert _pitches(convert(
        chord, ConversionSettings(double_melody_octave=True))) == [60, 64]
    assert _pitches(convert(
        octave_pair,
        ConversionSettings(double_melody_octave=True))) == [60, 72]
    assert _pitches(convert(single, ConversionSettings(
        double_melody_octave=True, max_chord_notes=1))) == [72]


def test_role_grouping_only_doubles_the_single_detected_melody():
    events = (
        _note(0.0, 1.0, 48, channel=4)
        + _note(0.0, 1.0, 64, channel=5)
        + _note(0.0, 1.0, 75, channel=6)
    )
    output = convert(events, ConversionSettings(
        double_melody_octave=True,
        auto_split=True,
        grouping_mode="roles",
        auto_split_parts=3,
    ))
    melody = [event for event in _note_ons(output)
              if event.get("group_name") == "Melody"]

    assert sorted(event["note"] for event in melody) == [63, 75]
    assert {event["channel"] for event in melody} == {0}
    assert len(_note_ons(output)) == 4


def test_role_doubling_respects_total_chord_capacity():
    events = (
        _note(0.0, 1.0, 48)
        + _note(0.0, 1.0, 64)
        + _note(0.0, 1.0, 75)
    )
    output = convert(events, ConversionSettings(
        double_melody_octave=True,
        auto_split=True,
        grouping_mode="roles",
        auto_split_parts=3,
        max_chord_notes=3,
    ))

    assert len(_note_ons(output)) == 3


def test_duet_octave_copy_stays_with_original_melody_part():
    events = _note(0.0, 1.0, 64)
    output = convert(events, ConversionSettings(
        double_melody_octave=True,
        duet_mode=True,
        duet_split_note=60,
    ))

    assert _pitches(output) == [52, 64]
    assert {event["channel"] for event in _note_ons(output)} == {1}


def test_phrase_zone_never_leaves_a_collapsed_duplicate_key():
    events = (
        _note(0.0, 0.08, 60)
        + _note(0.1, 0.18, 90)
        + _note(0.2, 0.28, 91)
    )
    output = convert(events, ConversionSettings(
        double_melody_octave=True,
        phrase_gap_shifting=True,
    ))
    first_onset = [event["note"] for event in _note_ons(output)
                   if event["time"] == 0.0]

    assert first_onset == [60]
    assert len({(event["time"], event["note"], event["channel"])
                for event in _note_ons(output)}) == len(_note_ons(output))


def test_zero_width_consistent_windows_groups_exact_onsets_safely():
    output = convert(_note(0.0, 1.0, 72), ConversionSettings(
        double_melody_octave=True,
        consistent_windows=True,
        chord_window=0.0,
    ))

    assert _pitches(output) == [60, 72]


def test_non_piano_instrument_cannot_enable_octave_doubling():
    events = _note(0.0, 1.0, 60)
    guitar = ConversionSettings(
        double_melody_octave=True,
        reach_low=40,
        reach_high=71,
        range_low=40,
        range_high=71,
    )

    assert _pitches(convert(events, guitar)) == [60]
