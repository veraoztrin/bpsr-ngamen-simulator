"""General-MIDI source metadata and conservative instrument classification.

The classifier deliberately reports evidence and confidence rather than
claiming that a MIDI channel is *definitely* a particular instrument.  GM
program numbers are useful hints, but custom banks and mislabeled files are
common in real-world MIDI collections.
"""

GM_FAMILIES = (
    "Piano", "Chromatic Percussion", "Organ", "Guitar",
    "Bass", "Strings", "Ensemble", "Brass",
    "Reed", "Pipe", "Synth Lead", "Synth Pad",
    "Synth Effects", "Ethnic", "Percussive", "Sound Effects",
)

# MIDI APIs use zero-based channels; this is the user-facing MIDI channel 10.
GM_DRUM_CHANNEL = 9
GM_PERCUSSION_LOW = 35
GM_PERCUSSION_HIGH = 81

_NAME_HINTS = (
    ("Drums", ("drum", "percussion", "kit")),
    ("Piano", ("piano", "grand", "upright")),
    ("Bass", ("bass",)),
    ("Guitar", ("guitar",)),
    ("Strings", ("string", "violin", "viola", "cello")),
    ("Brass", ("brass", "trumpet", "trombone", "horn")),
    ("Organ", ("organ",)),
    ("Ensemble", ("ensemble", "choir")),
    ("Synth Lead", ("lead",)),
    ("Synth Pad", ("pad",)),
)


def gm_family_name(program):
    """Return the GM family for a validated program number, or ``None``."""
    if isinstance(program, bool):
        return None
    try:
        value = int(program)
    except (TypeError, ValueError):
        return None
    if not 0 <= value <= 127:
        return None
    return GM_FAMILIES[value // 8]


def is_gm_percussion_note(note):
    """Whether a note is in the standard GM percussion-key span."""
    if isinstance(note, bool):
        return False
    try:
        value = int(note)
    except (TypeError, ValueError):
        return False
    return GM_PERCUSSION_LOW <= value <= GM_PERCUSSION_HIGH


def classify_midi_source(channel=None, program=None, bank_msb=None,
                         bank_lsb=None, track_name="", instrument_name="",
                         note=None):
    """Classify one MIDI source using explicit metadata and conservative hints.

    The returned mapping contains ``family``, ``confidence``, ``is_drum`` and
    a short ``reason`` suitable for diagnostics.  Confidence is one of
    ``high``, ``medium``, ``low`` or ``unknown``.
    """
    text = f"{track_name or ''} {instrument_name or ''}".strip().casefold()
    channel_is_drums = channel == GM_DRUM_CHANNEL
    note_is_percussion = is_gm_percussion_note(note)

    if channel_is_drums and note_is_percussion:
        return {
            "family": "Drums", "confidence": "high", "is_drum": True,
            "reason": "MIDI channel 10 with a GM percussion note",
        }
    if channel_is_drums:
        return {
            "family": "Drums", "confidence": "medium", "is_drum": True,
            "reason": "MIDI channel 10 percussion convention",
        }

    for family, words in _NAME_HINTS:
        if text and any(word in text for word in words):
            return {
                "family": family, "confidence": "medium",
                "is_drum": family == "Drums",
                "reason": "track or instrument name",
            }

    family = gm_family_name(program)
    if family:
        standard_bank = bank_msb in (None, 0) and bank_lsb in (None, 0)
        return {
            "family": family,
            "confidence": "high" if standard_bank else "low",
            "is_drum": False,
            "reason": ("General MIDI program"
                       if standard_bank else "program in a custom bank"),
        }

    return {
        "family": "Unclassified", "confidence": "unknown",
        "is_drum": False, "reason": "no usable instrument metadata",
    }


def guess_channel_instrument(channel, channel_programs):
    """Compatibility helper used by the channel list."""
    result = classify_midi_source(
        channel=channel, program=channel_programs.get(channel))
    return None if result["family"] == "Unclassified" else result["family"]
