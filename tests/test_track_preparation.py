from copy import deepcopy

import pytest
from mido import Message, MidiFile, MidiTrack

from arranger import ConversionSettings
from conversion_profile import make_conversion_profile
from track_preparation import (
    prepare_track,
    prepared_track_matches,
    profile_fingerprint,
    source_signature,
)


@pytest.fixture
def scale_midi(tmp_path):
    """Create the real MIDI fixture inside pytest's temporary directory."""
    path = tmp_path / "c_major_scale.mid"
    midi = MidiFile()
    track = MidiTrack()
    midi.tracks.append(track)
    for pitch in (60, 62, 64, 65, 67, 69, 71, 72):
        track.append(Message(
            "note_on", note=pitch, velocity=64, channel=0, time=0))
        track.append(Message(
            "note_off", note=pitch, velocity=64, channel=0, time=480))
    midi.save(path)
    return str(path)


def test_prepared_track_reuses_matching_file_and_profile(scale_midi):
    profile = make_conversion_profile(ConversionSettings(), "Piano")
    prepared = prepare_track(scale_midi, profile)

    assert prepared["events"]
    assert prepared["channels"]
    assert prepared_track_matches(prepared, scale_midi, profile)


def test_prepared_track_rejects_changed_conversion_profile(scale_midi):
    profile = make_conversion_profile(ConversionSettings(), "Piano")
    prepared = prepare_track(scale_midi, profile)
    changed = deepcopy(profile)
    changed["settings"]["speed"] = 1.25

    assert profile_fingerprint(profile) != profile_fingerprint(changed)
    assert not prepared_track_matches(prepared, scale_midi, changed)


def test_prepared_autoplay_track_applies_octave_doubling(scale_midi):
    plain_profile = make_conversion_profile(ConversionSettings(), "Piano")
    doubled_profile = make_conversion_profile(
        ConversionSettings(double_melody_octave=True), "Piano")

    plain = prepare_track(scale_midi, plain_profile)
    doubled = prepare_track(scale_midi, doubled_profile)
    plain_onsets = [event for event in plain["events"]
                    if event["type"] == "note_on"]
    doubled_onsets = [event for event in doubled["events"]
                      if event["type"] == "note_on"]

    assert len(doubled_onsets) == 2 * len(plain_onsets)
    assert prepared_track_matches(doubled, scale_midi, doubled_profile)
    assert not prepared_track_matches(doubled, scale_midi, plain_profile)


def test_prepared_track_rejects_changed_source_file(scale_midi):
    profile = make_conversion_profile(ConversionSettings(), "Piano")
    prepared = prepare_track(scale_midi, profile)
    old_signature = source_signature(scale_midi)

    with open(scale_midi, "ab") as handle:
        handle.write(b"\x00")

    assert source_signature(scale_midi) != old_signature
    assert not prepared_track_matches(prepared, scale_midi, profile)
