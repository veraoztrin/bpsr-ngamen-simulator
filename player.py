import atexit
import time
import threading
import math
try:
    from input_simulator import BPSRInputSimulator
except Exception:
    # Non-Windows platform (no ctypes.windll): run without input simulation.
    BPSRInputSimulator = None


def _request_high_resolution_timer():
    """Ask Windows for a 1 ms scheduler tick.

    Without this the default timer granularity is ~15.6 ms, so every
    Event.wait()/sleep() in the playback loop can overshoot badly.  The old
    code compensated by busy-spinning the last stretch of every gap, which
    pinned a core and held the GIL away from the Tk main loop - that is what
    made the window feel sluggish while a song was playing.  With a 1 ms tick
    we can sleep almost the whole gap and spin only for the final ~2 ms.
    """
    try:
        import ctypes
        winmm = ctypes.windll.winmm
    except Exception:
        return  # not Windows, or winmm unavailable - sleeps are fine as-is
    if winmm.timeBeginPeriod(1) == 0:  # TIMERR_NOERROR
        atexit.register(lambda: winmm.timeEndPeriod(1))


def _measure_sleep_margin(samples=5, floor=0.002, ceiling=0.020):
    """How early we must stop sleeping to never overshoot a note.

    Don't assume the 1 ms request above was honoured - a locked-down policy or
    an older Windows build can leave the tick at 15.6 ms, and sleeping too
    close to the target would then land the note *late*, which is far worse
    than a slightly longer spin.  Measure what this machine actually delivers
    and keep a 50% safety margin on top.
    """
    event = threading.Event()
    worst = 0.0
    for _ in range(samples):
        started = time.perf_counter()
        event.wait(0.001)
        worst = max(worst, time.perf_counter() - started - 0.001)
    return min(max(worst * 1.5, floor), ceiling)


_request_high_resolution_timer()
SLEEP_MARGIN = _measure_sleep_margin()

class MidiPlayer:
    def __init__(self, on_output_error=None):
        self.simulator = BPSRInputSimulator() if BPSRInputSimulator else None
        self.on_output_error = on_output_error
        self.events = []
        self.active_channels = set()
        # Optional song-time gates for multiplayer parts.  A channel with a
        # 10.0 entry remains silent until the shared playhead reaches 10s;
        # its events are not shifted, so it joins in at the authored position.
        self.channel_start_times = {}
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
        # Sustain is physically one Space key, but MIDI pedal state belongs to
        # each channel. Keep every channel's logical state so muting one part
        # cannot release a pedal that another active part still owns.
        self._sustain_by_channel = set()
        # Zone hints are global. release_all() physically resets the modifier,
        # so retain the requested zone for pause/focus-loss restoration.
        self._current_zone = 0
        self.is_focus_waiting = False
        self._focus_wait_started = None
        self.current_event_idx = 0
        self.start_time = 0.0
        self.pause_time = 0.0
        self.time_offset = 0.0
        
        # How much of each gap is spun rather than slept.  Calibrated to this
        # machine's real timer granularity at import (2 ms on anything modern).
        self.sleep_threshold = SLEEP_MARGIN
        self.strict_timing = False

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
                if self.is_playing:
                    if value == 0:
                        self.simulator.set_octave_shift(self._current_zone)
                    self.simulator.set_sustain(
                        self._sustain_output_active())

    def _channel_gate_open(self, channel, song_time=None):
        if song_time is None:
            song_time = self.get_current_time()
        return song_time + 1e-9 >= self.channel_start_times.get(channel, 0.0)

    def _sustain_output_active(self, song_time=None):
        return any(
            channel in self.active_channels
            and self._channel_gate_open(channel, song_time)
            for channel in self._sustain_by_channel
        )

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
        if self.is_focus_waiting and self._focus_wait_started is not None:
            position = self._focus_wait_started - self.start_time
        elif self.is_paused:
            position = self.pause_time - self.start_time
        else:
            position = time.perf_counter() - self.start_time
        return max(0.0, min(self.get_total_time(), position))

    def load_events(self, events, active_channels=None,
                    channel_start_times=None):
        self.stop()
        with self._state_lock:
            self.events = events
            if active_channels is not None:
                self.active_channels = set(active_channels)
            self.channel_start_times = {}
            if channel_start_times is not None:
                self._set_channel_start_times_locked(channel_start_times)
            self.current_event_idx = 0

    def _set_channel_start_times_locked(self, start_times):
        cleaned = {}
        for channel, value in dict(start_times).items():
            if (isinstance(channel, bool) or not isinstance(channel, int)
                    or not 0 <= channel <= 15
                    or isinstance(value, bool)):
                raise ValueError("Invalid channel start time.")
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ValueError("Invalid channel start time.") from None
            if not math.isfinite(value) or value < 0.0:
                raise ValueError("Invalid channel start time.")
            if value > 0.0:
                cleaned[channel] = value
        self.channel_start_times = cleaned

    def set_channel_start_times(self, start_times):
        """Set per-channel song positions before which output stays muted."""
        with self._state_lock:
            self._set_channel_start_times_locked(start_times)
            sustain = self._sustain_output_active()
        if self.simulator:
            self.simulator.set_sustain(sustain)

    def set_active_channels(self, channels):
        channels = set(channels)
        releases = []
        with self._state_lock:
            sustain_before = self._sustain_output_active()
            removed = self.active_channels - channels
            self.active_channels = channels
            sustain_after = self._sustain_output_active()
            for key in list(self._active_notes):
                if key[0] in removed:
                    releases.extend(self._active_notes.pop(key))
        if self.simulator:
            for note in releases:
                self.simulator.release_note(note)
            if sustain_before != sustain_after:
                self.simulator.set_sustain(sustain_after)

    def release_output_state(self):
        """Release physical keys while retaining resumable pedal/zone state."""
        with self._state_lock:
            self._active_notes.clear()
        if self.simulator:
            self.simulator.release_all()

    def restore_output_state(self, song_time=None):
        """Restore global MIDI state after pause or temporary focus loss."""
        if not self.simulator:
            return
        with self._state_lock:
            zone = self._current_zone
            sustain = self._sustain_output_active(song_time)
            transpose = self.transpose
        if transpose == 0:
            self.simulator.set_octave_shift(zone)
        self.simulator.set_sustain(sustain)

    def _cancel_worker(self):
        """Stop and join the current worker using a per-thread cancellation event."""
        with self._state_lock:
            thread = self.thread
            cancel = self._cancel_event
            cancel.set()
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join()

    def _output_focus_ready(self):
        simulator = self.simulator
        if not simulator or not getattr(simulator, "focus_guard_enabled", False):
            return True
        checker = getattr(simulator, "is_target_focused", None)
        return bool(checker and checker())

    def _wait_for_output_focus(self, cancel):
        """Freeze the song clock while focus safety blocks key output."""
        if self._output_focus_ready():
            return True
        blocked_at = time.perf_counter()
        with self._state_lock:
            self.is_focus_waiting = True
            self._focus_wait_started = blocked_at
        try:
            while not cancel.is_set() and not self._output_focus_ready():
                cancel.wait(0.025)
        finally:
            elapsed = time.perf_counter() - blocked_at
            with self._state_lock:
                # Even cancellation needs this adjustment: pause_time was
                # captured at the end of the blocked interval.
                self.start_time += elapsed
                self.is_focus_waiting = False
                self._focus_wait_started = None
        return not cancel.is_set()

    def play(self, delay_seconds=0.0, strict_timing=False):
        # A paused worker is always joined before pause() returns, but joining
        # here as well makes programmatic state changes safe.
        with self._state_lock:
            if self.is_playing:
                return False
            if not self.active_channels:
                return False
        try:
            delay_seconds = float(delay_seconds)
        except (TypeError, ValueError):
            delay_seconds = 0.0
        if not math.isfinite(delay_seconds):
            delay_seconds = 0.0
        delay_seconds = min(max(delay_seconds, 0.0), 30.0)
        self._cancel_worker()
        with self._state_lock:
            if self.is_paused:
                self.is_paused = False
                self.restore_output_state()
                # Restoring Shift/Ctrl can intentionally wait for the game's
                # modifier timing. Count that as paused time, not song time.
                self.start_time += time.perf_counter() - self.pause_time
            else:
                if not self.events:
                    return False
                self.current_event_idx = 0
                # delay_seconds allows synchronization
                self.start_time = time.perf_counter() + delay_seconds
                self.time_offset = 0.0

            self.stop_requested = False
            self.is_playing = True
            self.strict_timing = bool(strict_timing)
            self.is_focus_waiting = False
            self._focus_wait_started = None
            self._cancel_event = threading.Event()
            cancel = self._cancel_event
            self.thread = threading.Thread(
                target=self._playback_loop, args=(cancel,), daemon=True)
            self.thread.start()
            return True

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
        self.release_output_state()

        sustain_by_channel, last_zone, idx = set(), 0, 0
        for i, ev in enumerate(self.events):
            # Events exactly on the target belong to the resumed playback.
            if ev['time'] >= target_time:
                break
            idx = i + 1
            if ev['type'] == 'sustain':
                channel = ev.get('channel', 0)
                if ev['value']:
                    sustain_by_channel.add(channel)
                else:
                    sustain_by_channel.discard(channel)
            elif ev['type'] == 'zone':
                last_zone = ev['value']
        self.current_event_idx = idx
        with self._state_lock:
            self._sustain_by_channel = sustain_by_channel
            self._current_zone = last_zone

        self.restore_output_state(target_time)

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
            self.release_output_state()

    def stop(self):
        self.stop_requested = True
        self.is_playing = False
        self.is_paused = False
        self._cancel_worker()
        if self.simulator:
            self.simulator.release_all()
        with self._state_lock:
            self._active_notes.clear()
            self._sustain_by_channel.clear()
            self._current_zone = 0
            self.is_focus_waiting = False
            self._focus_wait_started = None
            self.current_event_idx = 0

    def _accurate_delay(self, target_time, cancel):
        while True:
            if cancel.is_set():
                break
            now = time.perf_counter()
            diff = target_time - now
            if not math.isfinite(diff):
                cancel.set()
                break
            if diff <= 0:
                break
            if diff > self.sleep_threshold:
                # Sleep all but the last sleep_threshold of the gap, capped at
                # 50 ms per slice so stop() stays immediately interruptible
                # even during a long sync countdown.  The calibrated margin
                # keeps the wakeup safely on the early side of the target.
                cancel.wait(min(diff - self.sleep_threshold, 0.05))
            else:
                # Final sub-threshold stretch: still a spin, because no OS
                # sleep is accurate here, but sleep(0) hands the GIL to the
                # Tk main loop on every pass instead of holding it for a full
                # 5 ms interpreter switch interval.
                time.sleep(0)

    def _playback_loop(self, cancel):
        focus_error = None
        while self.current_event_idx < len(self.events):
            if cancel.is_set():
                break

            ev = self.events[self.current_event_idx]
            
            while not cancel.is_set():
                target_time = self.start_time + ev['time']
                self._accurate_delay(target_time, cancel)
                if cancel.is_set() or self._output_focus_ready():
                    break
                if self.strict_timing:
                    focus_error = (
                        "Synchronized playback stopped because the target "
                        "game window was not focused. Refocus the game and "
                        "ask the host to start again.")
                    break
                if not self._wait_for_output_focus(cancel):
                    break
            
            if cancel.is_set() or focus_error:
                break

            with self._state_lock:
                if ev['type'] == 'zone':
                    # Pre-emptive octave zone hint from the arranger
                    # (phrase-gap shifting): toggle the modifier during silence.
                    self._current_zone = ev['value']
                    if self.simulator and self.transpose == 0:
                        self.simulator.set_octave_shift(ev['value'])
                elif 'channel' in ev:
                    key = (ev['channel'], ev.get('note'))
                    if ev['type'] == 'note_on':
                        gate_open = self._channel_gate_open(
                            ev['channel'], ev['time'])
                        if (ev['channel'] in self.active_channels
                                and gate_open and self.simulator):
                            # Pedal-down may have occurred before this part's
                            # entrance. Restore it immediately before the first
                            # audible note after the gate.
                            self.simulator.set_sustain(
                                self._sustain_output_active(ev['time']))
                            played_note = ev['note'] + self.transpose
                            accepted = self.simulator.press_note(played_note)
                            # Only an explicit False means focus/range safety
                            # rejected the press; simple test doubles may return
                            # None after successfully recording it.
                            if accepted is not False:
                                self._active_notes.setdefault(key, []).append(played_note)
                    elif ev['type'] == 'note_off':
                        played = self._active_notes.get(key)
                        if played and self.simulator:
                            played_note = played.pop(0)
                            if not played:
                                del self._active_notes[key]
                            self.simulator.release_note(played_note)
                    elif ev['type'] == 'sustain':
                        channel = ev['channel']
                        if ev['value']:
                            self._sustain_by_channel.add(channel)
                        else:
                            self._sustain_by_channel.discard(channel)
                        if self.simulator:
                            self.simulator.set_sustain(
                                self._sustain_output_active(ev['time']))

            self.current_event_idx += 1

        completed = self.current_event_idx >= len(self.events)
        if focus_error or (not cancel.is_set() and completed):
            with self._state_lock:
                self.is_playing = False
                self.is_paused = False
            if self.simulator:
                self.simulator.release_all()
            self._active_notes.clear()
            self._sustain_by_channel.clear()
            self._current_zone = 0
            if focus_error and self.on_output_error:
                try:
                    self.on_output_error(focus_error)
                except Exception:
                    pass
