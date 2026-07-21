# config.py

# VK Codes for SendInput
VK_SPACE = 0x20
VK_LSHIFT = 0xA0
VK_LCONTROL = 0xA2

# Virtual key codes mapping for alphanumeric and symbols
# A-Z are 0x41-0x5A
# 0-9 are 0x30-0x39
# VK_OEM_4 is '[' (0xDB)
# VK_OEM_6 is ']' (0xDD)

KEY_MAP = {
    # Octave 3 (C3 - B3)
    'C3': 0x5A,  # Z
    'C#3': 0x31, # 1
    'D3': 0x58,  # X
    'D#3': 0x32, # 2
    'E3': 0x43,  # C
    'F3': 0x56,  # V
    'F#3': 0x33, # 3
    'G3': 0x42,  # B
    'G#3': 0x34, # 4
    'A3': 0x4E,  # N
    'A#3': 0x35, # 5
    'B3': 0x4D,  # M

    # Octave 4 (C4 - B4)
    'C4': 0x41,  # A
    'C#4': 0x36, # 6
    'D4': 0x53,  # S
    'D#4': 0x37, # 7
    'E4': 0x44,  # D
    'F4': 0x46,  # F
    'F#4': 0x38, # 8
    'G4': 0x47,  # G
    'G#4': 0x39, # 9
    'A4': 0x48,  # H
    'A#4': 0x30, # 0
    'B4': 0x4A,  # J

    # Octave 5 (C5 - B5)
    'C5': 0x51,  # Q
    'C#5': 0x49, # I
    'D5': 0x57,  # W
    'D#5': 0x4F, # O
    'E5': 0x45,  # E
    'F5': 0x52,  # R
    'F#5': 0x50, # P
    'G5': 0x54,  # T
    'G#5': 0xDB, # [
    'A5': 0x59,  # Y
    'A#5': 0xDD, # ]
    'B5': 0x55,  # U
}

# MIDI notes are often 60 = C4. So C3 = 48, C5 = 72.
# We'll map MIDI notes to their string representation.
NOTES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

# Instruments: the playable MIDI-note range that loaded MIDIs are fitted into.
# Selected in the GUI before/while a MIDI is loaded; Piano is the default and
# keeps the current full-keyboard behavior. Notes outside the chosen range are
# octave-folded (or compressed with Proportional remap) to fit.
#   Piano : C2-B6  (the 3-octave keyboard extended by L-Shift / L-Ctrl)
#   Guitar: E2-B4
# low/high  = the instrument's playable range in its own SOUNDING pitches.
# offset    = semitones to ADD to a target note before looking it up in the
#             piano key map. The in-game bass keyboard is the piano layout
#             transposed down 3 octaves (its 'D' key sounds E1 where the
#             piano's D key sounds E4), so to sound bass note P we press the
#             piano key for P+36. Piano/Guitar share the piano pitch mapping,
#             so offset 0.
#             (Confirmed empirically: loading a C-major-scale MIDI on Bass
#             pressed physical keys D/F/G/H/J, which the game itself reported
#             sounding E1/F1/G1/A1/B1 - exactly 36 semitones below what those
#             same keys sound on Piano (E4/F4/G4/A4/B4), not the 24 originally
#             assumed here. Confirmed 24 was wrong before this fix.)
INSTRUMENTS = {
    "Piano":  {"low": 36, "high": 95, "offset": 0},
    "Guitar": {"low": 40, "high": 71, "offset": 0},
    "Bass":   {"low": 28, "high": 47, "offset": 36},
    # Drum isn't a continuous playable range like the others - see DRUM_NOTES
    # below and arranger.convert_drum(). low/high/offset are kept here only
    # so code that generically reads INSTRUMENTS[...] doesn't need a special
    # case just to populate the dropdown / range hint.
    "Drum": {"low": 62, "high": 69, "offset": 0, "is_drum": True},
}

# The in-game "Drum" instrument only produces sound on 3 of its on-screen
# keys - every other key (all black keys, the other 4 white keys in its home
# octave, and its entire upper octave) is silent. Confirmed by frame-by-frame
# analysis of the in-game key-press flash colors on a demo video. Because of
# this, drum conversion doesn't do a 1:1 pitch mapping like the other
# instruments - see arranger.convert_drum().
DRUM_KICK = 62   # D4
DRUM_SNARE = 65  # F4
DRUM_HAT = 69    # A4
DRUM_NOTES = (DRUM_KICK, DRUM_SNARE, DRUM_HAT)

def midi_to_note_name(midi_note):
    octave = (midi_note // 12) - 1
    note = NOTES[midi_note % 12]
    return f"{note}{octave}"

def note_name_to_midi(name):
    """Parse a note name like 'C2', 'F#5', or a raw MIDI number like '60'.
    Returns the MIDI note number, or None if unparseable."""
    name = name.strip().upper()
    if not name:
        return None
    if name.lstrip('-').isdigit():
        return int(name)
    # Split pitch class from octave (octave may be negative, e.g. C-1)
    for i in range(len(name)):
        if name[i].isdigit() or name[i] == '-':
            pitch, octave_str = name[:i], name[i:]
            break
    else:
        return None
    if pitch not in NOTES:
        return None
    try:
        octave = int(octave_str)
    except ValueError:
        return None
    return (octave + 1) * 12 + NOTES.index(pitch)
