"""Validated, versioned conversion settings shared by multiplayer hosts."""

from dataclasses import asdict, fields
import math

from arranger import ConversionSettings
from config import INSTRUMENTS


PROFILE_VERSION = 1
_SETTING_NAMES = {field.name for field in fields(ConversionSettings)}
_BOOL_FIELDS = {
    "note_thinning", "cull_low_priority", "prioritize_melody",
    "proportional_remap", "consistent_windows", "voice_aware",
    "phrase_gap_shifting", "melody_lock", "duet_mode", "auto_split",
    "disable_sustain",
}
_INT_RANGES = {
    "max_chord_notes": (1, 5),
    "duet_split_note": (0, 127),
    "auto_split_parts": (2, 3),
    "range_low": (0, 127),
    "range_high": (0, 127),
    "reach_low": (0, 127),
    "reach_high": (0, 127),
    "instrument_offset": (-127, 127),
    "drum_fill_frequency": (0, 32),
}
_FLOAT_RANGES = {
    "speed": (0.1, 4.0),
    "chord_window": (0.0, 1.0),
    "thinning_gap": (0.0, 1.0),
    "thinning_min_len": (0.0, 1.0),
    "phrase_gap": (0.0, 5.0),
    "retrigger_gap": (0.0, 0.5),
    "drum_intensity": (0.25, 2.0),
    "drum_bass_follow": (0.0, 1.0),
    "drum_swing": (0.0, 0.5),
    "drum_quantize": (0.0, 1.0),
    "drum_min_spacing": (0.0, 0.5),
}
_CHOICES = {
    "melody_lock_mode": {"drop", "fold", "hybrid"},
    "drum_source_mode": {"auto", "preserve", "augment", "generate"},
    "drum_style": {"auto", "rock", "pop", "ballad", "dance"},
    "drum_hat_density": {"quarter", "eighth", "sixteenth"},
}


def should_use_host_conversion(
        room_code, is_host, host_profile, use_local_override):
    """Return whether a room participant should apply the host arrangement."""
    return bool(
        room_code and not is_host and host_profile
        and not use_local_override)


def make_conversion_profile(settings, instrument_name):
    """Create a JSON-safe profile from a locally validated setting object."""
    if instrument_name not in INSTRUMENTS:
        raise ValueError("Unknown instrument in conversion profile.")
    profile = {
        "version": PROFILE_VERSION,
        "instrument": instrument_name,
        "settings": asdict(settings),
    }
    # Run our own output through the same boundary used for network input.
    load_conversion_profile(profile)
    return profile


def load_conversion_profile(profile):
    """Validate an untrusted profile and return settings plus instrument name."""
    if not isinstance(profile, dict) or set(profile) != {
            "version", "instrument", "settings"}:
        raise ValueError("Invalid conversion profile.")
    if (isinstance(profile["version"], bool)
            or profile["version"] != PROFILE_VERSION):
        raise ValueError("Unsupported conversion profile version.")
    instrument_name = profile["instrument"]
    if not isinstance(instrument_name, str) or instrument_name not in INSTRUMENTS:
        raise ValueError("Unknown instrument in conversion profile.")
    values = profile["settings"]
    if not isinstance(values, dict) or set(values) != _SETTING_NAMES:
        raise ValueError("Conversion profile settings are incomplete or unsupported.")

    cleaned = {}
    for name, value in values.items():
        if name in _BOOL_FIELDS:
            if not isinstance(value, bool):
                raise ValueError(f"Invalid {name} value.")
            cleaned[name] = value
        elif name in _INT_RANGES:
            low, high = _INT_RANGES[name]
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not low <= value <= high):
                raise ValueError(f"Invalid {name} value.")
            cleaned[name] = value
        elif name in _FLOAT_RANGES:
            low, high = _FLOAT_RANGES[name]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not low <= value <= high):
                raise ValueError(f"Invalid {name} value.")
            cleaned[name] = float(value)
        elif name == "bpm_override":
            if value is None:
                cleaned[name] = None
            elif (isinstance(value, bool) or not isinstance(value, (int, float))
                  or not math.isfinite(value) or not 20 <= value <= 400):
                raise ValueError("Invalid bpm_override value.")
            else:
                cleaned[name] = float(value)
        elif name in _CHOICES:
            if not isinstance(value, str) or value not in _CHOICES[name]:
                raise ValueError(f"Invalid {name} value.")
            cleaned[name] = value
        else:
            raise ValueError(f"Unsupported conversion setting: {name}.")

    instrument = INSTRUMENTS[instrument_name]
    expected = {
        "reach_low": instrument["low"],
        "reach_high": instrument["high"],
        "instrument_offset": instrument["offset"],
    }
    if any(cleaned[name] != value for name, value in expected.items()):
        raise ValueError("Conversion profile does not match its instrument.")
    if cleaned["range_low"] > cleaned["range_high"]:
        raise ValueError("Conversion profile range is reversed.")

    return ConversionSettings(**cleaned), instrument_name
