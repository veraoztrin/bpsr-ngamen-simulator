import time
import threading
try:
    from input_simulator import BPSRInputSimulator
except Exception:
    # Non-Windows platform (no ctypes.windll): run without input simulation.
    BPSRInputSimulator = None

class MidiPlayer:
    def __init__(self):
        self.simulator = BPSRInputSimulator() if BPSRInputSimulator else None
        self.events = []
        self.active_channels = set()
        self._transpose = 0
        
        self.is_playing = False
        self.is_paused = False
        self.stop_requested = False
        
        self.thread = None
        self._cancel_event = threading.Event()
        self._state_lock = threading.RLock()
        # (channel, source_note) -> transposed notes actually pressed.  Releases
        # must use this snapshot, not the current transpose/channel selection.
        self._active_notes = {}
        self.current_event_idx = 0
        self.start_time = 0.0
        self.pause_time = 0.0
        self.time_offset = 0.0
        
        self.sleep_threshold = 0.002 

    @property
    def transpose(self):
        return self._transpose

    @transpose.setter
    def transpose(self, value):
        self.set_transpose(value)

    def set_transpose(self, value):
        """Change transpose without leaving notes pressed under the old value."""
        value = int(value)
        with self._state_lock:
            if value == self._transpose:
                return
            self._transpose = value
            self._active_notes.clear()
            if self.simulator:
                self.simulator.release_all()

    @property
    def is_syncing(self):
        return self.is_playing and not self.is_paused and time.perf_counter() < self.start_time

    def get_total_time(self):
        if not self.events:
            return 0.0
        return self.events[-1]['time']

    def get_current_time(self):
        if not self.is_playing and not self.is_paused:
            return 0.0
        if self.is_syncing:
            return 0.0
        if self.is_paused:
            return self.pause_time - self.start_time
        return time.perf_counter() - self.start_time

    def load_events(self, events, active_channels=None):
        self.stop()
        with self._state_lock:
            self.events = events
            if active_channels is not None:
                self.active_channels = set(active_channels)
            self.current_event_idx = 0

    def set_active_channels(self, channels):
        channels = set(channels)
        releases = []
        with self._state_lock:
            removed = self.active_channels - channels
            self.active_channels = channels
            for key in list(self._active_notes):
                if key[0] in removed:
                    releases.extend(self._active_notes.pop(key))
        if self.simulator:
            for note in releases:
                self.simulator.release_note(note)

    def _cancel_worker(self):
        """Stop and join the current worker using a per-thread cancellation event."""
        with self._state_lock:
            thread = self.thread
            cancel = self._cancel_event
            cancel.set()
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join()

    def play(self, delay_seconds=0.0):
        # A paused worker is always joined before pause() returns, but joining
        # here as well makes programmatic state changes safe.
        with self._state_lock:
            if self.is_playing:
                return
        self._cancel_worker()
        with self._state_lock:
            if self.is_paused:
                self.is_paused = False
                self.start_time += time.perf_counter() - self.pause_time
            else:
                if not self.events:
                    return
                self.current_event_idx = 0
                # delay_seconds allows synchronization
                self.start_time = time.perf_counter() + delay_seconds
                self.time_offset = 0.0

            self.stop_requested = False
            self.is_playing = True
            self._cancel_event = threading.Event()
            cancel = self._cancel_event
            self.thread = threading.Thread(
                target=self._playback_loop, args=(cancel,), daemon=True)
            self.thread.start()

    def seek(self, target_time):
        """Jump to a specific point in the song (seconds).

        This doesn't decode audio, it simulates real key presses, so
        "seeking" means silently fast-forwarding through everything up to
        target_time rather than moving an audio playhead. Individual notes
        already sounding at that instant are intentionally not re-pressed
        (release_note() is reference-counted, so their eventual note_off is
        a harmless no-op) - but the sustain pedal and octave-shift modifier
        are global state, so we replay whatever their last value was before
        target_time, or resuming mid-song would drop the pedal / snap to
        the wrong octave.

        Preserves whatever state you were in: playing stays playing (from
        the new spot), paused stays paused (at the new spot, ready for the
        next Play), and seeking while stopped arms a paused-at-that-point
        state the same way.
        """
        if not self.events:
            return
        target_time = max(0.0, min(target_time, self.get_total_time()))
        now_before_cancel = time.perf_counter()
        was_playing = self.is_playing and not self.is_paused
        countdown_remaining = (
            max(0.0, self.start_time - now_before_cancel) if self.is_syncing else 0.0)

        # Halt the running thread (if any) and release whatever's currently
        # held, without resetting current_event_idx the way stop() does.
        self.stop_requested = True
        self._cancel_worker()
        if self.simulator:
            self.simulator.release_all()
        self._active_notes.clear()

        last_sustain, last_zone, idx = 0, 0, 0
        for i, ev in enumerate(self.events):
            # Events exactly on the target belong to the resumed playback.
            if ev['time'] >= target_time:
                break
            idx = i + 1
            if (ev['type'] == 'sustain'
                    and ev.get('channel', 0) in self.active_channels):
                last_sustain = ev['value']
            elif ev['type'] == 'zone':
                last_zone = ev['value']
        self.current_event_idx = idx

        if self.simulator:
            if self.transpose == 0:
                self.simulator.set_octave_shift(last_zone)
            self.simulator.set_sustain(bool(last_sustain))

        self.stop_requested = False
        now = time.perf_counter()
        if was_playing:
            self.start_time = now + countdown_remaining - target_time
            self.is_paused = False
            self.is_playing = True
            self._cancel_event = threading.Event()
            cancel = self._cancel_event
            self.thread = threading.Thread(
                target=self._playback_loop, args=(cancel,), daemon=True)
            self.thread.start()
        else:
            # Stopped or already paused: land in "paused at this position"
            # so get_current_time()/the progress bar reflect it immediately,
            # and the next Play() resumes from here (play()'s is_paused
            # branch just offsets start_time by the time since pause_time).
            self.is_playing = False
            self.is_paused = True
            self.pause_time = now
            self.start_time = now - target_time

    def pause(self):
        if self.is_playing and not self.is_paused:
            self.is_paused = True
            self.is_playing = False
            self.pause_time = time.perf_counter()
            self._cancel_worker()
            if self.simulator:
                self.simulator.release_all()
            self._active_notes.clear()

    def stop(self):
        self.stop_requested = True
        self.is_playing = False
        self.is_paused = False
        self._cancel_worker()
        if self.simulator:
            self.simulator.release_all()
        with self._state_lock:
            self._active_notes.clear()
            self.current_event_idx = 0

    def _accurate_delay(self, target_time, cancel):
        while True:
            if cancel.is_set():
                break
            now = time.perf_counter()
            diff = target_time - now
            if diff <= 0:
                break
            if diff > self.sleep_threshold:
                # Event.wait makes even a long sync countdown immediately
                # interruptible; the old half-gap sleep could outlive stop().
                cancel.wait(min(diff / 2.0, 0.05))
            else:
                pass

    def _playback_loop(self, cancel):
        while self.current_event_idx < len(self.events):
            if cancel.is_set():
                break

            ev = self.events[self.current_event_idx]
            
            target_time = self.start_time + ev['time']
            
            self._accurate_delay(target_time, cancel)
            
            if cancel.is_set():
                break

            with self._state_lock:
                if ev['type'] == 'zone':
                    # Pre-emptive octave zone hint from the arranger
                    # (phrase-gap shifting): toggle the modifier during silence.
                    if self.simulator and self.transpose == 0:
                        self.simulator.set_octave_shift(ev['value'])
                elif 'channel' in ev:
                    key = (ev['channel'], ev.get('note'))
                    if ev['type'] == 'note_on':
                        if ev['channel'] in self.active_channels and self.simulator:
                            played_note = ev['note'] + self.transpose
                            self.simulator.press_note(played_note)
                            self._active_notes.setdefault(key, []).append(played_note)
                    elif ev['type'] == 'note_off':
                        played = self._active_notes.get(key)
                        if played and self.simulator:
                            played_note = played.pop(0)
                            if not played:
                                del self._active_notes[key]
                            self.simulator.release_note(played_note)
                    elif ev['type'] == 'sustain':
                        if ev['channel'] in self.active_channels and self.simulator:
                            self.simulator.set_sustain(ev['value'])

            self.current_event_idx += 1

        if not cancel.is_set() and self.current_event_idx >= len(self.events):
            self.is_playing = False
            if self.simulator:
                self.simulator.release_all()
            self._active_notes.clear()
