from arranger import ConversionSettings, convert
from midi_metadata import classify_midi_source
import midi_parser


def _note(start, end, pitch, channel=0, **metadata):
    common = {"channel": channel, **metadata}
    return [
        {"time": start, "type": "note_on", "note": pitch,
         "velocity": 80, **common},
        {"time": end, "type": "note_off", "note": pitch, **common},
    ]


def _ons(events):
    return [event for event in events if event["type"] == "note_on"]


def test_parser_preserves_tracks_programs_banks_and_overlapping_notes(
        monkeypatch):
    class Message:
        def __init__(self, kind, time=0, **values):
            self.type = kind
            self.time = time
            for key, value in values.items():
                setattr(self, key, value)

    class Midi:
        ticks_per_beat = 480
        tracks = [
            [Message("set_tempo", tempo=500000)],
            [
                Message("track_name", name="Grand"),
                Message("instrument_name", name="Acoustic Piano"),
                Message("program_change", channel=0, program=0),
                Message("note_on", channel=0, note=60, velocity=80),
                Message("note_off", channel=0, note=60, time=480),
            ],
            [
                Message("track_name", name="Violin"),
                Message("control_change", channel=0, control=0, value=1),
                Message("program_change", channel=0, program=40),
                Message("note_on", channel=0, note=60, velocity=90),
                Message("note_off", channel=0, note=60, time=480),
            ],
        ]

    monkeypatch.setattr(midi_parser.mido, "MidiFile", lambda _path: Midi())
    monkeypatch.setattr(
        midi_parser.mido, "tick2second",
        lambda ticks, ticks_per_beat, tempo:
            ticks * tempo / 1_000_000 / ticks_per_beat,
        raising=False)
    parsed = midi_parser.parse_midi_full("same-channel.mid")
    notes = _ons(parsed["events"])

    assert len(notes) == 2
    assert {event["source_track"] for event in notes} == {1, 2}
    assert {event["source_track_name"] for event in notes} == {
        "Grand", "Violin"}
    assert {event["program"] for event in notes} == {0, 40}
    assert next(event for event in notes if event["source_track"] == 2)[
        "bank_msb"] == 1


def test_classifier_reports_evidence_and_never_claims_unknown_is_piano():
    piano = classify_midi_source(channel=0, program=0)
    custom_piano = classify_midi_source(channel=0, program=0, bank_msb=2)
    drums = classify_midi_source(channel=9, note=36)
    unknown = classify_midi_source(channel=2)

    assert (piano["family"], piano["confidence"]) == ("Piano", "high")
    assert custom_piano["family"] == "Piano"
    assert custom_piano["confidence"] == "low"
    assert drums["is_drum"] and drums["confidence"] == "high"
    assert unknown["family"] == "Unclassified"
    assert unknown["confidence"] == "unknown"


def test_roles_are_assigned_before_range_folding_and_drums_are_excluded():
    events = []
    events += _note(0, 1, 36, channel=1)
    events += _note(0, 1, 60, channel=2)
    events += _note(0, 1, 84, channel=3)
    events += _note(0, 0.2, 38, channel=9, source_channel=9,
                    source_is_drum=True, source_family="Drums")

    output = convert(events, ConversionSettings(
        auto_split=True, auto_split_parts=3,
        grouping_mode="roles", range_low=60, range_high=60))
    by_original = {event["original_note"]: event for event in _ons(output)}

    assert {event["note"] for event in _ons(output)} == {60}
    assert by_original[84]["group_name"] == "Melody"
    assert by_original[60]["group_name"] == "Harmony"
    assert by_original[36]["group_name"] == "Bass"
    assert by_original[38]["group_is_drum"] is True
    assert by_original[38]["channel"] not in {
        by_original[84]["channel"], by_original[60]["channel"],
        by_original[36]["channel"],
    }


def test_drum_reference_does_not_distort_pitch_remap_or_chord_limit():
    events = []
    events += _note(0, 1, 60, channel=0)
    events += _note(0, 1, 72, channel=0)
    events += _note(0, 0.2, 36, channel=9, source_channel=9,
                    source_is_drum=True, source_family="Drums")
    remapped = convert(events, ConversionSettings(
        auto_split=True, grouping_mode="roles", proportional_remap=True,
        range_low=60, range_high=72))
    by_original = {event["original_note"]: event for event in _ons(remapped)}
    assert by_original[60]["note"] == 60
    assert by_original[72]["note"] == 72

    limited = convert(events, ConversionSettings(
        auto_split=True, grouping_mode="roles", max_chord_notes=1,
        prioritize_melody=True))
    notes = _ons(limited)

    assert any(event["original_note"] == 72 and event["note"] == 72
               for event in notes)
    assert any(event["original_note"] == 36 and event["group_is_drum"]
               for event in notes)


def test_original_track_grouping_separates_tracks_sharing_a_midi_channel():
    events = []
    events += _note(0, 1, 60, source_track=1,
                    source_track_name="Piano", source_channel=0)
    events += _note(0, 1, 67, source_track=2,
                    source_track_name="Strings", source_channel=0)
    output = convert(events, ConversionSettings(
        auto_split=True, grouping_mode="tracks"))

    notes = _ons(output)
    assert {event["channel"] for event in notes} == {0, 1}
    assert {event["group_name"] for event in notes} == {"Piano", "Strings"}


def test_channel_and_family_grouping_use_preserved_source_metadata():
    events = []
    events += _note(0, 1, 60, channel=7, source_channel=7,
                    source_family="Piano", source_confidence="high")
    events += _note(0, 1, 48, channel=2, source_channel=2,
                    source_family="Bass", source_confidence="high")

    by_channel = convert(events, ConversionSettings(
        auto_split=True, grouping_mode="channels"))
    assert {event["group_name"] for event in _ons(by_channel)} == {
        "MIDI Ch. 8", "MIDI Ch. 3"}

    by_family = convert(events, ConversionSettings(
        auto_split=True, grouping_mode="families"))
    assert {event["group_name"] for event in _ons(by_family)} == {
        "Piano", "Bass"}


def test_source_group_sustain_follows_only_its_source_group():
    events = _note(0, 1, 60, channel=4, source_channel=4)
    events.append({
        "time": 0.2, "type": "sustain", "value": True,
        "channel": 4, "source_channel": 4,
    })
    output = convert(events, ConversionSettings(
        auto_split=True, grouping_mode="channels"))
    sustains = [event for event in output if event["type"] == "sustain"]

    assert len(sustains) == 1
    assert sustains[0]["channel"] == 0
    assert sustains[0]["group_name"] == "MIDI Ch. 5"


def test_grouping_uses_all_16_midi_channels_then_merges_only_real_overflow():
    sixteen = []
    for track in range(16):
        sixteen += _note(track * 0.01, 1, 60 + track % 12,
                         source_track=track,
                         source_track_name=f"Track {track + 1}")
    output = convert(sixteen, ConversionSettings(
        auto_split=True, grouping_mode="tracks"))
    assert {event["channel"] for event in _ons(output)} == set(range(16))
    assert "Track 16" in {event["group_name"] for event in _ons(output)}

    seventeen = sixteen + _note(
        0.2, 1, 77, source_track=16, source_track_name="Track 17")
    overflow = convert(seventeen, ConversionSettings(
        auto_split=True, grouping_mode="tracks"))
    channel_15_names = {
        event["group_name"] for event in _ons(overflow)
        if event["channel"] == 15}
    assert channel_15_names == {"Other sources"}
