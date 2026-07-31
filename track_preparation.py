"""Background-safe MIDI preparation for playlist autoplay.

The GUI owns playlist state and Tk widgets.  This module deliberately contains
only pure/file operations so parsing and conversion can run on a worker thread
without touching Tk.
"""

import json
import os
from copy import deepcopy

from arranger import convert, convert_drum
from conversion_profile import load_conversion_profile
from midi_parser import get_channels_info, parse_midi_full


def source_signature(path):
    """Return the file identity used to reject stale prepared results."""
    stat = os.stat(path)
    return (
        os.path.normcase(os.path.abspath(path)),
        int(stat.st_size),
        int(stat.st_mtime_ns),
    )


def profile_fingerprint(profile):
    """Stable identity for a validated, JSON-safe conversion profile."""
    # Validation also prevents odd non-JSON values (NaN, custom objects, etc.)
    # from entering the background preparation cache.
    load_conversion_profile(profile)
    return json.dumps(profile, sort_keys=True, separators=(",", ":"))


def prepare_track(path, profile):
    """Parse and convert one MIDI for instant activation by the GUI."""
    settings, instrument_name = load_conversion_profile(profile)
    signature = source_signature(path)
    parsed = parse_midi_full(path)
    raw_events = parsed.get("events", [])
    instrument_is_drum = instrument_name == "Drum"
    if instrument_is_drum:
        events = convert_drum(
            raw_events,
            settings,
            orig_bpm=parsed.get("bpm", 120.0),
            beats_per_measure=parsed.get("beats_per_measure", 4),
            tempo_map=parsed.get("tempo_map"),
            time_signature_map=parsed.get("time_signature_map"),
        )
    else:
        events = convert(
            raw_events, settings, orig_bpm=parsed.get("bpm", 120.0))
    return {
        "source_signature": signature,
        "profile_fingerprint": profile_fingerprint(profile),
        "profile": deepcopy(profile),
        "parsed": parsed,
        "events": events,
        "channels": get_channels_info(events),
    }


def prepared_track_matches(prepared, path, profile):
    """Whether a cached result still matches both its file and settings."""
    if not isinstance(prepared, dict):
        return False
    try:
        return (
            prepared.get("source_signature") == source_signature(path)
            and prepared.get("profile_fingerprint")
            == profile_fingerprint(profile)
        )
    except (OSError, TypeError, ValueError):
        return False
