# live_midi.py
#
# Live MIDI keyboard passthrough: listens on a connected MIDI input device
# and forwards note events straight to the BPSR input simulator, so you can
# play the in-game instrument with a real MIDI keyboard.
#
# (This module was referenced by gui.py but missing from the original repo,
#  a casualty of the corrupted .gitignore; recreated for v0.4.)

try:
    import mido
except ImportError:
    mido = None


class LiveMidiListener:
    def __init__(self, simulator, on_state_release=None):
        self.simulator = simulator
        self.on_state_release = on_state_release
        self.port = None
        self.device_name = None
        self._active_notes = {}
        self._sustain_active = False

    def get_devices(self):
        """List available MIDI input device names."""
        if mido is None:
            return []
        try:
            # De-duplicate while preserving order
            return list(dict.fromkeys(mido.get_input_names()))
        except Exception as e:
            print(f"Could not enumerate MIDI devices: {e}")
            return []

    def start_listening(self, device_name):
        self.stop_listening()
        if mido is None:
            print("mido (with python-rtmidi) is required for live MIDI input.")
            return
        try:
            self.port = mido.open_input(device_name, callback=self._on_message)
            self.device_name = device_name
            print(f"Listening on MIDI device: {device_name}")
        except Exception as e:
            print(f"Could not open MIDI device '{device_name}': {e}")
            self.port = None
            self.device_name = None

    def _on_message(self, msg):
        if self.simulator is None:
            return
        try:
            if msg.type == 'note_on' and msg.velocity > 0:
                accepted = self.simulator.press_note(msg.note)
                if accepted is not False:
                    self._active_notes[msg.note] = (
                        self._active_notes.get(msg.note, 0) + 1)
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                count = self._active_notes.get(msg.note, 0)
                if count > 0:
                    self.simulator.release_note(msg.note)
                    if count == 1:
                        del self._active_notes[msg.note]
                    else:
                        self._active_notes[msg.note] = count - 1
            elif msg.type == 'control_change' and msg.control == 64:
                active = msg.value >= 64
                self.simulator.set_sustain(active)
                self._sustain_active = active
                if not active:
                    self._restore_shared_state()
        except Exception as e:
            print(f"Live MIDI error: {e}")

    def _restore_shared_state(self):
        if self.on_state_release:
            try:
                self.on_state_release()
            except Exception as e:
                print(f"Could not restore playback state after live MIDI: {e}")

    def stop_listening(self):
        if self.port is not None:
            try:
                self.port.close()
            except Exception:
                pass
        self.port = None
        self.device_name = None
        if self.simulator:
            # Only release notes this listener successfully pressed. Calling
            # release_all() here used to cut off notes owned by MIDI playback.
            for note, count in list(self._active_notes.items()):
                for _ in range(count):
                    self.simulator.release_note(note)
            if self._sustain_active:
                self.simulator.set_sustain(False)
        had_state = bool(self._active_notes or self._sustain_active)
        self._active_notes.clear()
        self._sustain_active = False
        if had_state:
            self._restore_shared_state()
