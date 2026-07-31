import os
import shutil
from copy import deepcopy

from arranger import ConversionSettings
from conversion_profile import make_conversion_profile
from track_preparation import (
    prepare_track,
    prepared_track_matches,
    profile_fingerprint,
    source_signature,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCALE_MIDI = os.path.join(ROOT, "test_1_c_major_scale.mid")


def test_prepared_track_reuses_matching_file_and_profile():
    profile = make_conversion_profile(ConversionSettings(), "Piano")
    prepared = prepare_track(SCALE_MIDI, profile)

    assert prepared["events"]
    assert prepared["channels"]
    assert prepared_track_matches(prepared, SCALE_MIDI, profile)


def test_prepared_track_rejects_changed_conversion_profile():
    profile = make_conversion_profile(ConversionSettings(), "Piano")
    prepared = prepare_track(SCALE_MIDI, profile)
    changed = deepcopy(profile)
    changed["settings"]["speed"] = 1.25

    assert profile_fingerprint(profile) != profile_fingerprint(changed)
    assert not prepared_track_matches(prepared, SCALE_MIDI, changed)


def test_prepared_autoplay_track_applies_octave_doubling():
    plain_profile = make_conversion_profile(ConversionSettings(), "Piano")
    doubled_profile = make_conversion_profile(
        ConversionSettings(double_melody_octave=True), "Piano")

    plain = prepare_track(SCALE_MIDI, plain_profile)
    doubled = prepare_track(SCALE_MIDI, doubled_profile)
    plain_onsets = [event for event in plain["events"]
                    if event["type"] == "note_on"]
    doubled_onsets = [event for event in doubled["events"]
                      if event["type"] == "note_on"]

    assert len(doubled_onsets) == 2 * len(plain_onsets)
    assert prepared_track_matches(doubled, SCALE_MIDI, doubled_profile)
    assert not prepared_track_matches(doubled, SCALE_MIDI, plain_profile)


def test_prepared_track_rejects_changed_source_file(tmp_path):
    copied = tmp_path / "scale.mid"
    shutil.copyfile(SCALE_MIDI, copied)
    profile = make_conversion_profile(ConversionSettings(), "Piano")
    prepared = prepare_track(str(copied), profile)
    old_signature = source_signature(str(copied))

    with copied.open("ab") as handle:
        handle.write(b"\x00")

    assert source_signature(str(copied)) != old_signature
    assert not prepared_track_matches(prepared, str(copied), profile)
