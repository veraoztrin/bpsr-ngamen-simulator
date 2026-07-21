import mido

DEFAULT_BPM = 120.0
DEFAULT_BEATS_PER_MEASURE = 4

# Standard General MIDI Level 1 program families. GM groups its 128 patches
# into 16 families of 8 consecutive program numbers each - e.g. programs
# 0-7 are all pianos, 24-31 are all guitars, etc. We label channels with the
# family name (e.g. "Piano", "Guitar") rather than the exact patch name
# (e.g. "Acoustic Grand Piano") since the family is what's useful at a
# glance in the channel list, and is right far more often than a wrong
# specific guess would be misleading.
GM_FAMILIES = [
    "Piano", "Chromatic Percussion", "Organ", "Guitar",
    "Bass", "Strings", "Ensemble", "Brass",
    "Reed", "Pipe", "Synth Lead", "Synth Pad",
    "Synth Effects", "Ethnic", "Percussive", "Sound Effects",
]

# GM channel 9 (0-indexed, i.e. "channel 10" in 1-indexed MIDI terminology)
# is reserved for percussion/drum kits regardless of any program change sent
# on it - this convention is already relied on elsewhere (see
# convert_drum's has_gm_drum_track check in arranger.py).
GM_DRUM_CHANNEL = 9


def gm_family_name(program):
    """Map a GM program number (0-127) to its instrument family name."""
    idx = max(0, min(int(program) // 8, len(GM_FAMILIES) - 1))
    return GM_FAMILIES[idx]


def guess_channel_instrument(channel, channel_programs):
    """
    Best-effort instrument family guess for a channel, using the GM
    percussion-channel convention and any Program Change message seen on
    that channel. Returns None when there's nothing to go on (no program
    change was ever sent and it isn't the drum channel) - callers should
    fall back to a plain "Channel N" label in that case rather than
    guessing from note range alone, which is unreliable.
    """
    if channel == GM_DRUM_CHANNEL:
        return "Drums"
    program = channel_programs.get(channel)
    if program is not None:
        return gm_family_name(program)
    return None


def parse_midi_full(file_path):
    """
    Parses a MIDI file and returns a dict:
      {
        'events': [...],            # sorted event dicts (see below)
        'bpm': float,               # first tempo found (or 120.0)
        'beats_per_measure': int,   # first time signature numerator (or 4)
      }
    Events are dicts: {'time': float, 'type': str, 'note': int, 'velocity': int, 'channel': int, 'value': int}
    Types can be 'note_on', 'note_off', 'sustain'
    """
    try:
        mid = mido.MidiFile(file_path)
    except Exception as e:
        print(f"Error loading MIDI: {e}")
        return {'events': [], 'bpm': DEFAULT_BPM, 'beats_per_measure': DEFAULT_BEATS_PER_MEASURE}

    all_events = []
    current_time = 0.0
    bpm = None
    beats_per_measure = None
    channel_programs = {}  # channel -> first Program Change number seen (GM patch 0-127)

    # Iterating over MidiFile yields messages in exact chronological playback order.
    # msg.time is the delta time in seconds since the last yielded message.
    for msg in mid:
        current_time += msg.time

        if msg.type == 'set_tempo':
            # Remember the FIRST tempo as the song's nominal BPM.
            if bpm is None and msg.tempo > 0:
                bpm = 60000000.0 / msg.tempo

        elif msg.type == 'time_signature':
            if beats_per_measure is None:
                beats_per_measure = msg.numerator

        elif msg.type == 'program_change':
            # Keep the first patch a channel is set to - some songs re-send
            # program changes mid-track (e.g. patch swaps) and the initial
            # one is the best representative "what instrument is this" guess.
            if msg.channel not in channel_programs:
                channel_programs[msg.channel] = msg.program

        elif msg.type == 'note_on':
            # note_on with velocity 0 is often used as note_off
            if msg.velocity == 0:
                all_events.append({
                    'time': current_time,
                    'type': 'note_off',
                    'note': msg.note,
                    'channel': msg.channel
                })
            else:
                all_events.append({
                    'time': current_time,
                    'type': 'note_on',
                    'note': msg.note,
                    'velocity': msg.velocity,
                    'channel': msg.channel
                })

        elif msg.type == 'note_off':
            all_events.append({
                'time': current_time,
                'type': 'note_off',
                'note': msg.note,
                'channel': msg.channel
            })

        elif msg.type == 'control_change' and hasattr(msg, 'control') and msg.control == 64:
            # CC 64 is Sustain Pedal
            # >= 64 is ON, < 64 is OFF
            is_on = msg.value >= 64
            all_events.append({
                'time': current_time,
                'type': 'sustain',
                'value': is_on,
                'channel': msg.channel
            })

    # Ensure events are sorted
    all_events.sort(key=lambda x: x['time'])

    # Anti-Stack Filter: Remove duplicate note_on events occurring at the exact same time
    # This prevents sending redundant keystrokes to the OS for poorly quantized chords
    filtered_events = []
    last_event_time = {}  # (type, channel, note) -> time

    for ev in all_events:
        if ev['type'] in ('note_on', 'note_off'):
            key = (ev['type'], ev.get('channel', 0), ev['note'])
            if key in last_event_time and abs(ev['time'] - last_event_time[key]) < 0.002:
                continue  # Skip stacked duplicate
            last_event_time[key] = ev['time']

        filtered_events.append(ev)

    return {
        'events': filtered_events,
        'bpm': bpm if bpm is not None else DEFAULT_BPM,
        'beats_per_measure': beats_per_measure if beats_per_measure is not None else DEFAULT_BEATS_PER_MEASURE,
        'channel_programs': channel_programs,
    }


def parse_midi(file_path):
    """Backward-compatible wrapper returning just the event list."""
    return parse_midi_full(file_path)['events']


def get_channels_info(events):
    """
    Returns a sorted list of active channels in the parsed events.
    """
    channels = set()
    for ev in events:
        if 'channel' in ev:
            channels.add(ev['channel'])
    return sorted(list(channels))
