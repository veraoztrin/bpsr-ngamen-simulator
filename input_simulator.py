import ctypes
import time
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

class BPSRInputSimulator:
    def __init__(self):
        self.current_octave_shift = 0 # 0: normal, 1: High, -1: Low
        self.sustain_active = False
        self.key_refs = {}
        # Timing knobs (settable from the GUI):
        # shift_delay_ms: wait after toggling Shift/Ctrl before the next note,
        #                 so the game reliably registers the modifier change.
        # shift_hold_ms:  minimum time a modifier state is held before it may
        #                 be toggled again (prevents dropped toggles on fast runs).
        self.shift_delay_ms = 30
        self.shift_hold_ms = 10
        self._last_shift_change = 0.0
        # Semitone offset added before the piano key lookup, so instruments
        # whose keyboard is the piano layout transposed (e.g. Bass = -2 octaves)
        # press the right key. 0 = piano/guitar.
        self.key_offset = 0

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

    def set_sustain(self, active):
        if self.sustain_active != active:
            tap_key(VK_SPACE)
            self.sustain_active = active

    def press_note(self, midi_note):
        midi_note = midi_note + self.key_offset
        target_shift, base_note = self._get_mapping(midi_note)
        if base_note is None:
            return # Out of range
        
        self.set_octave_shift(target_shift)
        
        note_name = midi_to_note_name(base_note)
        vk_code = KEY_MAP.get(note_name)
        if vk_code:
            refs = self.key_refs.get(vk_code, 0)
            if refs == 0:
                press_key(vk_code)
            else:
                # Key is already physically held. Release and press again to trigger the new note.
                release_key(vk_code)
                time.sleep(0.005)
                press_key(vk_code)
            self.key_refs[vk_code] = refs + 1

    def release_note(self, midi_note):
        midi_note = midi_note + self.key_offset
        _, base_note = self._get_mapping(midi_note)
        if base_note is None:
            return
            
        note_name = midi_to_note_name(base_note)
        vk_code = KEY_MAP.get(note_name)
        if vk_code:
            refs = self.key_refs.get(vk_code, 0)
            if refs > 0:
                self.key_refs[vk_code] = refs - 1
                if self.key_refs[vk_code] == 0:
                    release_key(vk_code)

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
            
        self.key_refs.clear()
