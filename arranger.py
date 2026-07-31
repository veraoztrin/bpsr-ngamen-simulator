# arranger.py
#
# MIDI conversion / arrangement pipeline for BPSR playback.
#
# Takes the raw parsed event list from midi_parser and re-transcribes it
# according to user settings (speed, chord limiting, range remapping, etc).
# The player then plays the transformed events unchanged, so every feature
# here works with solo AND multiplayer playback.
#
# All transforms operate on a "notes" representation (paired note_on/note_off)
# which is much easier to reason about than raw events, then get re-emitted
# as an event list at the end.

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

# The nine fixed in-game drum voices are defined once in config (the actual
# keys the game's Drum instrument sounds); import them here so the drum
# conversion and its GM-percussion table reference a single source of truth.
from config import (
    DRUM_HH_CLOSED, DRUM_KICK, DRUM_FLOOR_TOM, DRUM_SNARE,
    DRUM_TOM_1, DRUM_TOM_2, DRUM_CRASH_1, DRUM_HH_OPEN, DRUM_CRASH_2,
    DRUM_NOTES as _DRUM_NOTES,
)
from midi_metadata import (
    GM_DRUM_CHANNEL, classify_midi_source, is_gm_percussion_note,
)

# Kept as a public compatibility export for tests and external callers.
DRUM_NOTES = _DRUM_NOTES

# Playable zones for the BPSR 3-octave keyboard + octave modifiers.
# zone 0  : no modifier    -> MIDI 48..83  (C3..B5)
# zone +1 : L Shift held   -> MIDI 60..95  (C4..B7 sounding)
# zone -1 : L Ctrl held    -> MIDI 36..71  (C2..B4 sounding)
ZONE_RANGES = {0: (48, 83), 1: (60, 95), -1: (36, 71)}
ABS_LOW = 36   # C2 - lowest reachable note (piano)
ABS_HIGH = 95  # B6 - highest reachable note (piano)


@dataclass
class ConversionSettings:
    bpm_override: float = None      # None = keep original tempo
    speed: float = 1.0              # playback speed multiplier (0.5 = half speed)
    max_chord_notes: int = 5        # 1..5 simultaneous new notes per chord
    note_thinning: bool = False     # merge machine-gun repeats / drop micro-notes
    cull_low_priority: bool = False # drop quiet notes inside dense chords
    prioritize_melody: bool = False # always keep the highest voice when trimming
    double_melody_octave: bool = False # thicken single Piano melody notes by 1 octave
    proportional_remap: bool = False# compress full pitch span into allowed range
    consistent_windows: bool = False# fixed-grid chord windows instead of greedy
    voice_aware: bool = False       # octave-fold toward each channel's register
    phrase_gap_shifting: bool = False# only change octave zones between phrases
    melody_lock: bool = False       # lock octave shift to the melody; drop conflicts
    melody_lock_mode: str = 'drop'  # 'drop' | 'fold' | 'hybrid'
    duet_mode: bool = False         # split into Low/High parts (channels 0/1)
    duet_split_note: int = 60       # notes below this go to the Low part
    auto_split: bool = False        # auto-assign channels by musical role
    auto_split_parts: int = 2       # 2 = melody+accomp, 3 = melody+harmony+bass
    grouping_mode: str = 'roles'    # roles | tracks | channels | families
    disable_sustain: bool = False   # strip all pedal events (hold Space manually)
    range_low: int = ABS_LOW        # allowed output range (folded into)
    range_high: int = ABS_HIGH
    reach_low: int = ABS_LOW        # instrument's physical reach (fold floor/ceiling)
    reach_high: int = ABS_HIGH
    instrument_offset: int = 0      # !=0 => non-piano keyboard; skip piano zone logic
    chord_window: float = 0.030     # seconds; notes within this = one chord
    thinning_gap: float = 0.030     # min silence between same-pitch repeats
    thinning_min_len: float = 0.020 # drop notes shorter than this when thinning
    phrase_gap: float = 0.150       # silence >= this = phrase boundary
    retrigger_gap: float = 0.025    # min key-UP time before a key is re-pressed
    drum_source_mode: str = 'auto'  # auto | preserve | augment | generate
    drum_style: str = 'auto'        # auto | rock | pop | ballad | dance
    drum_intensity: float = 1.0     # 0.25..2.0
    drum_fill_frequency: int = 8    # fallback fill every N bars; 0 disables
    drum_hat_density: str = 'eighth'# quarter | eighth | sixteenth
    drum_bass_follow: float = 1.0   # 0..1
    drum_swing: float = 0.0         # 0..0.5 of the second grid division
    drum_quantize: float = 0.0      # 0..1, mainly for preserved GM drums
    drum_min_spacing: float = 0.0   # seconds; 0 derives from retrigger_gap


# ---------------------------------------------------------------------------
# Event <-> Note conversion
# ---------------------------------------------------------------------------

def events_to_notes(events):
    """Pair note_on/note_off events into note dicts; collect sustain events."""
    notes = []
    sustains = []
    open_notes = {}  # source identity + pitch -> notes awaiting note_off
    last_time = events[-1]['time'] if events else 0.0

    for ev in events:
        t = ev['time']
        if ev['type'] == 'note_on':
            n = {
                'start': t, 'end': None,
                'note': ev['note'],
                'velocity': ev.get('velocity', 64),
                'channel': ev.get('channel', 0),
                'start_beat': ev.get('beat'),
                'end_beat': None,
            }
            for field in (
                    'source_track', 'source_track_name',
                    'source_instrument_name', 'source_port',
                    'source_channel', 'program', 'bank_msb', 'bank_lsb',
                    'source_family', 'source_confidence', 'source_is_drum',
                    'classification_reason', 'group_name', 'group_is_drum'):
                if field in ev:
                    n[field] = ev[field]
            n.setdefault('source_port', 0)
            n.setdefault('source_track', 0)
            n.setdefault('source_channel', n['channel'])
            n['original_note'] = ev.get('original_note', n['note'])
            source_key = (n['source_port'], n['source_track'],
                          n['source_channel'], n['note'])
            open_notes.setdefault(source_key, []).append(n)
            notes.append(n)
        elif ev['type'] == 'note_off':
            source_key = (ev.get('source_port', 0),
                          ev.get('source_track', 0),
                          ev.get('source_channel', ev.get('channel', 0)),
                          ev['note'])
            stack = open_notes.get(source_key)
            if stack:
                matched = stack.pop(0)
                matched['end'] = t
                matched['end_beat'] = ev.get('beat')
        elif ev['type'] == 'sustain':
            sustain = {'time': t, 'value': ev['value'],
                       'channel': ev.get('channel', 0)}
            for field in (
                    'source_track', 'source_track_name',
                    'source_instrument_name', 'source_port',
                    'source_channel', 'program', 'bank_msb', 'bank_lsb',
                    'source_family', 'source_confidence', 'source_is_drum'):
                if field in ev:
                    sustain[field] = ev[field]
            sustain.setdefault('source_port', 0)
            sustain.setdefault('source_track', 0)
            sustain.setdefault('source_channel', sustain['channel'])
            sustains.append(sustain)

    for stack in open_notes.values():
        for n in stack:
            if n['end'] is None:
                n['end'] = last_time

    for n in notes:
        if n['end'] is None or n['end'] <= n['start']:
            n['end'] = n['start'] + 0.01
    return notes, sustains


def notes_to_events(notes, sustains, zone_hints=None):
    """Re-emit an event list the player understands."""
    evs = []
    for n in notes:
        metadata = {
            field: n[field] for field in (
                'source_track', 'source_track_name',
                'source_instrument_name', 'source_port', 'source_channel',
                'program', 'bank_msb', 'bank_lsb', 'source_family',
                'source_confidence', 'source_is_drum',
                'classification_reason', 'group_name', 'group_is_drum',
                'group_confidence', 'original_note')
            if field in n
        }
        note_on = {'time': n['start'], 'type': 'note_on', 'note': n['note'],
                   'velocity': n['velocity'], 'channel': n['channel']}
        note_off = {'time': n['end'], 'type': 'note_off', 'note': n['note'],
                    'channel': n['channel']}
        note_on.update(metadata)
        note_off.update(metadata)
        evs.extend((note_on, note_off))
    for s in sustains:
        event = {'time': s['time'], 'type': 'sustain', 'value': s['value'],
                 'channel': s['channel']}
        for field in ('group_name', 'group_is_drum'):
            if field in s:
                event[field] = s[field]
        evs.append(event)
    for z in (zone_hints or []):
        evs.append({'time': z['time'], 'type': 'zone', 'value': z['value']})

    # At an octave boundary, release notes that end there under the OLD
    # modifier first. Then change the zone so only genuinely sustained notes
    # are remapped before the new attacks. This avoids briefly re-pressing a
    # note whose note_off was scheduled for the same instant.
    order = {'note_off': 0, 'zone': 1, 'sustain': 2, 'note_on': 3}
    evs.sort(key=lambda e: (e['time'], order.get(e['type'], 4)))
    return evs


# ---------------------------------------------------------------------------
# Individual transforms
# ---------------------------------------------------------------------------

def scale_times(notes, sustains, factor):
    if factor == 1.0:
        return
    for n in notes:
        n['start'] *= factor
        n['end'] *= factor
    for s in sustains:
        s['time'] *= factor


def _fold_pitch(note, lo, hi, target=None):
    """Shift a pitch by octaves until inside [lo, hi].

    If target is given, choose the in-range octave closest to target
    (used by voice-aware placement); otherwise stay closest to original.
    """
    if lo > hi:
        lo, hi = hi, lo
    candidates = []
    # All octave transpositions that land inside the range
    k_min = -((note - lo) // 12 + 2)
    for k in range(int(k_min), 12):
        cand = note + 12 * k
        if lo <= cand <= hi:
            candidates.append(cand)
    if not candidates:
        # Range narrower than an octave: clamp to the nearest edge
        return min(max(note, lo), hi)
    ref = target if target is not None else note
    return min(candidates, key=lambda c: (abs(c - ref), abs(c - note)))


def fold_into_range(notes, lo, hi, voice_aware=False):
    """Octave-shift out-of-range notes into [lo, hi]."""
    medians = {}
    if voice_aware:
        by_ch = {}
        for n in notes:
            by_ch.setdefault(n['channel'], []).append(n['note'])
        for ch, pitches in by_ch.items():
            pitches.sort()
            medians[ch] = pitches[len(pitches) // 2]

    for n in notes:
        if not (lo <= n['note'] <= hi):
            target = medians.get(n['channel']) if voice_aware else None
            n['note'] = _fold_pitch(n['note'], lo, hi, target)


def proportional_remap(notes, lo, hi):
    """Linearly compress the song's whole pitch span into [lo, hi].

    Preserves the melodic contour instead of octave-folding outliers,
    at the cost of exact intervals when compression is needed.
    """
    if not notes:
        return
    if lo > hi:
        lo, hi = hi, lo
    pitches = [n['note'] for n in notes]
    song_lo, song_hi = min(pitches), max(pitches)
    span = song_hi - song_lo
    target_span = hi - lo

    if span == 0:
        shift = 0
        if not (lo <= song_lo <= hi):
            shift = _fold_pitch(song_lo, lo, hi) - song_lo
        for n in notes:
            n['note'] += shift
        return

    if span <= target_span:
        # Fits without compression: shift by whole octaves to sit inside.
        shift = 0
        while song_lo + shift < lo:
            shift += 12
        while song_hi + shift > hi:
            shift -= 12
        if song_lo + shift < lo:  # couldn't fit on octave grid; center it
            shift = (lo + (target_span - span) // 2) - song_lo
        for n in notes:
            n['note'] += shift
    else:
        scale = target_span / span
        for n in notes:
            n['note'] = int(round(lo + (n['note'] - song_lo) * scale))


def thin_notes(notes, min_gap, min_len):
    """Merge machine-gun same-pitch repeats and drop micro-notes."""
    # Pass 1: merge re-triggers whose ONSETS are genuinely machine-gun fast.
    # Measuring silence after the previous note incorrectly merges ordinary
    # legato repeats at every tempo because quantized MIDI commonly has zero
    # silence between a note-off and the next note-on.
    merged = []
    last_by_pitch = {}  # (channel, note) -> last kept note dict
    last_onset = {}
    for n in sorted(notes, key=lambda x: (x['start'], x['note'])):
        key = (n['channel'], n['note'])
        prev = last_by_pitch.get(key)
        if prev is not None and n['start'] - last_onset[key] < min_gap:
            prev['end'] = max(prev['end'], n['end'])
            last_onset[key] = n['start']
            continue
        merged.append(n)
        last_by_pitch[key] = n
        last_onset[key] = n['start']
    # Pass 2: drop notes still too short to be audible after merging.
    return [n for n in merged if (n['end'] - n['start']) >= min_len]


# Never shorten a note below this when making room for a retrigger gap - a
# key-down/key-up pair closer together than this reads as no press at all.
MIN_NOTE_LEN = 0.010


def enforce_retrigger_gaps(notes, min_gap):
    """Guarantee real key-UP time between two hits on the same key.

    This app performs by holding and releasing physical keys, so a repeated
    note only reads as a *repeat* if the key is actually observed to be up in
    between. Most MIDI is quantised edge-to-edge: the note_off of one C4 sits
    on the exact timestamp of the next C4's note_on. The player would then
    dispatch release-then-press microseconds apart, and a game that samples
    the keyboard once per frame never sees the gap - so C4 C4 C4 C4 comes out
    as one long C4.

    We fix that at the source by pulling each note's release back to min_gap
    before the next hit on the same key. Onsets are never moved (that's the
    musical timing that matters); only the release moves, and the sustain
    pedal covers the shortened tail in-game anyway.

    Notes are grouped per (channel, pitch) because that is what maps to one
    physical key for one performer. Same-pitch collisions ACROSS channels
    (several channels soloed onto one keyboard) are caught at runtime by
    BPSRInputSimulator's own key-up guard.
    """
    if min_gap <= 0:
        return
    by_key = {}
    for n in notes:
        by_key.setdefault((n['channel'], n['note']), []).append(n)
    for group in by_key.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda n: n['start'])
        for cur, nxt in zip(group, group[1:]):
            latest_end = nxt['start'] - min_gap
            if cur['end'] > latest_end:
                # Keep the note pressable even when the repeat is faster than
                # min_gap; the simulator's runtime guard is the backstop.
                cur['end'] = max(latest_end, cur['start'] + MIN_NOTE_LEN)


def group_chords(notes, window, consistent):
    """Group notes whose starts fall in the same chord window."""
    groups = []
    if consistent and window > 0:
        # Fixed grid anchored at t=0: same input timing -> same grouping,
        # regardless of which notes got culled earlier.
        by_slot = {}
        for n in notes:
            by_slot.setdefault(int(n['start'] / window), []).append(n)
        groups = [by_slot[k] for k in sorted(by_slot)]
    else:
        current = []
        anchor = None
        for n in sorted(notes, key=lambda x: x['start']):
            if anchor is None or n['start'] - anchor <= window:
                current.append(n)
                if anchor is None:
                    anchor = n['start']
            else:
                groups.append(current)
                current = [n]
                anchor = n['start']
        if current:
            groups.append(current)
    return groups


def limit_chords(notes, settings):
    """Apply max chord size / low-priority culling / melody priority."""
    groups = group_chords(notes, settings.chord_window, settings.consistent_windows)
    keep = []
    for g in groups:
        if len(g) <= 1:
            keep.extend(g)
            continue

        g_sorted = sorted(g, key=lambda n: (-n['velocity'], -n['note']))
        chosen = []

        if settings.prioritize_melody:
            melody = max(g, key=lambda n: n['note'])
            chosen.append(melody)
            g_sorted = [n for n in g_sorted if n is not melody]

        for n in g_sorted:
            if len(chosen) >= settings.max_chord_notes:
                break
            chosen.append(n)

        if settings.cull_low_priority and len(chosen) > 1:
            vmax = max(n['velocity'] for n in chosen)
            strong = [n for n in chosen if n['velocity'] >= 0.45 * vmax]
            if settings.prioritize_melody:
                melody = max(chosen, key=lambda n: n['note'])
                if melody not in strong:
                    strong.append(melody)
            chosen = strong if strong else chosen[:1]

        keep.extend(chosen)

    keep.sort(key=lambda n: (n['start'], n['note']))
    return keep


def double_single_note_melody(notes, settings, lo, hi):
    """Add one octave copy to eligible single-note Piano melody attacks.

    With Musical roles grouping, a single Melody attack may be doubled while
    accompaniment plays alongside it.  Otherwise the complete onset must be a
    single note, which avoids guessing that a source track/channel is melody.
    The lower octave is preferred for body; the upper octave is the fallback
    near the bottom of the selected range.  Existing octaves and full chords
    are left alone.
    """
    if (not settings.double_melody_octave
            or settings.max_chord_notes < 2
            or settings.instrument_offset != 0
            or settings.reach_low != ABS_LOW
            or settings.reach_high != ABS_HIGH
            or not notes):
        return notes

    result = list(notes)
    role_grouping = bool(
        settings.auto_split and settings.grouping_mode == 'roles')
    groups = group_chords(
        notes, settings.chord_window, settings.consistent_windows)
    for group in groups:
        if len(group) >= settings.max_chord_notes:
            continue
        if role_grouping:
            melody = [note for note in group
                      if note.get('group_name') == 'Melody']
            if len(melody) != 1:
                continue
            source = melody[0]
        else:
            if len(group) != 1:
                continue
            source = group[0]

        existing = {note['note'] for note in group}
        if (source['note'] - 12 in existing
                or source['note'] + 12 in existing):
            continue
        doubled_pitch = next((
            pitch for pitch in (source['note'] - 12, source['note'] + 12)
            if lo <= pitch <= hi and pitch not in existing
            and any(zone_lo <= source['note'] <= zone_hi
                    and zone_lo <= pitch <= zone_hi
                    for zone_lo, zone_hi in ZONE_RANGES.values())
        ), None)
        if doubled_pitch is None:
            continue
        doubled = dict(source)
        doubled['note'] = doubled_pitch
        doubled['_octave_double_source'] = source
        result.append(doubled)

    result.sort(key=lambda note: (note['start'], note['note']))
    return result


def finalize_octave_doubles(notes, settings):
    """Drop synthetic copies invalidated by later octave-zone placement."""
    present = {id(note) for note in notes}
    result = []
    for note in notes:
        source = note.pop('_octave_double_source', None)
        if source is None:
            result.append(note)
            continue
        # Phrase or melody-lock placement can fold a copy onto its source, or
        # omit the source. Keeping such a copy would create a duplicate key or
        # a detached harmony note rather than a true one-octave doubling.
        if id(source) not in present or abs(note['note'] - source['note']) != 12:
            continue
        if settings.duet_mode and not settings.auto_split:
            note['_duet_octave_source'] = source
        result.append(note)
    return result


def apply_phrase_zones(notes, phrase_gap):
    """Pick ONE octave zone per musical phrase and fold stragglers into it.

    Octave modifier toggles (Shift/Ctrl) then only happen in the silence
    between phrases, never in the middle of a run. Returns zone hint events.
    """
    if not notes:
        return []

    notes_sorted = sorted(notes, key=lambda n: n['start'])
    phrases = []
    current = [notes_sorted[0]]
    phrase_end = notes_sorted[0]['end']

    for n in notes_sorted[1:]:
        if n['start'] >= phrase_end + phrase_gap:
            phrases.append(current)
            current = [n]
        else:
            current.append(n)
        phrase_end = max(phrase_end, n['end'])
    phrases.append(current)

    zone_hints = []
    for phrase in phrases:
        # Pick the zone that already fits the most notes (ties prefer no-modifier)
        def fit_count(z):
            lo, hi = ZONE_RANGES[z]
            return sum(1 for n in phrase if lo <= n['note'] <= hi)
        zone = max((0, 1, -1), key=fit_count)
        lo, hi = ZONE_RANGES[zone]
        for n in phrase:
            if not (lo <= n['note'] <= hi):
                n['note'] = _fold_pitch(n['note'], lo, hi)
        zone_hints.append({
            'time': max(0.0, phrase[0]['start'] - 0.08),
            'value': zone,
        })
    return zone_hints


def apply_melody_lock(notes, chord_window, mode='drop'):
    """Lock the octave shift to the melody (top voice) so it is never cut.

    The game keyboard is one 3-octave window (36 semitones) that L-Shift /
    L-Ctrl slide up or down; only one shift can be held at a time. So every
    note sounding at a given instant must fit inside ONE zone window:
        zone  0 (no mod) : 48..83
        zone +1 (Shift)  : 60..95
        zone -1 (Ctrl)   : 36..71

    We scan the music in onset groups, keep the shift wherever the melody
    (highest sounding line, including still-ringing notes) stays playable, and
    resolve any note that falls outside that window:
        mode='drop'   -> silence it (melody plays clean; default)
        mode='fold'   -> octave-shift it into the window (keeps harmony)
        mode='hybrid' -> fold if it lands clear of the melody, else drop

    Returns (zone_hints, kept_notes). zone_hints are {'time','value'} events
    the player uses to toggle the modifier at the right moment.
    """
    if not notes:
        return [], notes

    ordered = sorted(notes, key=lambda n: (n['start'], -n['note']))
    # Group notes by onset window.
    groups = []
    cur = []
    anchor = None
    for n in ordered:
        if anchor is None or n['start'] - anchor <= chord_window:
            cur.append(n)
            if anchor is None:
                anchor = n['start']
        else:
            groups.append((anchor, cur))
            cur = [n]
            anchor = n['start']
    if cur:
        groups.append((anchor, cur))

    kept = []
    zone_hints = []
    current_zone = 0
    active = []  # kept notes still sounding, for sustained-melody tracking

    for gstart, group in groups:
        active = [k for k in active if k['end'] > gstart + 1e-9]
        sustained_top = max((k['note'] for k in active), default=None)
        group_max = max(n['note'] for n in group)
        melody_ref = group_max if sustained_top is None else max(group_max, sustained_top)
        melody_ref = min(max(melody_ref, ABS_LOW), ABS_HIGH)

        valid = [z for z in (0, 1, -1)
                 if ZONE_RANGES[z][0] <= melody_ref <= ZONE_RANGES[z][1]]
        if not valid:
            valid = [0]

        sounding = active + group

        def fit(z):
            lo, hi = ZONE_RANGES[z]
            return sum(1 for n in sounding if lo <= n['note'] <= hi)

        # Keep hysteresis only as a tie-breaker. Previously, merely being
        # valid for the melody pinned the old zone even when another valid
        # zone preserved an entire chord.
        chosen = max(
            valid,
            key=lambda z: (fit(z), z == current_zone, z == 0,
                           -abs(z - current_zone)))

        if chosen != current_zone:
            zone_hints.append({'time': gstart, 'value': chosen})
            current_zone = chosen

        lo, hi = ZONE_RANGES[chosen]
        # A forced zone change can make an already-ringing accompaniment note
        # impossible. End it at the boundary instead of changing modifiers
        # underneath a physically held key.
        for n in list(active):
            if not (lo <= n['note'] <= hi):
                if n['start'] + MIN_NOTE_LEN <= gstart:
                    n['end'] = min(n['end'], gstart)
                elif n in kept:
                    # Too young to shorten into a pressable note without
                    # crossing the modifier boundary; omit it altogether.
                    kept.remove(n)
                active.remove(n)
        for n in group:
            if lo <= n['note'] <= hi:
                kept.append(n); active.append(n)
                continue
            # Out of the melody's window -> resolve by mode.
            if mode == 'drop':
                continue
            folded = _fold_pitch(n['note'], lo, hi)
            if mode == 'hybrid' and abs(folded - melody_ref) <= 3:
                continue                     # would sit on top of the melody: drop
            n['note'] = folded
            kept.append(n); active.append(n)

    return zone_hints, kept


def _note_is_drum(note):
    """Use parser evidence, with a safe fallback for legacy/manual events."""
    if note.get('source_is_drum') is not None:
        return bool(note['source_is_drum'])
    channel = note.get('source_channel', note.get('channel', 0))
    original_note = note.get('original_note', note.get('note'))
    return (channel == GM_DRUM_CHANNEL
            and is_gm_percussion_note(original_note))


def _copy_sustain(sustain, channel, name, is_drum=False):
    return {
        'time': sustain['time'], 'value': sustain['value'],
        'channel': channel, 'group_name': name,
        'group_is_drum': bool(is_drum),
    }


def assign_auto_parts(notes, sustains, n_parts=2, chord_window=0.030):
    """Assign musical roles from original pitches, excluding percussion."""
    if not notes:
        return sustains

    melodic = [note for note in notes if not _note_is_drum(note)]
    active = []
    for group in group_chords(
            sorted(melodic, key=lambda note: note['start']),
            max(0.0, chord_window), False):
        start = min(note['start'] for note in group)
        active = [note for note in active if note['end'] > start]
        active.extend(group)
        high = max(note.get('original_note', note['note']) for note in active)
        low = min(note.get('original_note', note['note']) for note in active)
        for note in group:
            pitch = note.get('original_note', note['note'])
            if pitch == high:
                channel, name = 0, 'Melody'
            elif n_parts >= 3 and pitch == low:
                channel, name = 2, 'Bass'
            else:
                channel = 1
                name = 'Harmony' if n_parts >= 3 else 'Accompaniment'
            note['channel'] = channel
            note['group_name'] = name
            note['group_is_drum'] = False
            note['group_confidence'] = 'inferred'

    used_melodic = sorted({note['channel'] for note in melodic})
    drum_channel = (max(used_melodic) + 1) if used_melodic else 0
    for note in notes:
        if _note_is_drum(note):
            note['channel'] = drum_channel
            note['group_name'] = 'Drums (not selected for pitch playback)'
            note['group_is_drum'] = True
            note['group_confidence'] = note.get('source_confidence', 'medium')

    names = {note['channel']: note['group_name'] for note in melodic}
    return [_copy_sustain(sustain, channel, names[channel])
            for sustain in sustains for channel in used_melodic]


def _source_group(note, mode):
    port = note.get('source_port', 0)
    source_channel = note.get('source_channel', note.get('channel', 0))
    if mode == 'tracks':
        track = note.get('source_track', 0)
        name = (note.get('source_track_name')
                or note.get('source_instrument_name')
                or f'Track {track + 1}')
        return (port, track), name
    if mode == 'channels':
        label = f'MIDI Ch. {source_channel + 1}'
        if port:
            label = f'Port {port + 1} · {label}'
        return (port, source_channel), label

    family = note.get('source_family')
    if not family:
        classified = classify_midi_source(
            channel=source_channel, program=note.get('program'),
            bank_msb=note.get('bank_msb'), bank_lsb=note.get('bank_lsb'),
            track_name=note.get('source_track_name', ''),
            instrument_name=note.get('source_instrument_name', ''),
            note=note.get('original_note', note.get('note')))
        family = classified['family']
        note.setdefault('source_confidence', classified['confidence'])
        note.setdefault('source_is_drum', classified['is_drum'])
    return family, family


def assign_source_groups(notes, sustains, mode):
    """Group by preserved track, MIDI channel, or instrument family."""
    if mode not in ('tracks', 'channels', 'families'):
        raise ValueError(f'Unsupported grouping mode: {mode}')
    if not notes:
        return sustains

    groups = {}
    for note in sorted(notes, key=lambda item: item['start']):
        key, name = _source_group(note, mode)
        groups.setdefault(key, {'name': name, 'notes': []})['notes'].append(note)

    entries = list(groups.items())
    channel_for = {}
    group_info = {}
    overflow = len(entries) > 16
    for index, (key, info) in enumerate(entries):
        channel = index if not overflow or index < 15 else 15
        channel_for[key] = channel
        target = group_info.setdefault(channel, {
            'name': (info['name'] if not overflow or index < 15
                     else 'Other sources'),
            'is_drum': True,
        })
        target['is_drum'] = target['is_drum'] and all(
            _note_is_drum(note) for note in info['notes'])

    for note in notes:
        key, _name = _source_group(note, mode)
        channel = channel_for[key]
        info = group_info[channel]
        note['channel'] = channel
        note['group_name'] = info['name']
        note['group_is_drum'] = info['is_drum']
        note['group_confidence'] = note.get('source_confidence', 'unknown')

    result = []
    for sustain in sustains:
        probe = dict(sustain)
        probe.setdefault('note', 60)
        key, _name = _source_group(probe, mode)
        channel = channel_for.get(key)
        if channel is not None and not group_info[channel]['is_drum']:
            info = group_info[channel]
            result.append(_copy_sustain(
                sustain, channel, info['name'], info['is_drum']))
    return result


def split_duet(notes, sustains, split_note):
    """Split into Low (channel 0) / High (channel 1) parts."""
    linked_copies = []
    for n in notes:
        source = n.pop('_duet_octave_source', None)
        if source is None:
            n['channel'] = 0 if n['note'] < split_note else 1
        else:
            linked_copies.append((n, source))
    # An octave copy belongs to the same musical line as its source even if
    # the added pitch falls on the other side of the fixed duet split.
    for copy, source in linked_copies:
        copy['channel'] = source.get(
            'channel', 0 if source['note'] < split_note else 1)
    # Sustain pedal is global in-game; give both parts a copy so whichever
    # part is active still gets pedal events (simulator dedupes state).
    doubled = []
    for s in sustains:
        doubled.append({'time': s['time'], 'value': s['value'], 'channel': 0})
        doubled.append({'time': s['time'], 'value': s['value'], 'channel': 1})
    return doubled


# ---------------------------------------------------------------------------
# Drum conversion mode
# ---------------------------------------------------------------------------
# The in-game "Drum" instrument responds on 9 fixed on-screen keys, each a
# distinct percussion voice (D4..A5 - see config.DRUM_NOTES); every other key
# is silent. A straight pitch-based 1:1 conversion would drop almost the whole
# song, so Drum gets a separate Preserve / Augment / Generate path. Auto keeps
# authored GM channel-10 percussion and generates when none exists. Generated
# parts use the MIDI's beat, tempo and meter maps; onset count and sustained
# occupancy determine activity without treating the members of one chord as
# separate rhythmic events.

DRUM_HIT_LEN = 0.09      # seconds a drum tap is held - drums aren't sustained
DRUM_MIN_GAP = 0.06      # per-voice retrigger floor, so a blast-beat passage
                         # doesn't turn into an unplayable flood of re-presses

# Velocity presets. The game plays plain keystrokes, so velocity isn't audible
# in-game, but sensible values keep the output musically meaningful.
_V_ACCENT, _V_NORMAL, _V_SOFT, _V_GHOST = 118, 100, 82, 55

# General MIDI percussion (channel 10) note number -> closest in-game drum
# voice. The common backbone (kick / snare / hi-hats / toms / crashes) maps
# faithfully; rarer hand & auxiliary percussion is folded onto the nearest
# voice (drum-like -> toms, short metallic/shaker ticks -> closed hi-hat,
# sustained metallic -> open hi-hat). The game has no ride cymbal, so rides
# fold onto the closed hi-hat (their usual steady-timekeeping role). Anything
# not listed is ignored rather than turned into a misleading hi-hat.
_GM_TO_VOICE = {
    # kick
    35: DRUM_KICK, 36: DRUM_KICK,
    # snare / rimshot / clap
    37: DRUM_SNARE, 38: DRUM_SNARE, 39: DRUM_SNARE, 40: DRUM_SNARE,
    # toms: high -> tom 1, mid -> tom 2, floor -> floor tom
    50: DRUM_TOM_1, 48: DRUM_TOM_1,
    47: DRUM_TOM_2, 45: DRUM_TOM_2,
    43: DRUM_FLOOR_TOM, 41: DRUM_FLOOR_TOM,
    # hi-hats
    42: DRUM_HH_CLOSED, 44: DRUM_HH_CLOSED, 46: DRUM_HH_OPEN,
    # cymbals
    49: DRUM_CRASH_1, 55: DRUM_CRASH_1, 57: DRUM_CRASH_2, 52: DRUM_CRASH_2,
    58: DRUM_CRASH_1,                       # vibraslap -> trashy crash
    51: DRUM_HH_CLOSED, 59: DRUM_HH_CLOSED, 53: DRUM_HH_CLOSED,  # rides
    # hand drums -> toms
    60: DRUM_TOM_1, 62: DRUM_TOM_1, 63: DRUM_TOM_1, 65: DRUM_TOM_1,
    76: DRUM_TOM_1, 78: DRUM_TOM_1,
    61: DRUM_TOM_2, 66: DRUM_TOM_2, 77: DRUM_TOM_2, 79: DRUM_TOM_2,
    64: DRUM_FLOOR_TOM,                     # low conga
    # shakers / metallic ticks -> closed hi-hat
    54: DRUM_HH_CLOSED, 56: DRUM_HH_CLOSED, 67: DRUM_HH_CLOSED,
    68: DRUM_HH_CLOSED, 69: DRUM_HH_CLOSED, 70: DRUM_HH_CLOSED,
    71: DRUM_HH_CLOSED, 73: DRUM_HH_CLOSED, 75: DRUM_HH_CLOSED,
    80: DRUM_HH_CLOSED,
    # sustained metallic -> open hi-hat
    72: DRUM_HH_OPEN, 74: DRUM_HH_OPEN, 81: DRUM_HH_OPEN,
}


def _gm_drum_bucket(note):
    """Closest in-game drum voice for a GM percussion note (channel 10)."""
    # Unknown percussion notes are ignored. Treating every unknown value as a
    # closed hi-hat can turn vendor-specific percussion into a hat machine-gun.
    return _GM_TO_VOICE.get(note)


def _chord_onsets(notes):
    """Collapse near-simultaneous note starts into single onsets.

    Returns a sorted list of (time, lowest_pitch) - one entry per musical
    onset, carrying the lowest pitch of the group (used to follow the bass).
    """
    onsets = []
    for n in sorted(notes, key=lambda n: n['start']):
        if onsets and n['start'] - onsets[-1][0] < 0.03:
            t, low = onsets[-1]
            onsets[-1] = (t, min(low, n['note']))
        else:
            onsets.append((n['start'], n['note']))
    return onsets


# ---------------------------------------------------------------------------
# Fill palette. Each generator maps a number of 16th steps to a list of
# (step_index, voice, velocity). They are deliberately different in shape -
# descending/ascending tom cascades, a building snare roll, tom+snare trades,
# a galloping (gapped) pattern, doubled hits, and a sparse open fill - so that
# rotating through them stops every fill from sounding the same.
# ---------------------------------------------------------------------------

def _fill_cascade_down(steps):
    seq = (DRUM_SNARE, DRUM_TOM_1, DRUM_TOM_2, DRUM_FLOOR_TOM)
    return [(s, seq[min(s * len(seq) // steps, len(seq) - 1)],
             _V_NORMAL if s % 2 == 0 else _V_SOFT) for s in range(steps)]


def _fill_cascade_up(steps):
    seq = (DRUM_FLOOR_TOM, DRUM_TOM_2, DRUM_TOM_1, DRUM_SNARE)
    return [(s, seq[min(s * len(seq) // steps, len(seq) - 1)],
             _V_NORMAL if s % 2 == 0 else _V_SOFT) for s in range(steps)]


def _fill_snare_roll(steps):
    out = []
    for s in range(steps):
        frac = s / max(1, steps - 1)
        vel = _V_GHOST if frac < 0.5 else (_V_SOFT if frac < 0.85 else _V_ACCENT)
        out.append((s, DRUM_SNARE, vel))
    return out


def _fill_tom_snare(steps):
    seq = (DRUM_SNARE, DRUM_TOM_1, DRUM_SNARE, DRUM_TOM_2, DRUM_SNARE, DRUM_FLOOR_TOM)
    return [(s, seq[s % len(seq)], _V_NORMAL if s % 2 == 0 else _V_SOFT)
            for s in range(steps)]


def _fill_gallop(steps):
    # Groups of three: two hits then a rest - a galloping tom figure.
    seq = (DRUM_TOM_1, DRUM_TOM_2, DRUM_FLOOR_TOM)
    out = []
    for s in range(steps):
        if s % 3 == 2:
            continue
        out.append((s, seq[(s // 3) % len(seq)], _V_NORMAL))
    return out


def _fill_double(steps):
    seq = (DRUM_TOM_1, DRUM_TOM_1, DRUM_TOM_2, DRUM_TOM_2,
           DRUM_FLOOR_TOM, DRUM_FLOOR_TOM, DRUM_SNARE, DRUM_SNARE)
    return [(s, seq[min(s * len(seq) // steps, len(seq) - 1)],
             _V_NORMAL if s % 2 == 0 else _V_SOFT) for s in range(steps)]


def _fill_sparse(steps):
    # Only on the 8th positions - a more open, less busy fill.
    picks = (DRUM_FLOOR_TOM, DRUM_SNARE, DRUM_TOM_2, DRUM_SNARE)
    return [(s, picks[(s // 2) % len(picks)], _V_NORMAL) for s in range(0, steps, 2)]


_FILLS = (_fill_cascade_down, _fill_snare_roll, _fill_tom_snare, _fill_cascade_up,
          _fill_gallop, _fill_double, _fill_sparse)


def _emit_fill(hits, start, fill_beats, s16, pick):
    """Append one fill from the palette (selected by `pick`)."""
    steps = fill_beats * 4
    for step, voice, vel in _FILLS[pick % len(_FILLS)](steps):
        if 0 <= step < steps:
            hits.append((start + step * s16, voice, vel))


def _emit_hats(hits, bt, down, last_beat, half, s16, sixteenths=False):
    """Append the closed hi-hats for one beat.

    Default is 8th notes (2 per beat) so the closed hat never machine-guns.
    `sixteenths` adds a short 16th flutter, which the styles use only on the
    final beat of a bar as a lead-in rather than across the whole bar.
    """
    hits.append((bt, DRUM_HH_CLOSED, _V_NORMAL if down else _V_SOFT))
    if sixteenths and last_beat and s16 >= DRUM_MIN_GAP:
        hits.append((bt + s16, DRUM_HH_CLOSED, _V_GHOST))
        hits.append((bt + half, DRUM_HH_CLOSED, _V_SOFT))
        hits.append((bt + 3 * s16, DRUM_HH_CLOSED, _V_GHOST))
    else:
        hits.append((bt + half, DRUM_HH_CLOSED, _V_SOFT))


def _emit_groove_bar(hits, beats, groove_beats, style, k, half, s16, bass_slots):
    """Append one bar of steady groove in the given style.

    `k` is the bar's index within its section (used to vary ornaments).
    `bass_slots` is the set of 8th-note slot indices (0..2*nbeats-1) where the
    melody plays a low/bass onset; the kick follows those off-beats so the
    groove locks to the actual song rather than a fixed pattern.
    """
    nb = len(beats)
    for b in range(groove_beats):
        bt = beats[b]
        down = (b == 0)
        last_beat = (b == nb - 1)

        if style == 'ballad':
            if b == 0:
                hits.append((bt, DRUM_KICK, _V_ACCENT))
            if b == 2:
                hits.append((bt, DRUM_SNARE, _V_SOFT))
            hits.append((bt, DRUM_HH_CLOSED, _V_SOFT))
            if k % 2 == 1 and last_beat:
                hits.append((bt + half, DRUM_HH_CLOSED, _V_GHOST))

        elif style == 'backbeat':
            if b == 0 or b == 2:
                hits.append((bt, DRUM_KICK, _V_ACCENT if down else _V_NORMAL))
            if b % 2 == 1:
                hits.append((bt, DRUM_SNARE, _V_NORMAL))
            _emit_hats(hits, bt, down, last_beat, half, s16)
            if k % 4 == 2 and b == 3:
                hits.append((bt + half, DRUM_HH_OPEN, _V_NORMAL))

        elif style == 'drive':
            if b == 0 or b == 2:
                hits.append((bt, DRUM_KICK, _V_ACCENT if down else _V_NORMAL))
            if b % 2 == 1:
                hits.append((bt, DRUM_SNARE, _V_NORMAL))
            _emit_hats(hits, bt, down, last_beat, half, s16, sixteenths=(k % 2 == 1))
            if k % 2 == 0 and b == 3:
                hits.append((bt + s16, DRUM_SNARE, _V_GHOST))
            if k % 4 == 2 and b == 1:
                hits.append((bt + half, DRUM_HH_OPEN, _V_NORMAL))

        else:  # 'four' - four-on-floor, the highest-energy / "drop" style
            hits.append((bt, DRUM_KICK, _V_ACCENT if down else _V_NORMAL))
            if b % 2 == 1:
                hits.append((bt, DRUM_SNARE, _V_NORMAL))
            hits.append((bt, DRUM_HH_CLOSED, _V_NORMAL if down else _V_SOFT))
            hits.append((bt + half, DRUM_HH_OPEN, _V_SOFT))
            if k % 4 == 0 and b == 0:
                hits.append((bt, DRUM_FLOOR_TOM, _V_NORMAL))

    # Kick follows the bassline: add a syncopated kick on the melody's most
    # prominent off-beat bass hits (on-beats are already the backbone). Capped
    # at two per bar so it tracks the song without becoming a constant rumble;
    # the lowest-pitched off-beats win. This is what makes each bar's kick
    # pattern differ and lock to the actual music.
    if style != 'ballad':
        cand = [(slot, pitch) for slot, pitch in bass_slots.items()
                if slot % 2 == 1 and slot < groove_beats * 2]
        cand.sort(key=lambda sp: (sp[1], sp[0]))
        for slot, _pitch in cand[:2]:
            hits.append((beats[0] + slot * half, DRUM_KICK, _V_SOFT))


def _generate_groove(notes, beat_len, beats_per_measure):
    """Write a section-aware, melody-following drum-kit groove.

    Returns a list of (time, voice, velocity). The song is split into sections
    by density and register; each section gets its own style; the kick tracks
    the melody's bassline; and fills (varied, from a palette) land at phrase
    ends. Hits are quantised to the tempo grid, not slaved to every onset.
    """
    if beat_len <= 0:
        return []
    onsets = _chord_onsets(notes)
    if not onsets:
        return []

    nbeats = max(1, int(beats_per_measure))
    bar_len = beat_len * nbeats
    half = beat_len / 2.0
    s16 = beat_len / 4.0
    t0 = onsets[0][0]
    t_end = max(n['end'] for n in notes)
    n_bars = max(1, int(math.ceil((t_end - t0) / bar_len - 1e-9)))

    # Per-bar melody stats: onset count and median pitch (register).
    counts = [0] * n_bars
    pitches = [[] for _ in range(n_bars)]
    for n in notes:
        bi = int((n['start'] - t0) / bar_len)
        if 0 <= bi < n_bars:
            counts[bi] += 1
            pitches[bi].append(n['note'])
    all_pitches = sorted(n['note'] for n in notes)
    global_med = all_pitches[len(all_pitches) // 2]
    bar_med = [(sorted(p)[len(p) // 2] if p else None) for p in pitches]

    # Bass 8th-slots per bar: for each 8th position that carries a low onset,
    # remember the lowest pitch there. The groove later picks only the couple
    # most prominent off-beats from this, so the kick locks to the bassline
    # without turning into a constant rumble.
    bass_slots = [dict() for _ in range(n_bars)]
    for t, low in onsets:
        bi = int((t - t0) / bar_len)
        if not (0 <= bi < n_bars):
            continue
        med = bar_med[bi] if bar_med[bi] is not None else global_med
        if low <= med:
            slot = max(0, min(int(round((t - (t0 + bi * bar_len)) / half)), 2 * nbeats - 1))
            if slot not in bass_slots[bi] or low < bass_slots[bi][slot]:
                bass_slots[bi][slot] = low

    # Phrase-end bars: a rest of >= ~1.5 beats in the melody marks a phrase
    # boundary, and we place a fill in the bar leading into it.
    phrase_end = [False] * n_bars
    otimes = [t for t, _ in onsets]
    for a, b in zip(otimes, otimes[1:]):
        if b - a >= beat_len * 1.5:
            bi = int((a - t0) / bar_len)
            if 0 <= bi < n_bars:
                phrase_end[bi] = True

    def density_level(bi):
        if counts[bi] == 0:
            return -1
        window = [counts[j] for j in (bi - 1, bi, bi + 1) if 0 <= j < n_bars]
        per_beat = (sum(window) / len(window)) / nbeats
        if per_beat < 0.85:
            return 0
        if per_beat < 2.2:
            return 1
        return 2

    def reg_bucket(bi):
        m = bar_med[bi]
        if m is None:
            return 1
        if m <= global_med - 4:
            return 0
        if m >= global_med + 4:
            return 2
        return 1

    levels = [density_level(bi) for bi in range(n_bars)]
    regs = [reg_bucket(bi) for bi in range(n_bars)]

    # Section key per bar; a rest (-1) is its own break. Smooth 1-bar islands.
    keys = [None if levels[bi] < 0 else (levels[bi], regs[bi]) for bi in range(n_bars)]
    for bi in range(1, n_bars - 1):
        if keys[bi] is not None and keys[bi - 1] == keys[bi + 1] and keys[bi] != keys[bi - 1] \
                and keys[bi - 1] is not None:
            keys[bi] = keys[bi - 1]

    seg_start = [False] * n_bars
    seg_end = [False] * n_bars
    for bi in range(n_bars):
        if keys[bi] is None:
            continue
        if bi == 0 or keys[bi - 1] != keys[bi]:
            seg_start[bi] = True
        if bi == n_bars - 1 or keys[bi + 1] != keys[bi]:
            seg_end[bi] = True

    hits = []
    bar_in_seg = 0
    prev_seg_level = None
    prev_seg_reg = None
    fill_rot = 0

    for bi in range(n_bars):
        lvl = levels[bi]
        if lvl < 0:
            bar_in_seg = 0
            continue
        reg = regs[bi]
        if seg_start[bi]:
            bar_in_seg = 0

        base = t0 + bi * bar_len
        beats = [base + b * beat_len for b in range(nbeats)]

        is_drop = seg_start[bi] and (
            (prev_seg_level is not None and lvl > prev_seg_level) or
            (reg == 2 and (prev_seg_reg is None or prev_seg_reg < 2)))

        if lvl >= 2 and (is_drop or reg == 2):
            style = 'four'
        elif lvl >= 2:
            style = 'drive'
        elif lvl == 1:
            style = 'backbeat'
        else:
            style = 'ballad'

        if seg_start[bi]:
            hits.append((beats[0], DRUM_CRASH_2 if is_drop else DRUM_CRASH_1, _V_ACCENT))

        # Fills land at real musical seams: a section change, a melodic phrase
        # gap, or - as a fallback so long uniform sections still breathe - every
        # 8 bars. Section/phrase-end fills are longer (2 beats) than the fallback.
        at_seam = (seg_end[bi] and bi != n_bars - 1) or phrase_end[bi]
        want_fill = lvl >= 1 and (at_seam or bar_in_seg % 8 == 7)
        fill_beats = (2 if (lvl >= 2 and at_seam) else 1) if want_fill else 0
        groove_beats = nbeats - fill_beats

        _emit_groove_bar(hits, beats, groove_beats, style, bar_in_seg, half, s16,
                         bass_slots[bi])

        if fill_beats:
            _emit_fill(hits, beats[groove_beats], fill_beats, s16, fill_rot * 3 + bi)
            fill_rot += 1
            nxt = bi + 1
            if nxt < n_bars and levels[nxt] >= 0 and not seg_start[nxt]:
                hits.append((t0 + nxt * bar_len,
                             DRUM_CRASH_1 if fill_rot % 2 else DRUM_CRASH_2, _V_ACCENT))

        if seg_end[bi]:
            prev_seg_level, prev_seg_reg = lvl, reg
        bar_in_seg += 1

    return hits


class _DrumTimeline:
    """Convert absolute quarter-note beats to scaled playback seconds."""

    def __init__(self, tempo_map, default_bpm, factor):
        points = tempo_map or [{'beat': 0.0, 'bpm': default_bpm}]
        cleaned = []
        for point in points:
            beat = max(0.0, float(point.get('beat', 0.0)))
            bpm = float(point.get('bpm', default_bpm) or default_bpm)
            if bpm > 0:
                cleaned.append((beat, bpm))
        cleaned.sort()
        if not cleaned or cleaned[0][0] > 0:
            cleaned.insert(0, (0.0, default_bpm))
        self.points = cleaned
        self.factor = factor

    def seconds(self, beat):
        beat = max(0.0, float(beat))
        total = 0.0
        for index, (start, bpm) in enumerate(self.points):
            if start >= beat:
                break
            end = beat
            if index + 1 < len(self.points):
                end = min(end, self.points[index + 1][0])
            if end > start:
                total += (end - start) * 60.0 / bpm
            if end >= beat:
                break
        return total * self.factor


def _drum_note_beats(notes, timeline, default_bpm, factor):
    """Fill beat metadata for legacy callers and synthetic tests."""
    seconds_per_beat = (60.0 / default_bpm) * factor
    for note in notes:
        if note.get('start_beat') is None:
            note['start_beat'] = note['start'] / max(seconds_per_beat, 1e-9)
        if note.get('end_beat') is None:
            note['end_beat'] = max(
                note['start_beat'],
                note['start_beat'] + (note['end'] - note['start']) /
                max(seconds_per_beat, 1e-9),
            )


def _drum_bars(end_beat, time_signature_map, default_numerator):
    """Build meter-aware bars as (start, end, numerator, denominator)."""
    points = time_signature_map or [{
        'beat': 0.0, 'numerator': default_numerator, 'denominator': 4
    }]
    cleaned = []
    for point in points:
        cleaned.append((
            max(0.0, float(point.get('beat', 0.0))),
            max(1, int(point.get('numerator', default_numerator))),
            max(1, int(point.get('denominator', 4))),
        ))
    cleaned.sort()
    if not cleaned or cleaned[0][0] > 0:
        cleaned.insert(0, (0.0, max(1, int(default_numerator)), 4))

    bars = []
    for index, (section_start, numerator, denominator) in enumerate(cleaned):
        section_end = end_beat
        if index + 1 < len(cleaned):
            section_end = min(section_end, cleaned[index + 1][0])
        bar_beats = numerator * 4.0 / denominator
        cursor = section_start
        while cursor < section_end - 1e-9:
            bar_end = min(cursor + bar_beats, section_end)
            bars.append((cursor, bar_end, numerator, denominator))
            cursor = bar_end
        if section_end >= end_beat:
            break
    return bars


def _drum_bar_analysis(notes, bars):
    """Extract musical activity, dynamics, register, and low-voice rhythm."""
    if not bars:
        return []
    all_pitches = sorted(int(note['note']) for note in notes)
    global_median = all_pitches[len(all_pitches) // 2]
    pitch_span = all_pitches[-1] - all_pitches[0]
    lower_quartile = all_pitches[len(all_pitches) // 4]
    if global_median <= 55:
        bass_cutoff = global_median + 3
    elif pitch_span >= 12:
        bass_cutoff = min(global_median - 5, lower_quartile + 3)
    else:
        bass_cutoff = -1

    starts = [bar[0] for bar in bars]
    ends = [bar[1] for bar in bars]
    onsets_by_bar = [[] for _bar in bars]
    max_overlap = [0.0 for _bar in bars]
    full_bar_delta = [0 for _ in range(len(bars) + 1)]

    for note in notes:
        ns = float(note['start_beat'])
        ne = max(ns, float(note['end_beat']))
        onset_bar = bisect_right(starts, ns) - 1
        if (0 <= onset_bar < len(bars)
                and starts[onset_bar] <= ns < ends[onset_bar]):
            onsets_by_bar[onset_bar].append((
                ns, int(note['note']), int(note.get('velocity', 64)),
                max(0.0, ne - ns),
            ))

        first = bisect_right(ends, ns)
        last = bisect_left(starts, ne) - 1
        first = max(0, first)
        last = min(len(bars) - 1, last)
        if first > last:
            continue
        max_overlap[first] = max(
            max_overlap[first], min(ne, ends[first]) - max(ns, starts[first]))
        if last != first:
            max_overlap[last] = max(
                max_overlap[last], min(ne, ends[last]) - max(ns, starts[last]))
            if first + 1 < last:
                full_bar_delta[first + 1] += 1
                full_bar_delta[last] -= 1

    full_depth = 0
    analysis = []
    for index, (start, end, numerator, denominator) in enumerate(bars):
        full_depth += full_bar_delta[index]
        nominal_bar_beats = numerator * 4.0 / denominator
        onset_groups = []
        for beat, pitch, velocity, duration in sorted(onsets_by_bar[index]):
            if not onset_groups or beat - onset_groups[-1]['beat'] > 0.06:
                onset_groups.append({
                    'beat': beat,
                    'low': pitch,
                    'high': pitch,
                    'velocity': velocity,
                    'duration': duration,
                    'notes': 1,
                })
            else:
                group = onset_groups[-1]
                group['low'] = min(group['low'], pitch)
                group['high'] = max(group['high'], pitch)
                group['velocity'] = max(group['velocity'], velocity)
                group['duration'] = max(group['duration'], duration)
                group['notes'] += 1
        occupancy = 1.0 if full_depth else max_overlap[index] / max(
            nominal_bar_beats, 1e-9)
        rhythmic_rate = len(onset_groups) / max(nominal_bar_beats, 1e-9)
        active = occupancy > 0.08 or bool(onset_groups)
        velocities = [group['velocity'] for group in onset_groups]
        high_notes = [group['high'] for group in onset_groups]
        average_velocity = (
            sum(velocities) / len(velocities) if velocities else 64)
        average_high = (
            sum(high_notes) / len(high_notes) if high_notes else global_median)
        rhythm_energy = min(1.0, rhythmic_rate / 2.0)
        dynamic_energy = max(0.0, min(1.0, (average_velocity - 35.0) / 75.0))
        register_lift = max(
            0.0, min(1.0, (average_high - global_median) / 18.0))
        energy = (
            0.55 * rhythm_energy
            + 0.25 * dynamic_energy
            + 0.10 * min(1.0, occupancy)
            + 0.10 * register_lift
        ) if active else 0.0
        analysis.append({
            'active': active,
            'occupancy': occupancy,
            'onsets': [group['beat'] for group in onset_groups],
            'onset_details': onset_groups,
            'bass_onsets': [
                group for group in onset_groups
                if group['low'] <= bass_cutoff
            ],
            'melody_onsets': [
                group for group in onset_groups
                if group['high'] >= global_median
            ],
            'velocity': average_velocity,
            'energy': energy,
        })

    # Smooth energy just enough to make builds feel gradual, while retaining
    # the current bar as the strongest vote so drops still land on time.
    for index, info in enumerate(analysis):
        if not info['active']:
            info['level'] = -1
            info['smoothed_energy'] = 0.0
            continue
        neighbour_energy = []
        if index and analysis[index - 1]['active']:
            neighbour_energy.append(analysis[index - 1]['energy'])
        if index + 1 < len(analysis) and analysis[index + 1]['active']:
            neighbour_energy.append(analysis[index + 1]['energy'])
        smoothed = (
            info['energy'] * 2.0 + sum(neighbour_energy)
        ) / (2.0 + len(neighbour_energy))
        info['smoothed_energy'] = smoothed
        if smoothed < 0.27:
            info['level'] = 0
        elif smoothed < 0.58:
            info['level'] = 1
        else:
            info['level'] = 2

    all_onsets = [
        group['beat']
        for info in analysis
        for group in info['onset_details']
    ]
    for index, info in enumerate(analysis):
        last = info['onsets'][-1] if info['onsets'] else None
        next_position = (
            bisect_right(all_onsets, last) if last is not None else 0)
        next_onset = (
            all_onsets[next_position]
            if last is not None and next_position < len(all_onsets)
            else None)
        next_active = (
            index + 1 < len(analysis) and analysis[index + 1]['active'])
        info['phrase_end'] = bool(
            info['active'] and (
                not next_active
                or (last is not None and next_onset is not None
                    and next_onset - last >= 1.5)
                or (last is not None and next_onset is None)
            ))
    return analysis


def _swing_beat(beat, division, amount):
    if amount <= 0 or division <= 0:
        return beat
    slot = int(round(beat / division))
    if slot % 2:
        return beat + division * min(0.5, amount)
    return beat


def _add_beat_hit(hits, timeline, beat, voice, velocity, swing=0.0,
                  swing_division=0.5):
    beat = _swing_beat(beat, swing_division, swing)
    hits.append((timeline.seconds(beat), voice, int(max(1, min(127, velocity)))))


def _meter_backbeats(numerator, denominator):
    """Return strong kick/snare positions in quarter-note beat units."""
    bar_beats = numerator * 4.0 / denominator
    if denominator == 8 and numerator % 3 == 0:
        pulse = 1.5
        kicks = [0.0]
        snares = [pulse] if numerator >= 6 else []
    elif numerator == 3:
        kicks, snares = [0.0], [1.0, 2.0]
    elif numerator == 2:
        kicks, snares = [0.0], [1.0]
    elif numerator == 5:
        kicks, snares = [0.0, 3.0], [2.0, 4.0]
    elif numerator == 7 and denominator == 8:
        kicks, snares = [0.0, 2.0], [1.0, 3.0]
    else:
        kicks = [beat for beat in (0.0, 2.0) if beat < bar_beats]
        snares = [beat for beat in (1.0, 3.0) if beat < bar_beats]
    return (
        bar_beats,
        [beat for beat in kicks if 0 <= beat < bar_beats],
        [beat for beat in snares if 0 <= beat < bar_beats],
    )


def _musical_bar_variant(info, bar_start, index):
    """Stable 0..2 variation derived from this bar's authored MIDI rhythm."""
    signature = index * 17 + int(round(info['energy'] * 100))
    for group in info['onset_details']:
        local_slot = int(round((group['beat'] - bar_start) * 4))
        signature += (
            (local_slot + 1) * 7
            + group['low'] * 3
            + group['high']
            + group['velocity']
        )
    return signature % 3


def _append_unique_slot(slots, value, tolerance=0.10):
    if not any(abs(existing - value) <= tolerance for existing in slots):
        slots.append(value)


def _generate_groove_v2(notes, timeline, bars, settings):
    """Generate a meter-aware groove driven by musical phrases and voices."""
    if not notes or not bars:
        return []
    analysis = _drum_bar_analysis(notes, bars)
    first_onset = min(float(note['start_beat']) for note in notes)
    hits = []
    fill_rotation = 0
    style_setting = (settings.drum_style or 'auto').lower()
    hat_setting = (settings.drum_hat_density or 'eighth').lower()
    intensity = max(0.25, min(2.0, float(settings.drum_intensity)))
    fill_every = max(0, int(settings.drum_fill_frequency))

    for index, ((start, end, numerator, denominator), info) in enumerate(
            zip(bars, analysis)):
        if not info['active']:
            continue
        level = info['level']
        variant = _musical_bar_variant(info, start, index)
        style = style_setting
        if style == 'auto':
            style = ('ballad', 'pop', 'rock')[max(0, min(2, level))]
        bar_beats, kicks, snares = _meter_backbeats(numerator, denominator)

        # Pattern variants respond deterministically to each bar's rhythm and
        # pitches. Re-converting is stable, but adjacent bars stop feeling like
        # a pasted loop.
        if style == 'dance':
            kicks = [b for b in range(int(math.ceil(bar_beats))) if b < bar_beats]
        elif style == 'ballad':
            kicks = kicks[:1]
            if bar_beats >= 4 and variant == 2 and level >= 1:
                _append_unique_slot(kicks, 2.5)
        elif style == 'rock' and bar_beats >= 4:
            rock_extras = ((3.0,), (1.5, 3.0), (0.5, 3.5))[variant]
            for extra in rock_extras:
                _append_unique_slot(kicks, extra)
        elif style == 'pop' and bar_beats >= 3:
            pop_extras = ((2.5,), (1.5, 3.5), (0.5, 2.5))[variant]
            for extra in pop_extras:
                if extra < bar_beats:
                    _append_unique_slot(kicks, extra)

        # Follow the inferred LOW voice only. High melody syncopation is used
        # for snare responses below and no longer masquerades as a bassline.
        follow_capacity = max(0, level)
        if level >= 2:
            follow_capacity = 3 if intensity >= 1.35 else 2
        follow_count = int(round(
            max(0.0, min(1.0, settings.drum_bass_follow))
            * follow_capacity))
        bass_candidates = []
        for group in info['bass_onsets']:
            local = group['beat'] - start
            if 0 <= local < bar_beats and abs(local - round(local)) > 0.12:
                bass_candidates.append((group['velocity'], local))
        bass_candidates.sort(key=lambda item: (-item[0], item[1]))
        for _velocity, local in bass_candidates[:follow_count]:
            _append_unique_slot(kicks, local)

        # Intensity changes actual arrangement density, not merely velocity.
        if intensity < 0.65:
            snares = snares[:1]
            kicks = sorted(kicks)[:max(1, min(2, len(kicks)))]
        elif intensity >= 1.35 and bar_beats >= 2:
            _append_unique_slot(kicks, bar_beats / 2.0)

        previous = analysis[index - 1] if index else None
        energy_rise = bool(
            previous and previous['active']
            and info['smoothed_energy'] - previous['smoothed_energy'] >= 0.14)
        section_start = not previous or not previous['active'] or energy_rise
        if section_start:
            crash = DRUM_CRASH_2 if level >= 2 else DRUM_CRASH_1
            _add_beat_hit(hits, timeline, start, crash, _V_ACCENT)
        for offset in sorted(kicks):
            _add_beat_hit(hits, timeline, start + offset, DRUM_KICK,
                          _V_ACCENT if offset == 0 else _V_NORMAL)
        for offset in snares:
            _add_beat_hit(hits, timeline, start + offset, DRUM_SNARE, _V_NORMAL)

        if hat_setting == 'quarter':
            hat_step = 1.0
        elif hat_setting == 'sixteenth':
            hat_step = 0.25
        else:
            hat_step = 0.5
        if (intensity < 0.55 or level == 0
                or (level == 1 and info['smoothed_energy'] < 0.38)):
            hat_step = max(1.0, hat_step)
        elif intensity > 1.6:
            hat_step = min(0.25, hat_step)
        offset = 0.0
        slot = 0
        while offset < bar_beats - 1e-9:
            voice = (
                DRUM_HH_OPEN
                if style == 'dance' and slot % 2 == 1
                else DRUM_HH_CLOSED)
            _add_beat_hit(
                hits, timeline, start + offset, voice,
                _V_NORMAL if slot % 2 == 0 else _V_SOFT,
                settings.drum_swing, hat_step,
            )
            offset += hat_step
            slot += 1

        # A high-energy phrase gets one purposeful open-hat lift, rather than
        # the previous open-hat hit on every subdivision.
        if style != 'dance' and level >= 2 and intensity >= 0.8:
            open_offsets = [(
                max(0.0, bar_beats - 0.5)
                if info['phrase_end'] or variant == 2
                else 1.5 if variant == 1 and bar_beats > 2 else 0.5)]
            if intensity >= 1.0 and info['energy'] >= 0.62 and bar_beats >= 4:
                open_offsets.append(2.5)
            for open_offset in sorted(set(open_offsets)):
                _add_beat_hit(
                    hits, timeline, start + open_offset,
                    DRUM_HH_OPEN, _V_NORMAL, settings.drum_swing, 0.5)

        # Let a syncopated melody receive a short ghost-snare answer. The low
        # voice is excluded so kick and snare react to different musical roles.
        melody_syncopations = []
        for group in info['melody_onsets']:
            local = group['beat'] - start
            if (group not in info['bass_onsets']
                    and 0 <= local < bar_beats - 0.25
                    and abs(local - round(local)) > 0.12):
                melody_syncopations.append(group)
        if level >= 1 and intensity >= 0.85 and melody_syncopations:
            # A restrained passage gets one answer; a genuinely busy phrase
            # can earn up to four, spaced apart and kept away from the main
            # backbeats. This makes authored motion audible without a constant
            # sixteenth-note drum roll.
            response_limit = (
                4 if level >= 2 and info['energy'] >= 0.62
                else 2 if level >= 2 else 1)
            responses = []
            backbeats = [start + offset for offset in snares]
            for group in reversed(melody_syncopations):
                response = group['beat'] + 0.25
                if response >= end:
                    continue
                if any(abs(response - backbeat) < 0.18
                       for backbeat in backbeats):
                    continue
                if any(abs(response - existing) < 0.40
                       for existing in responses):
                    continue
                responses.append(response)
                if len(responses) >= response_limit:
                    break
            for response in sorted(responses):
                _add_beat_hit(
                    hits, timeline, response, DRUM_SNARE, _V_GHOST,
                    settings.drum_swing, 0.25)

        next_info = analysis[index + 1] if index + 1 < len(analysis) else None
        energy_change = bool(
            next_info and next_info['active']
            and abs(next_info['smoothed_energy']
                    - info['smoothed_energy']) >= 0.16)
        seam = info['phrase_end'] or energy_change
        periodic = fill_every and (index + 1) % fill_every == 0
        if level >= 1 and intensity >= 0.8 and (seam or periodic):
            fill_len = min(
                1.0 if level >= 2 or energy_change else 0.5,
                bar_beats)
            fill_start = end - fill_len
            step = 0.25 if intensity >= 1.25 else 0.5
            voices = (DRUM_TOM_1, DRUM_TOM_2, DRUM_FLOOR_TOM, DRUM_SNARE)
            fill_pos = fill_start
            fi = 0
            while fill_pos < end - 1e-9:
                _add_beat_hit(hits, timeline, fill_pos,
                              voices[(fill_rotation + fi)
                                     % len(voices)], _V_NORMAL)
                fill_pos += step
                fi += 1
            fill_rotation += 1

    # A pickup may begin after beat zero. Preserve the grid, but do not play
    # accompaniment before the source starts or extend it past the source end.
    first_time = timeline.seconds(first_onset)
    last_time = timeline.seconds(max(float(note['end_beat']) for note in notes))
    return [hit for hit in hits
            if first_time - 1e-9 <= hit[0] <= last_time + 1e-9]


def _gm_voice_family(voice):
    if voice == DRUM_KICK:
        return 'kick'
    if voice == DRUM_SNARE:
        return 'snare'
    if voice in (DRUM_HH_CLOSED, DRUM_HH_OPEN):
        return 'hat'
    if voice in (DRUM_CRASH_1, DRUM_CRASH_2):
        return 'cymbal'
    return 'tom'


def _map_gm_hits(notes, timeline, settings):
    """Map GM drums while retaining source timing unless quantize is requested."""
    hits = []
    quantize = max(0.0, min(1.0, float(settings.drum_quantize)))
    for note in notes:
        voice = _gm_drum_bucket(note['note'])
        if voice is None:
            continue
        source_beat = float(note['start_beat'])
        if quantize:
            grid = 0.25
            target = round(source_beat / grid) * grid
            source_beat += (target - source_beat) * quantize
            source_beat = _swing_beat(source_beat, 0.5, settings.drum_swing)
        hits.append((timeline.seconds(source_beat), voice, note['velocity']))

    # Only collapse effectively simultaneous duplicates of the same voice.
    hits.sort(key=lambda hit: (hit[0], hit[1]))
    collapsed = []
    for hit in hits:
        if collapsed and hit[1] == collapsed[-1][1] and hit[0] - collapsed[-1][0] < 0.005:
            if hit[2] > collapsed[-1][2]:
                collapsed[-1] = hit
        else:
            collapsed.append(hit)
    return collapsed


def _choose_drum_source_mode(requested, original_hits, bars, timeline):
    requested = (requested or 'auto').lower()
    if requested in ('preserve', 'augment', 'generate'):
        return requested
    if not original_hits:
        return 'generate'
    # Auto is deliberately conservative: if the author supplied percussion,
    # keep it. "Augment" is an explicit opt-in because adding parts can change
    # the musical intent even when a source track looks incomplete.
    return 'preserve'


def _augment_hits(original, generated):
    """Add only generated voice families missing near an original groove."""
    if not original:
        return generated
    output = list(original)
    for candidate in generated:
        time, voice, _velocity = candidate
        family = _gm_voice_family(voice)
        if any(abs(time - existing[0]) < 0.08 and
               _gm_voice_family(existing[1]) == family for existing in output):
            continue
        output.append(candidate)
    return output


def convert_drum(events, settings, orig_bpm=120.0, beats_per_measure=4,
                 tempo_map=None, time_signature_map=None):
    """Drum-mode conversion.

    Preserves a real GM percussion track (mapped onto the 9 in-game voices),
    or writes a section-aware, melody-following groove when the source is
    melodic.
    """
    notes, _sustains = events_to_notes(events)
    if not notes:
        return []

    # Tempo / speed - same knobs as the normal pipeline, applied the same way.
    factor = 1.0
    if settings.bpm_override and settings.bpm_override > 0 and orig_bpm > 0:
        factor *= orig_bpm / settings.bpm_override
    if settings.speed and settings.speed > 0:
        factor /= settings.speed
    if factor != 1.0:
        for n in notes:
            n['start'] *= factor
            n['end'] *= factor

    orig_bpm_safe = orig_bpm if orig_bpm and orig_bpm > 0 else 120.0
    timeline = _DrumTimeline(tempo_map, orig_bpm_safe, factor)
    _drum_note_beats(notes, timeline, orig_bpm_safe, factor)
    end_beat = max(float(note['end_beat']) for note in notes)
    bars = _drum_bars(end_beat, time_signature_map, beats_per_measure)

    drum_notes = [note for note in notes if _note_is_drum(note)]
    melodic_notes = [note for note in notes if not _note_is_drum(note)]
    original_hits = _map_gm_hits(drum_notes, timeline, settings)
    # Auto should preserve only when the source produced usable mapped hits.
    # A channel-10 track containing unsupported GM percussion is otherwise
    # mistaken for a valid drum part and converts to silence.
    mode = _choose_drum_source_mode(
        settings.drum_source_mode, original_hits, bars, timeline)
    groove_source = melodic_notes or notes
    generated_hits = _generate_groove_v2(groove_source, timeline, bars, settings)
    if mode == 'preserve':
        hits = original_hits
    elif mode == 'augment':
        hits = _augment_hits(original_hits, generated_hits)
    else:
        hits = generated_hits

    # Per-voice retrigger floor: drop hits arriving too soon after the last
    # hit on the same voice (keeps fast passages from becoming a key-mash).
    hits.sort(key=lambda h: (h[0], h[1]))
    last_by_voice = {}
    kept = []
    min_spacing = max(
        DRUM_MIN_GAP,
        MIN_NOTE_LEN + max(0.0, settings.retrigger_gap),
        max(0.0, float(settings.drum_min_spacing)),
    )
    for t, voice, vel in hits:
        prev = last_by_voice.get(voice)
        if prev is not None and t - prev < min_spacing:
            continue
        kept.append([t, voice, vel])
        last_by_voice[voice] = t

    # Trim each tap so the key is genuinely UP before the next hit on the SAME
    # voice re-presses it - a 5ms blip between release and press is invisible
    # to a game that polls input per frame, which turns a run of hits on one
    # voice into a single held key. Otherwise stay a short tap.
    gap = max(0.0, settings.retrigger_gap)
    next_same = {}
    for item in reversed(kept):
        t, voice, _vel = item
        nxt = next_same.get(voice)
        end = t + DRUM_HIT_LEN
        if nxt is not None:
            end = min(end, nxt - gap)
        if end <= t:
            end = t + MIN_NOTE_LEN
        item.append(end)
        next_same[voice] = t

    out_notes = [{'start': t, 'end': end, 'note': voice,
                  'velocity': vel, 'channel': 0} for t, voice, vel, end in kept]
    return notes_to_events(out_notes, [], [])


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def convert(events, settings, orig_bpm=120.0):
    """Run the full conversion pipeline. Returns a new event list."""
    notes, sustains = events_to_notes(events)

    # Optionally strip all sustain-pedal events: the app then never taps Space,
    # so you can hold the in-game sustain manually for a smooth legato — useful
    # for very fast passages where rapid key re-triggering sounds glitchy.
    if settings.disable_sustain:
        sustains = []

    # 1. Tempo / speed
    factor = 1.0
    if settings.bpm_override and settings.bpm_override > 0 and orig_bpm > 0:
        factor *= orig_bpm / settings.bpm_override
    if settings.speed and settings.speed > 0:
        factor /= settings.speed
    scale_times(notes, sustains, factor)

    # Group while pitches still reflect the authored MIDI.  Range folding can
    # collapse distant registers onto one octave; doing this later made bass,
    # harmony and melody indistinguishable and caused the stiff part changes
    # reported by users.
    if settings.auto_split:
        if settings.grouping_mode == 'roles':
            sustains = assign_auto_parts(
                notes, sustains, settings.auto_split_parts,
                settings.chord_window)
        else:
            sustains = assign_source_groups(
                notes, sustains, settings.grouping_mode)
    percussion_notes = (
        [note for note in notes if note.get('group_is_drum')]
        if settings.auto_split else [])
    pitched_notes = (
        [note for note in notes if not note.get('group_is_drum')]
        if settings.auto_split else notes)

    # 2. Pitch range mapping (clamped to the instrument's physical reach)
    reach_lo, reach_hi = settings.reach_low, settings.reach_high
    lo = max(reach_lo, min(settings.range_low, settings.range_high))
    hi = min(reach_hi, max(settings.range_low, settings.range_high))
    if lo > hi:
        # The requested range does not intersect the instrument at all. Clamp
        # to the nearest reachable edge rather than creating a negative scale.
        if max(settings.range_low, settings.range_high) < reach_lo:
            lo = hi = reach_lo
        else:
            lo = hi = reach_hi
    if settings.proportional_remap:
        proportional_remap(pitched_notes, lo, hi)
        # Percussion is a selectable reference part, not pitch material that
        # should stretch the melody's range calculation.
        fold_into_range(percussion_notes, lo, hi)
    else:
        fold_into_range(
            pitched_notes, lo, hi, voice_aware=settings.voice_aware)
        fold_into_range(percussion_notes, lo, hi)

    # 3. Note thinning
    if settings.note_thinning:
        pitched_notes = thin_notes(
            pitched_notes, settings.thinning_gap, settings.thinning_min_len)
        percussion_notes = thin_notes(
            percussion_notes, settings.thinning_gap,
            settings.thinning_min_len)

    # 4. Chord limiting / culling / melody priority
    if (settings.max_chord_notes < 5 or settings.cull_low_priority
            or settings.consistent_windows or settings.prioritize_melody):
        pitched_notes = limit_chords(pitched_notes, settings)

    # 5. Optional Piano octave doubling. It runs after chord limiting so the
    # final onset count can be checked against Max chord notes, but before zone
    # planning so both simultaneous keys always share a valid modifier zone.
    pitched_notes = double_single_note_melody(
        pitched_notes, settings, lo, hi)

    # 6. Octave-zone planning (melody-lock takes precedence over phrase-gap).
    # These model the PIANO's 3 shift zones, so they only apply to instruments
    # that use the piano pitch mapping (offset 0); a transposed keyboard like
    # Bass fits in one zone and needs no shifting.
    zone_hints = []
    if settings.instrument_offset == 0:
        if settings.melody_lock:
            zone_hints, pitched_notes = apply_melody_lock(
                pitched_notes, settings.chord_window,
                settings.melody_lock_mode)
        elif settings.phrase_gap_shifting:
            zone_hints = apply_phrase_zones(
                pitched_notes, settings.phrase_gap)

    notes = pitched_notes + percussion_notes

    # 7. Final safety: everything must be inside the instrument's reach
    fold_into_range(notes, reach_lo, reach_hi)
    notes = finalize_octave_doubles(notes, settings)

    # 8. A fixed duet remains a post-arrangement pitch split. Source-aware
    # grouping was already performed before folding and supersedes it.
    if not settings.auto_split and settings.duet_mode:
        sustains = split_duet(notes, sustains, settings.duet_split_note)

    # 9. Retrigger gaps. Must be LAST: it works on the final pitch of each note
    # (after folding/melody-lock) and its final channel (after the duet /
    # grouping assignment above), since together those decide which physical
    # key a note lands on and who plays it.
    enforce_retrigger_gaps(notes, settings.retrigger_gap)

    return notes_to_events(notes, sustains, zone_hints)
