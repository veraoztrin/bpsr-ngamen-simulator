import os

import mido

from midi_metadata import (
    GM_DRUM_CHANNEL as GM_DRUM_CHANNEL,
    GM_FAMILIES as GM_FAMILIES,
    classify_midi_source,
    gm_family_name as gm_family_name,
    guess_channel_instrument as guess_channel_instrument,
)

DEFAULT_BPM = 120.0
DEFAULT_BEATS_PER_MEASURE = 4
MAX_LOCAL_MIDI_BYTES = 20 * 1024 * 1024
MAX_MIDI_EVENTS = 500_000
MAX_MIDI_DURATION_SECONDS = 6 * 60 * 60


def _clean_meta_text(value):
    return str(value or "").replace("\x00", "").strip()[:200]


def _scan_tracks(tracks):
    """Collect descriptive metadata without changing playback state."""
    metadata = []
    for track_index, track in enumerate(tracks):
        track_name = ""
        instrument_name = ""
        for msg in track:
            if msg.type == "track_name" and not track_name:
                track_name = _clean_meta_text(getattr(msg, "name", ""))
            elif msg.type == "instrument_name" and not instrument_name:
                instrument_name = _clean_meta_text(
                    getattr(msg, "name", getattr(msg, "text", "")))
        metadata.append({
            "index": track_index,
            "name": track_name,
            "instrument_name": instrument_name,
        })
    return metadata


def _timed_messages(mid):
    """Yield messages with time, beat, source track information and port.

    Mido's normal merged iterator drops track identity.  Keeping it here lets
    the converter offer genuine track-, channel- and instrument-aware groups.
    Lightweight test doubles still use the already-seconds iterator fallback.
    """
    tempo = 500000
    seconds = 0.0
    beat = 0.0
    tracks = getattr(mid, "tracks", None)
    ticks_per_beat = getattr(mid, "ticks_per_beat", None)
    if tracks is not None and ticks_per_beat:
        track_metadata = _scan_tracks(tracks)
        timeline = []
        for track_index, track in enumerate(tracks):
            absolute_tick = 0
            port = 0
            for message_index, msg in enumerate(track):
                absolute_tick += msg.time
                if msg.type == "midi_port":
                    port = max(0, int(getattr(msg, "port", 0)))
                timeline.append((absolute_tick, track_index, message_index,
                                 msg, port))
        timeline.sort(key=lambda item: (item[0], item[1], item[2]))

        last_tick = 0
        for absolute_tick, track_index, _order, msg, port in timeline:
            delta_ticks = absolute_tick - last_tick
            seconds += mido.tick2second(delta_ticks, ticks_per_beat, tempo)
            beat += delta_ticks / ticks_per_beat
            last_tick = absolute_tick
            yield (msg, seconds, beat, track_index,
                   track_metadata[track_index], port)
            if msg.type == "set_tempo" and msg.tempo > 0:
                tempo = msg.tempo
        return

    fallback_meta = {"index": 0, "name": "", "instrument_name": ""}
    for msg in mid:
        delta_seconds = msg.time
        seconds += delta_seconds
        beat += delta_seconds / (tempo / 1_000_000.0)
        yield msg, seconds, beat, 0, fallback_meta, 0
        if msg.type == "set_tempo" and msg.tempo > 0:
            tempo = msg.tempo


def _append_map_point(points, point):
    """Replace a same-beat map event; otherwise append it."""
    if points and abs(points[-1]["beat"] - point["beat"]) < 1e-9:
        points[-1] = point
    else:
        points.append(point)


def _empty_result(tempo_map=None, time_signature_map=None):
    return {
        "events": [],
        "bpm": DEFAULT_BPM,
        "beats_per_measure": DEFAULT_BEATS_PER_MEASURE,
        "channel_programs": {},
        "track_metadata": [],
        "tempo_map": tempo_map or [{"beat": 0.0, "bpm": DEFAULT_BPM}],
        "time_signature_map": time_signature_map or [
            {"beat": 0.0, "numerator": 4, "denominator": 4}],
    }


def _source_fields(msg, track_index, track_meta, port, state, note=None):
    channel = msg.channel
    state_key = (port, channel)
    program = state["program"].get(state_key)
    bank_msb = state["bank_msb"].get(state_key)
    bank_lsb = state["bank_lsb"].get(state_key)
    classification = classify_midi_source(
        channel=channel, program=program, bank_msb=bank_msb,
        bank_lsb=bank_lsb, track_name=track_meta.get("name", ""),
        instrument_name=track_meta.get("instrument_name", ""), note=note)
    return {
        "source_track": track_index,
        "source_track_name": track_meta.get("name", ""),
        "source_instrument_name": track_meta.get("instrument_name", ""),
        "source_port": port,
        "source_channel": channel,
        "program": program,
        "bank_msb": bank_msb,
        "bank_lsb": bank_lsb,
        "source_family": classification["family"],
        "source_confidence": classification["confidence"],
        "source_is_drum": classification["is_drum"],
        "classification_reason": classification["reason"],
    }


def parse_midi_full(file_path):
    """Parse a MIDI while preserving source identity and program metadata."""
    try:
        if (os.path.exists(file_path)
                and os.path.getsize(file_path) > MAX_LOCAL_MIDI_BYTES):
            raise ValueError(
                f"MIDI exceeds the {MAX_LOCAL_MIDI_BYTES // (1024 * 1024)} MiB limit")
        mid = mido.MidiFile(file_path)
    except Exception as exc:
        print(f"Error loading MIDI: {exc}")
        return _empty_result()

    tracks = getattr(mid, "tracks", None)
    track_metadata = _scan_tracks(tracks) if tracks is not None else []
    all_events = []
    bpm = None
    beats_per_measure = None
    channel_programs = {}
    tempo_map = [{"beat": 0.0, "bpm": DEFAULT_BPM}]
    time_signature_map = [
        {"beat": 0.0, "numerator": 4, "denominator": 4}]
    state = {"program": {}, "bank_msb": {}, "bank_lsb": {}}

    for (msg, current_time, current_beat, track_index,
         track_meta, port) in _timed_messages(mid):
        if (len(all_events) >= MAX_MIDI_EVENTS
                or current_time > MAX_MIDI_DURATION_SECONDS):
            print("Error loading MIDI: event-count or duration safety limit exceeded")
            return _empty_result(tempo_map, time_signature_map)

        if msg.type == "set_tempo":
            if bpm is None and msg.tempo > 0:
                bpm = 60000000.0 / msg.tempo
            if msg.tempo > 0:
                _append_map_point(tempo_map, {
                    "beat": current_beat,
                    "bpm": 60000000.0 / msg.tempo,
                })
            continue
        if msg.type == "time_signature":
            if beats_per_measure is None:
                beats_per_measure = msg.numerator
            _append_map_point(time_signature_map, {
                "beat": current_beat,
                "numerator": max(1, int(msg.numerator)),
                "denominator": max(1, int(msg.denominator)),
            })
            continue
        if not hasattr(msg, "channel"):
            continue

        state_key = (port, msg.channel)
        if msg.type == "program_change":
            state["program"][state_key] = msg.program
            channel_programs.setdefault(msg.channel, msg.program)
            continue
        if msg.type == "control_change" and msg.control in (0, 32):
            bank_key = "bank_msb" if msg.control == 0 else "bank_lsb"
            state[bank_key][state_key] = msg.value
            continue

        if msg.type in ("note_on", "note_off"):
            event_type = (
                "note_off" if msg.type == "note_off" or msg.velocity == 0
                else "note_on")
            event = {
                "time": current_time,
                "type": event_type,
                "note": msg.note,
                "channel": msg.channel,
                "beat": current_beat,
            }
            if event_type == "note_on":
                event["velocity"] = msg.velocity
            event.update(_source_fields(
                msg, track_index, track_meta, port, state, note=msg.note))
            all_events.append(event)
        elif msg.type == "control_change" and msg.control == 64:
            event = {
                "time": current_time,
                "type": "sustain",
                "value": msg.value >= 64,
                "channel": msg.channel,
                "beat": current_beat,
            }
            event.update(_source_fields(
                msg, track_index, track_meta, port, state))
            all_events.append(event)

    all_events.sort(key=lambda event: event["time"])

    # Remove only true same-source duplicates.  Identically pitched notes from
    # two separate tracks are musically independent and must both survive.
    filtered_events = []
    last_event_time = {}
    for event in all_events:
        if event["type"] == "note_on":
            key = (event.get("source_port", 0),
                   event.get("source_track", 0),
                   event.get("source_channel", event.get("channel", 0)),
                   event["note"])
            if (key in last_event_time
                    and abs(event["time"] - last_event_time[key]) < 0.002):
                continue
            last_event_time[key] = event["time"]
        filtered_events.append(event)

    return {
        "events": filtered_events,
        "bpm": bpm if bpm is not None else DEFAULT_BPM,
        "beats_per_measure": (
            beats_per_measure if beats_per_measure is not None
            else DEFAULT_BEATS_PER_MEASURE),
        "channel_programs": channel_programs,
        "track_metadata": track_metadata,
        "tempo_map": tempo_map,
        "time_signature_map": time_signature_map,
    }


def parse_midi(file_path):
    """Backward-compatible wrapper returning just the event list."""
    return parse_midi_full(file_path)["events"]


def get_channels_info(events):
    """Return sorted active output channels."""
    return sorted({event["channel"] for event in events
                   if "channel" in event})
