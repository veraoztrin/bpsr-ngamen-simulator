import mido
import os

DEFAULT_BPM = 120.0
DEFAULT_BEATS_PER_MEASURE = 4
MAX_LOCAL_MIDI_BYTES = 20 * 1024 * 1024
MAX_MIDI_EVENTS = 500_000
MAX_MIDI_DURATION_SECONDS = 6 * 60 * 60


def _timed_messages(mid):
    """Yield (message, absolute_seconds, absolute_quarter_note_beats).

    Real MidiFile objects expose tracks and ticks_per_beat, which lets drum
    conversion retain the musical grid through tempo changes. Lightweight test
    doubles fall back to mido's already-seconds iterator.
    """
    tempo = 500000
    seconds = 0.0
    beat = 0.0
    tracks = getattr(mid, "tracks", None)
    ticks_per_beat = getattr(mid, "ticks_per_beat", None)
    if tracks is not None and ticks_per_beat:
        for msg in mido.merge_tracks(tracks):
            delta_ticks = msg.time
            seconds += mido.tick2second(delta_ticks, ticks_per_beat, tempo)
            beat += delta_ticks / ticks_per_beat
            yield msg, seconds, beat
            if msg.type == "set_tempo" and msg.tempo > 0:
                tempo = msg.tempo
        return

    for msg in mid:
        delta_seconds = msg.time
        seconds += delta_seconds
        beat += delta_seconds / (tempo / 1_000_000.0)
        yield msg, seconds, beat
        if msg.type == "set_tempo" and msg.tempo > 0:
            tempo = msg.tempo


def _append_map_point(points, point):
    """Replace a same-beat map event; otherwise append it."""
    if points and abs(points[-1]["beat"] - point["beat"]) < 1e-9:
        points[-1] = point
    else:
        points.append(point)

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
        if (os.path.exists(file_path)
                and os.path.getsize(file_path) > MAX_LOCAL_MIDI_BYTES):
            raise ValueError(
                f"MIDI exceeds the {MAX_LOCAL_MIDI_BYTES // (1024 * 1024)} MiB limit")
        mid = mido.MidiFile(file_path)
    except Exception as e:
        print(f"Error loading MIDI: {e}")
        return {
            'events': [], 'bpm': DEFAULT_BPM,
            'beats_per_measure': DEFAULT_BEATS_PER_MEASURE,
            'tempo_map': [{'beat': 0.0, 'bpm': DEFAULT_BPM}],
            'time_signature_map': [
                {'beat': 0.0, 'numerator': 4, 'denominator': 4}],
        }

    all_events = []
    current_time = 0.0
    bpm = None
    beats_per_measure = None
    channel_programs = {}  # channel -> first Program Change number seen (GM patch 0-127)
    tempo_map = [{'beat': 0.0, 'bpm': DEFAULT_BPM}]
    time_signature_map = [
        {'beat': 0.0, 'numerator': 4, 'denominator': 4}]

    # Iterating over MidiFile yields messages in exact chronological playback order.
    # msg.time is the delta time in seconds since the last yielded message.
    for msg, current_time, current_beat in _timed_messages(mid):
        if (len(all_events) >= MAX_MIDI_EVENTS
                or current_time > MAX_MIDI_DURATION_SECONDS):
            print("Error loading MIDI: event-count or duration safety limit exceeded")
            return {
                'events': [], 'bpm': DEFAULT_BPM,
                'beats_per_measure': DEFAULT_BEATS_PER_MEASURE,
                'channel_programs': {},
                'tempo_map': tempo_map,
                'time_signature_map': time_signature_map,
            }

        if msg.type == 'set_tempo':
            # Remember the FIRST tempo as the song's nominal BPM.
            if bpm is None and msg.tempo > 0:
                bpm = 60000000.0 / msg.tempo
            if msg.tempo > 0:
                _append_map_point(tempo_map, {
                    'beat': current_beat,
                    'bpm': 60000000.0 / msg.tempo,
                })

        elif msg.type == 'time_signature':
            if beats_per_measure is None:
                beats_per_measure = msg.numerator
            _append_map_point(time_signature_map, {
                'beat': current_beat,
                'numerator': max(1, int(msg.numerator)),
                'denominator': max(1, int(msg.denominator)),
            })

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
                    'channel': msg.channel,
                    'beat': current_beat,
                })
            else:
                all_events.append({
                    'time': current_time,
                    'type': 'note_on',
                    'note': msg.note,
                    'velocity': msg.velocity,
                    'channel': msg.channel,
                    'beat': current_beat,
                })

        elif msg.type == 'note_off':
            all_events.append({
                'time': current_time,
                'type': 'note_off',
                'note': msg.note,
                'channel': msg.channel,
                'beat': current_beat,
            })

        elif msg.type == 'control_change' and hasattr(msg, 'control') and msg.control == 64:
            # CC 64 is Sustain Pedal
            # >= 64 is ON, < 64 is OFF
            is_on = msg.value >= 64
            all_events.append({
                'time': current_time,
                'type': 'sustain',
                'value': is_on,
                'channel': msg.channel,
                'beat': current_beat,
            })

    # Ensure events are sorted
    all_events.sort(key=lambda x: x['time'])

    # Anti-Stack Filter: Remove near-simultaneous duplicate note_on events.
    # Never filter note_off: two overlapping same-pitch notes need two
    # releases, even if those releases are only 1 ms apart.
    # This prevents sending redundant keystrokes to the OS for poorly quantized chords
    filtered_events = []
    last_event_time = {}  # (type, channel, note) -> time

    for ev in all_events:
        if ev['type'] == 'note_on':
            key = (ev.get('channel', 0), ev['note'])
            if (key in last_event_time
                    and abs(ev['time'] - last_event_time[key]) < 0.002):
                continue  # Skip stacked duplicate
            last_event_time[key] = ev['time']

        filtered_events.append(ev)

    return {
        'events': filtered_events,
        'bpm': bpm if bpm is not None else DEFAULT_BPM,
        'beats_per_measure': beats_per_measure if beats_per_measure is not None else DEFAULT_BEATS_PER_MEASURE,
        'channel_programs': channel_programs,
        'tempo_map': tempo_map,
        'time_signature_map': time_signature_map,
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
