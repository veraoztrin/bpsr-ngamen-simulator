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

from dataclasses import dataclass
from itertools import groupby

# Playable zones for the BPSR 3-octave keyboard + octave modifiers.
# zone 0  : no modifier    -> MIDI 48..83  (C3..B5)
# zone +1 : L Shift held   -> MIDI 60..95  (C4..B7 sounding)
# zone -1 : L Ctrl held    -> MIDI 36..71  (C2..B4 sounding)
ZONE_RANGES = {0: (48, 83), 1: (60, 95), -1: (36, 71)}
ABS_LOW = 36   # C2 - lowest reachable note
ABS_HIGH = 95  # B7 - highest reachable note


@dataclass
class ConversionSettings:
    bpm_override: float = None      # None = keep original tempo
    speed: float = 1.0              # playback speed multiplier (0.5 = half speed)
    max_chord_notes: int = 5        # 1..5 simultaneous new notes per chord
    note_thinning: bool = False     # merge machine-gun repeats / drop micro-notes
    cull_low_priority: bool = False # drop quiet notes inside dense chords
    prioritize_melody: bool = False # always keep the highest voice when trimming
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
    disable_sustain: bool = False   # strip all pedal events (hold Space manually)
    range_low: int = ABS_LOW        # allowed output range (folded into)
    range_high: int = ABS_HIGH
    chord_window: float = 0.030     # seconds; notes within this = one chord
    thinning_gap: float = 0.030     # min silence between same-pitch repeats
    thinning_min_len: float = 0.020 # drop notes shorter than this when thinning
    phrase_gap: float = 0.150       # silence >= this = phrase boundary


# ---------------------------------------------------------------------------
# Event <-> Note conversion
# ---------------------------------------------------------------------------

def events_to_notes(events):
    """Pair note_on/note_off events into note dicts; collect sustain events."""
    notes = []
    sustains = []
    open_notes = {}  # (channel, note) -> [note dicts awaiting note_off]
    last_time = events[-1]['time'] if events else 0.0

    for ev in events:
        t = ev['time']
        if ev['type'] == 'note_on':
            n = {
                'start': t, 'end': None,
                'note': ev['note'],
                'velocity': ev.get('velocity', 64),
                'channel': ev.get('channel', 0),
            }
            open_notes.setdefault((n['channel'], n['note']), []).append(n)
            notes.append(n)
        elif ev['type'] == 'note_off':
            stack = open_notes.get((ev.get('channel', 0), ev['note']))
            if stack:
                stack.pop(0)['end'] = t
        elif ev['type'] == 'sustain':
            sustains.append({'time': t, 'value': ev['value'],
                             'channel': ev.get('channel', 0)})

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
        evs.append({'time': n['start'], 'type': 'note_on', 'note': n['note'],
                    'velocity': n['velocity'], 'channel': n['channel']})
        evs.append({'time': n['end'], 'type': 'note_off', 'note': n['note'],
                    'channel': n['channel']})
    for s in sustains:
        evs.append({'time': s['time'], 'type': 'sustain', 'value': s['value'],
                    'channel': s['channel']})
    for z in (zone_hints or []):
        evs.append({'time': z['time'], 'type': 'zone', 'value': z['value']})

    # At identical timestamps: zone hints first, then releases (free the keys),
    # then sustain, then new presses.
    order = {'zone': 0, 'note_off': 1, 'sustain': 2, 'note_on': 3}
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
    # Pass 1: merge re-triggers that come faster than min_gap after the
    # previous same-pitch note ends (they'd just be dropped keystrokes in-game).
    merged = []
    last_by_pitch = {}  # (channel, note) -> last kept note dict
    for n in sorted(notes, key=lambda x: (x['start'], x['note'])):
        key = (n['channel'], n['note'])
        prev = last_by_pitch.get(key)
        if prev is not None and n['start'] - prev['end'] < min_gap:
            prev['end'] = max(prev['end'], n['end'])
            continue
        merged.append(n)
        last_by_pitch[key] = n
    # Pass 2: drop notes still too short to be audible after merging.
    return [n for n in merged if (n['end'] - n['start']) >= min_len]


def group_chords(notes, window, consistent):
    """Group notes whose starts fall in the same chord window."""
    groups = []
    if consistent:
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

        if current_zone in valid:
            chosen = current_zone            # hysteresis: don't toggle needlessly
        else:
            def fit(z):
                lo, hi = ZONE_RANGES[z]
                return sum(1 for n in group if lo <= n['note'] <= hi)
            chosen = max(valid, key=lambda z: (fit(z), -abs(z - current_zone), z == 0))

        if chosen != current_zone:
            zone_hints.append({'time': gstart, 'value': chosen})
            current_zone = chosen

        lo, hi = ZONE_RANGES[chosen]
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


def assign_auto_parts(notes, sustains, n_parts=2):
    """Auto-categorize notes into channels by musical role (skyline split).

        channel 0 = melody       (highest voice)
        channel 1 = accompaniment / harmony
        channel 2 = bass         (lowest voice) — only when n_parts >= 3

    A note's role is decided at its onset by whether it is the highest / lowest
    pitch *sounding* at that instant (counting notes still ringing from before),
    so a sustained melody line keeps its role while lower notes come and go.

    Returns the (possibly duplicated) sustain list; note channels are set
    in place.
    """
    if not notes:
        return sustains

    ordered = sorted(notes, key=lambda n: n['start'])
    active = []
    for t, grp in groupby(ordered, key=lambda n: n['start']):
        grp = list(grp)
        active = [a for a in active if a['end'] > t]
        active.extend(grp)
        hi = max(a['note'] for a in active)
        lo = min(a['note'] for a in active)
        for n in grp:
            if n['note'] == hi:
                n['channel'] = 0                       # melody (top voice wins)
            elif n_parts >= 3 and n['note'] == lo:
                n['channel'] = 2                       # bass
            else:
                n['channel'] = 1                       # accompaniment / harmony

    # Sustain pedal is global in-game: give every part a copy so whichever
    # part a player selects still receives pedal events.
    parts = sorted(set(n['channel'] for n in notes))
    doubled = []
    for s in sustains:
        for ch in parts:
            doubled.append({'time': s['time'], 'value': s['value'], 'channel': ch})
    return doubled


def split_duet(notes, sustains, split_note):
    """Split into Low (channel 0) / High (channel 1) parts."""
    for n in notes:
        n['channel'] = 0 if n['note'] < split_note else 1
    # Sustain pedal is global in-game; give both parts a copy so whichever
    # part is active still gets pedal events (simulator dedupes state).
    doubled = []
    for s in sustains:
        doubled.append({'time': s['time'], 'value': s['value'], 'channel': 0})
        doubled.append({'time': s['time'], 'value': s['value'], 'channel': 1})
    return doubled


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

    # 2. Pitch range mapping
    lo = max(ABS_LOW, min(settings.range_low, settings.range_high))
    hi = min(ABS_HIGH, max(settings.range_low, settings.range_high))
    if settings.proportional_remap:
        proportional_remap(notes, lo, hi)
    else:
        fold_into_range(notes, lo, hi, voice_aware=settings.voice_aware)

    # 3. Note thinning
    if settings.note_thinning:
        notes = thin_notes(notes, settings.thinning_gap, settings.thinning_min_len)

    # 4. Chord limiting / culling / melody priority
    if (settings.max_chord_notes < 5 or settings.cull_low_priority
            or settings.consistent_windows or settings.prioritize_melody):
        notes = limit_chords(notes, settings)

    # 5. Octave-zone planning (melody-lock takes precedence over phrase-gap)
    zone_hints = []
    if settings.melody_lock:
        zone_hints, notes = apply_melody_lock(notes, settings.chord_window,
                                              settings.melody_lock_mode)
    elif settings.phrase_gap_shifting:
        zone_hints = apply_phrase_zones(notes, settings.phrase_gap)

    # 6. Final safety: everything must be physically reachable
    fold_into_range(notes, ABS_LOW, ABS_HIGH)

    # 7. Channel assignment (auto-split by role supersedes the fixed duet split)
    if settings.auto_split:
        sustains = assign_auto_parts(notes, sustains, settings.auto_split_parts)
    elif settings.duet_mode:
        sustains = split_duet(notes, sustains, settings.duet_split_note)

    return notes_to_events(notes, sustains, zone_hints)
