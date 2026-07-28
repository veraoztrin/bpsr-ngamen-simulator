"""Focused playback-state and Win32 global-hotkey lifecycle tests."""

import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from player import MidiPlayer
import hotkeys


PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name} {detail}")


class StatefulSimulator:
    def __init__(self):
        self.log = []
        self.sustain_active = False
        self.current_octave_shift = 0

    def press_note(self, note):
        self.log.append(("press", note))

    def release_note(self, note):
        self.log.append(("release", note))

    def set_sustain(self, value):
        value = bool(value)
        if value != self.sustain_active:
            self.log.append(("sustain", value))
            self.sustain_active = value

    def set_octave_shift(self, value):
        if value != self.current_octave_shift:
            self.log.append(("zone", value))
            self.current_octave_shift = value

    def release_all(self):
        self.log.append(("all",))
        self.sustain_active = False
        self.current_octave_shift = 0


class FocusSimulator(StatefulSimulator):
    def __init__(self):
        super().__init__()
        self.focus_guard_enabled = True
        self.target_window_text = "Blue Protocol"
        self.focused = False

    def is_target_focused(self):
        return self.focused


def _state_events():
    return [
        {"time": 0.0, "type": "zone", "value": 1},
        {"time": 0.0, "type": "sustain", "value": True, "channel": 0},
        {"time": 0.4, "type": "note_on", "note": 72, "channel": 0},
        {"time": 0.5, "type": "note_off", "note": 72, "channel": 0},
    ]


def test_pause_resume_restores_global_state():
    player = MidiPlayer()
    player.simulator = StatefulSimulator()
    player.load_events(_state_events(), [0])
    player.play()
    time.sleep(0.04)
    player.pause()
    player.simulator.log.clear()

    player.play()
    restored = list(player.simulator.log)
    check("resume restores the octave-zone modifier",
          ("zone", 1) in restored, f"log={restored}")
    check("resume restores an active sustain pedal",
          ("sustain", True) in restored, f"log={restored}")
    player.stop()


def test_pause_during_countdown_never_shows_negative_time():
    player = MidiPlayer()
    player.simulator = StatefulSimulator()
    player.load_events([
        {"time": 0.0, "type": "note_on", "note": 60, "channel": 0},
        {"time": 1.0, "type": "note_off", "note": 60, "channel": 0},
    ], [0])
    player.play(delay_seconds=0.5)
    time.sleep(0.02)
    player.pause()
    check("pausing during the countdown displays 0:00",
          player.get_current_time() == 0.0,
          f"time={player.get_current_time()}")
    player.play()
    check("resuming a paused countdown preserves the countdown",
          player.is_syncing, f"start={player.start_time}")
    player.stop()


def test_play_waits_for_game_focus_without_skipping():
    player = MidiPlayer()
    player.simulator = FocusSimulator()
    player.load_events([
        {"time": 0.0, "type": "note_on", "note": 60, "channel": 0},
        {"time": 0.2, "type": "note_off", "note": 60, "channel": 0},
    ], [0])
    player.play()
    time.sleep(0.06)
    check("playback waits when focus safety blocks the game",
          player.is_playing and player.is_focus_waiting
          and ("press", 60) not in player.simulator.log,
          f"log={player.simulator.log}")
    check("the song clock stays at the first event while focus is blocked",
          player.get_current_time() < 0.02,
          f"time={player.get_current_time()}")

    player.simulator.focused = True
    deadline = time.time() + 1.0
    while ("press", 60) not in player.simulator.log and time.time() < deadline:
        time.sleep(0.01)
    check("playback starts from the first note when game focus returns",
          ("press", 60) in player.simulator.log,
          f"log={player.simulator.log}")
    player.stop()


def test_sustain_is_owned_per_channel():
    player = MidiPlayer()
    player.simulator = StatefulSimulator()
    player.load_events([
        {"time": 0.0, "type": "sustain", "value": True, "channel": 0},
        {"time": 0.0, "type": "sustain", "value": True, "channel": 1},
        {"time": 1.0, "type": "note_on", "note": 60, "channel": 0},
    ], [0, 1])
    player.play()
    time.sleep(0.04)
    player.simulator.log.clear()

    player.set_active_channels([1])
    check("muting one pedal-owning channel keeps the other channel sustained",
          player.simulator.sustain_active
          and ("sustain", False) not in player.simulator.log,
          f"log={player.simulator.log}")
    player.set_active_channels([])
    check("sustain releases when no active channel owns it",
          not player.simulator.sustain_active, f"log={player.simulator.log}")
    player.stop()


def test_seek_reconstructs_each_channel_pedal():
    player = MidiPlayer()
    player.simulator = StatefulSimulator()
    player.load_events([
        {"time": 0.1, "type": "sustain", "value": True, "channel": 0},
        {"time": 0.2, "type": "sustain", "value": True, "channel": 1},
        {"time": 0.3, "type": "sustain", "value": False, "channel": 0},
        {"time": 1.0, "type": "note_on", "note": 60, "channel": 0},
    ], [0, 1])
    player.seek(0.5)
    check("seek keeps sustain from a channel whose pedal is still down",
          player.simulator.sustain_active
          and player._sustain_by_channel == {1},
          f"owners={player._sustain_by_channel}, log={player.simulator.log}")
    player.stop()


def test_focus_release_can_restore_state():
    player = MidiPlayer()
    player.simulator = StatefulSimulator()
    player.load_events(_state_events(), [0])
    player.seek(0.2)
    player.release_output_state()
    check("focus-loss release physically clears global keys",
          not player.simulator.sustain_active
          and player.simulator.current_octave_shift == 0)
    player.restore_output_state()
    check("focus regain restores pedal and zone",
          player.simulator.sustain_active
          and player.simulator.current_octave_shift == 1,
          f"log={player.simulator.log}")
    player.stop()


class FakeKernel32:
    @staticmethod
    def GetCurrentThreadId():
        return 1234


class FakeUser32:
    def __init__(self, fail_vk=None):
        self.fail_vk = fail_vk
        self.messages = queue.Queue()
        self.registrations = []
        self.unregistered = []

    def RegisterHotKey(self, _window, hotkey_id, modifiers, vk):
        self.registrations.append((hotkey_id, modifiers, vk))
        return vk != self.fail_vk

    def UnregisterHotKey(self, _window, hotkey_id):
        self.unregistered.append(hotkey_id)
        return True

    def PostThreadMessageW(self, _thread_id, message, wparam, lparam):
        self.messages.put((message, wparam, lparam))
        return True

    def GetMessageW(self, msg_pointer, _window, _minimum, _maximum):
        message, wparam, lparam = self.messages.get(timeout=1.0)
        if message == hotkeys.WM_QUIT:
            return 0
        msg_pointer._obj.message = message
        msg_pointer._obj.wParam = wparam
        msg_pointer._obj.lParam = lparam
        return 1


class FakePollingUser32(FakeUser32):
    def __init__(self, fail_vk):
        super().__init__(fail_vk=fail_vk)
        self.down = set()

    def GetAsyncKeyState(self, vk):
        return 0x8000 if vk in self.down else 0


def test_hotkey_registration_dispatch_and_shutdown():
    original_user32, original_kernel32 = hotkeys.user32, hotkeys.kernel32
    fake = FakeUser32()
    fired = threading.Event()
    try:
        hotkeys.user32 = fake
        hotkeys.kernel32 = FakeKernel32()
        listener = hotkeys.GlobalHotkeys({hotkeys.VK_F9: fired.set})
        started = listener.start()
        fake.PostThreadMessageW(1234, hotkeys.WM_HOTKEY, 1, 0)
        dispatched = fired.wait(timeout=1.0)
        stopped = listener.stop()
        check("global hotkey registration succeeds", started)
        check("hotkey messages dispatch their callback", dispatched)
        check("hotkeys use the no-repeat Win32 modifier",
              fake.registrations == [
                  (1, hotkeys.MOD_NOREPEAT, hotkeys.VK_F9)],
              f"registrations={fake.registrations}")
        check("hotkey listener shuts down and unregisters cleanly",
              stopped and fake.unregistered == [1],
              f"unregistered={fake.unregistered}")
    finally:
        hotkeys.user32 = original_user32
        hotkeys.kernel32 = original_kernel32


def test_partial_hotkey_registration_is_rejected():
    original_user32, original_kernel32 = hotkeys.user32, hotkeys.kernel32
    fake = FakeUser32(fail_vk=hotkeys.VK_F10)
    try:
        hotkeys.user32 = fake
        hotkeys.kernel32 = FakeKernel32()
        listener = hotkeys.GlobalHotkeys({
            hotkeys.VK_F9: lambda: None,
            hotkeys.VK_F10: lambda: None,
            hotkeys.VK_F11: lambda: None,
        })
        started = listener.start()
        listener.stop()
        check("a partial F9-F11 registration is reported as unavailable",
              not started and listener.failed_keys == [hotkeys.VK_F10],
              f"failed={listener.failed_keys}")
        check("successful partial registrations are rolled back",
              fake.unregistered == [1, 3],
              f"unregistered={fake.unregistered}")
    finally:
        hotkeys.user32 = original_user32
        hotkeys.kernel32 = original_kernel32


def test_conflicted_hotkeys_fall_back_to_edge_polling():
    original_user32, original_kernel32 = hotkeys.user32, hotkeys.kernel32
    fake = FakePollingUser32(fail_vk=hotkeys.VK_F10)
    fired = []
    try:
        hotkeys.user32 = fake
        hotkeys.kernel32 = FakeKernel32()
        listener = hotkeys.GlobalHotkeys({
            hotkeys.VK_F9: lambda: fired.append("play"),
            hotkeys.VK_F10: lambda: fired.append("pause"),
            hotkeys.VK_F11: lambda: fired.append("stop"),
        })
        started = listener.start()
        mode = listener.mode
        fake.down.add(hotkeys.VK_F9)
        time.sleep(0.06)
        time.sleep(0.04)  # held key must not repeat
        fake.down.remove(hotkeys.VK_F9)
        time.sleep(0.04)
        fake.down.add(hotkeys.VK_F9)
        time.sleep(0.06)
        listener.stop()
        check("registration conflicts enable the polling fallback",
              started and mode == "poll", f"started={started}, mode={mode}")
        check("polling fires once per physical key press",
              fired == ["play", "play"], f"fired={fired}")
    finally:
        hotkeys.user32 = original_user32
        hotkeys.kernel32 = original_kernel32


if __name__ == "__main__":
    test_pause_resume_restores_global_state()
    test_pause_during_countdown_never_shows_negative_time()
    test_play_waits_for_game_focus_without_skipping()
    test_sustain_is_owned_per_channel()
    test_seek_reconstructs_each_channel_pedal()
    test_focus_release_can_restore_state()
    test_hotkey_registration_dispatch_and_shutdown()
    test_partial_hotkey_registration_is_rejected()
    test_conflicted_hotkeys_fall_back_to_edge_polling()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
