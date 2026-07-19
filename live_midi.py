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
    def __init__(self, simulator):
        self.simulator = simulator
        self.port = None
        self.device_name = None

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
                self.simulator.press_note(msg.note)
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                self.simulator.release_note(msg.note)
            elif msg.type == 'control_change' and msg.control == 64:
                self.simulator.set_sustain(msg.value >= 64)
        except Exception as e:
            print(f"Live MIDI error: {e}")

    def stop_listening(self):
        if self.port is not None:
            try:
                self.port.close()
            except Exception:
                pass
            self.port = None
            self.device_name = None
            if self.simulator:
                self.simulator.release_all()
