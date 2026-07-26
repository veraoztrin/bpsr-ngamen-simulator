import ctypes
import time
import threading
from functools import wraps
from config import KEY_MAP, VK_LSHIFT, VK_LCONTROL, VK_SPACE, midi_to_note_name

SendInput = ctypes.windll.user32.SendInput
MapVirtualKey = ctypes.windll.user32.MapVirtualKeyW

# C struct definitions for Windows Input
PUL = ctypes.POINTER(ctypes.c_ulong)
class KeyBdInput(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort),
                ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", PUL)]

class HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong),
                ("wParamL", ctypes.c_short),
                ("wParamH", ctypes.c_ushort)]

class MouseInput(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long),
                ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", PUL)]

class Input_I(ctypes.Union):
    _fields_ = [("ki", KeyBdInput),
                ("mi", MouseInput),
                ("hi", HardwareInput)]

class Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong),
                ("ii", Input_I)]

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

def press_key(hexKeyCode):
    extra = ctypes.c_ulong(0)
    ii_ = Input_I()
    scan_code = MapVirtualKey(hexKeyCode, 0)
    ii_.ki = KeyBdInput(0, scan_code, KEYEVENTF_SCANCODE, 0, ctypes.pointer(extra))
    x = Input(ctypes.c_ulong(1), ii_)
    SendInput(1, ctypes.pointer(x), ctypes.sizeof(x))

def release_key(hexKeyCode):
    extra = ctypes.c_ulong(0)
    ii_ = Input_I()
    scan_code = MapVirtualKey(hexKeyCode, 0)
    ii_.ki = KeyBdInput(0, scan_code, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP, 0, ctypes.pointer(extra))
    x = Input(ctypes.c_ulong(1), ii_)
    SendInput(1, ctypes.pointer(x), ctypes.sizeof(x))

def tap_key(hexKeyCode, duration=0.01):
    press_key(hexKeyCode)
    time.sleep(duration)
    release_key(hexKeyCode)


def synchronized(method):
    """Serialize OS-key state shared by playback and live-MIDI callbacks."""
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper

class BPSRInputSimulator:
    def __init__(self):
        self._lock = threading.RLock()
        self.current_octave_shift = 0 # 0: normal, 1: High, -1: Low
        self.sustain_active = False
        self.key_refs = {}
        # Timing knobs (settable from the GUI):
        # shift_delay_ms: wait after toggling Shift/Ctrl before the next note,
        #                 so the game reliably registers the modifier change.
        # shift_hold_ms:  minimum time a modifier state is held before it may
        #                 be toggled again (prevents dropped toggles on fast runs).
        # retrigger_gap_ms: minimum time a key must be observed UP before it may
        #                 be pressed again. The game samples the keyboard once
        #                 per frame, so a release immediately followed by a press
        #                 reads as one uninterrupted hold - a repeated note
        #                 (C4 C4 C4 C4) then sounds as a single long C4. The
        #                 arranger already spaces scheduled playback; this is the
        #                 runtime backstop for live MIDI passthrough and for keys
        #                 shared by several soloed channels.
        self.shift_delay_ms = 30
        self.shift_hold_ms = 10
        self.retrigger_gap_ms = 25
        self._last_shift_change = 0.0
        self._last_release = {}  # vk_code -> perf_counter() of its last key-up
        # midi_note -> [vk_code, ...] actually pressed for the notes of that
        # pitch currently sounding. The octave modifier can change between a
        # note's press and its release, and re-deriving the key at release time
        # would then release a *different* key, leaving this one stuck down.
        self.note_keys = {}
        # Semitone offset added before the piano key lookup, so instruments
        # whose keyboard is the piano layout transposed (e.g. Bass = -2 octaves)
        # press the right key. 0 = piano/guitar.
        self.key_offset = 0

    @synchronized
    def set_octave_shift(self, target_shift):
        if self.current_octave_shift == target_shift:
            return

        # Respect the minimum hold time since the last modifier change
        if self.shift_hold_ms > 0:
            since = time.perf_counter() - self._last_shift_change
            remaining = (self.shift_hold_ms / 1000.0) - since
            if remaining > 0:
                time.sleep(remaining)

        # Release the previous modifier
        if self.current_octave_shift == 1:
            release_key(VK_LSHIFT)
        elif self.current_octave_shift == -1:
            release_key(VK_LCONTROL)

        # Press the new modifier
        if target_shift == 1:
            press_key(VK_LSHIFT)
        elif target_shift == -1:
            press_key(VK_LCONTROL)

        self.current_octave_shift = target_shift
        self._last_shift_change = time.perf_counter()

        # Give the game time to register the modifier before the next note
        if self.shift_delay_ms > 0:
            time.sleep(self.shift_delay_ms / 1000.0)

    @synchronized
    def set_sustain(self, active):
        if self.sustain_active != active:
            tap_key(VK_SPACE)
            self.sustain_active = active

    def _await_key_up(self, vk_code):
        """Block until this key has been up for at least retrigger_gap_ms.

        Without this a release and the following press land microseconds
        apart and the game never samples the key as up, so the repeat is
        swallowed and the note just sounds held.
        """
        gap = self.retrigger_gap_ms / 1000.0
        if gap <= 0:
            return
        last_up = self._last_release.get(vk_code)
        if last_up is None:
            return
        remaining = gap - (time.perf_counter() - last_up)
        if remaining > 0:
            time.sleep(min(remaining, gap))

    def _release_vk(self, vk_code):
        release_key(vk_code)
        self._last_release[vk_code] = time.perf_counter()

    @synchronized
    def press_note(self, midi_note):
        target = midi_note + self.key_offset
        target_shift, base_note = self._get_mapping(target)
        if base_note is None:
            return # Out of range

        self.set_octave_shift(target_shift)

        note_name = midi_to_note_name(base_note)
        vk_code = KEY_MAP.get(note_name)
        if not vk_code:
            return

        refs = self.key_refs.get(vk_code, 0)
        if refs > 0:
            # Key is already physically held by another sounding note. Let go
            # first so the game sees a fresh key-down rather than a hold.
            self._release_vk(vk_code)
        self._await_key_up(vk_code)
        press_key(vk_code)
        self.key_refs[vk_code] = refs + 1
        # Record the key we actually pressed, keyed by the note as the caller
        # gave it to us, so release_note() can undo exactly this press even if
        # the octave modifier has moved on by then.
        self.note_keys.setdefault(midi_note, []).append(vk_code)

    @synchronized
    def release_note(self, midi_note):
        # Prefer the key this note was actually pressed with (FIFO, matching
        # how the arranger pairs note_on/note_off) over re-deriving it, which
        # would pick the wrong key whenever the octave zone changed mid-note.
        stack = self.note_keys.get(midi_note)
        if stack:
            vk_code = stack.pop(0)
            if not stack:
                del self.note_keys[midi_note]
        else:
            # No record of the press - e.g. a note_off left over from before a
            # seek or release_all. Fall back to deriving the key.
            _, base_note = self._get_mapping(midi_note + self.key_offset)
            if base_note is None:
                return
            vk_code = KEY_MAP.get(midi_to_note_name(base_note))
        if not vk_code:
            return

        refs = self.key_refs.get(vk_code, 0)
        if refs > 0:
            self.key_refs[vk_code] = refs - 1
            if self.key_refs[vk_code] == 0:
                self._release_vk(vk_code)

    def _get_mapping(self, midi_note):
        # Check if playable in CURRENT shift first to minimize toggling
        if self.current_octave_shift == 0 and 48 <= midi_note <= 83:
            return 0, midi_note
        elif self.current_octave_shift == 1 and 60 <= midi_note <= 95:
            return 1, midi_note - 12
        elif self.current_octave_shift == -1 and 36 <= midi_note <= 71:
            return -1, midi_note + 12
            
        # If not playable currently, map to the default shift
        if 48 <= midi_note <= 83:
            return 0, midi_note
        elif 84 <= midi_note <= 95:
            return 1, midi_note - 12
        elif 36 <= midi_note <= 47:
            return -1, midi_note + 12
        
        return 0, None

    @synchronized
    def release_all(self):
        if self.current_octave_shift == 1:
            release_key(VK_LSHIFT)
        elif self.current_octave_shift == -1:
            release_key(VK_LCONTROL)
        self.current_octave_shift = 0
        
        if self.sustain_active:
            self.set_sustain(False)
            
        for vk_code in KEY_MAP.values():
            release_key(vk_code)

        # Everything is up as of now: make the next press of any of these keys
        # honour the retrigger gap rather than landing on top of its own key-up.
        now = time.perf_counter()
        for vk_code in KEY_MAP.values():
            self._last_release[vk_code] = now

        self.key_refs.clear()
        self.note_keys.clear()
