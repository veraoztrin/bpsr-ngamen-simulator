from dataclasses import asdict

import pytest

from arranger import ConversionSettings, convert
from conversion_profile import (
    make_conversion_profile, load_conversion_profile,
    should_use_host_conversion,
)


def test_conversion_profile_round_trip():
    original = ConversionSettings(
        speed=1.25, prioritize_melody=True, double_melody_octave=True,
        auto_split=True,
        auto_split_parts=3, range_low=36, range_high=95)
    profile = make_conversion_profile(original, "Piano")
    restored, instrument = load_conversion_profile(profile)

    assert instrument == "Piano"
    assert asdict(restored) == asdict(original)


@pytest.mark.parametrize("mutation", [
    lambda profile: profile.update(version=True),
    lambda profile: profile.update(instrument=[]),
    lambda profile: profile["settings"].update(speed=float("nan")),
    lambda profile: profile["settings"].update(reach_low=0),
    lambda profile: profile["settings"].update(grouping_mode="guess"),
    lambda profile: profile["settings"].update(double_melody_octave=1),
    lambda profile: profile["settings"].update(extra=True),
])
def test_conversion_profile_rejects_malformed_values(mutation):
    profile = make_conversion_profile(ConversionSettings(), "Piano")
    mutation(profile)
    with pytest.raises(ValueError):
        load_conversion_profile(profile)


def test_host_profile_produces_identical_client_arrangement():
    events = [
        {"time": 0.0, "type": "note_on", "note": 48,
         "velocity": 70, "channel": 5},
        {"time": 0.0, "type": "note_on", "note": 72,
         "velocity": 90, "channel": 5},
        {"time": 0.5, "type": "note_off", "note": 48, "channel": 5},
        {"time": 0.5, "type": "note_off", "note": 72, "channel": 5},
    ]
    host_settings = ConversionSettings(
        auto_split=True, auto_split_parts=2,
        prioritize_melody=True, double_melody_octave=True,
        range_low=36, range_high=95)
    profile = make_conversion_profile(host_settings, "Piano")
    client_settings, _instrument = load_conversion_profile(profile)

    host_output = convert(events, host_settings)

    assert host_output == convert(events, client_settings)
    assert client_settings.double_melody_octave


def test_client_conversion_defaults_to_host_but_allows_local_override():
    profile = make_conversion_profile(ConversionSettings(), "Piano")

    assert should_use_host_conversion(
        "room", False, profile, use_local_override=False)
    assert not should_use_host_conversion(
        "room", False, profile, use_local_override=True)
    assert not should_use_host_conversion(
        "room", True, profile, use_local_override=False)
    assert not should_use_host_conversion(
        None, False, profile, use_local_override=False)
