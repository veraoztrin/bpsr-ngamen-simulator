from types import SimpleNamespace

from live_midi import LiveMidiListener


class FakeSimulator:
    def __init__(self):
        self.pressed = []
        self.released = []
        self.sustain = []
        self.release_all_calls = 0
        self.accept = True

    def press_note(self, note):
        self.pressed.append(note)
        return self.accept

    def release_note(self, note):
        self.released.append(note)

    def set_sustain(self, active):
        self.sustain.append(active)

    def release_all(self):
        self.release_all_calls += 1


def test_stop_releases_only_live_owned_state():
    simulator = FakeSimulator()
    restored = []
    listener = LiveMidiListener(simulator, lambda: restored.append(True))
    listener._on_message(
        SimpleNamespace(type="note_on", note=60, velocity=90))
    listener._on_message(
        SimpleNamespace(type="control_change", control=64, value=127))

    listener.stop_listening()

    assert simulator.released == [60]
    assert simulator.sustain[-1] is False
    assert simulator.release_all_calls == 0
    assert restored == [True]


def test_rejected_live_press_does_not_own_later_note_off():
    simulator = FakeSimulator()
    simulator.accept = False
    listener = LiveMidiListener(simulator)
    listener._on_message(
        SimpleNamespace(type="note_on", note=60, velocity=90))
    listener._on_message(
        SimpleNamespace(type="note_off", note=60, velocity=0))

    assert simulator.released == []
