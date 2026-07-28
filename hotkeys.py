# hotkeys.py
#
# Global hotkey support via the Win32 RegisterHotKey API.
# Hotkeys fire even while another window (the game) has focus, with no
# extra dependencies - same ctypes approach as input_simulator.
#
# Callbacks run on the listener thread; GUI users should marshal them
# onto the Tk main loop with .after(0, ...).

import ctypes
import threading

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_NOREPEAT = 0x4000

# Common VK codes for convenience
VK_F9 = 0x78
VK_F10 = 0x79
VK_F11 = 0x7A


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p),
                ("message", ctypes.c_uint),
                ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_size_t),
                ("time", ctypes.c_ulong),
                ("pt_x", ctypes.c_long),
                ("pt_y", ctypes.c_long)]


class GlobalHotkeys:
    """Registers system-wide hotkeys and dispatches callbacks.

    bindings: {vk_code: callable}
    """

    def __init__(self, bindings):
        self.bindings = dict(bindings)
        self.thread = None
        self.thread_id = None
        self._started = threading.Event()
        self.registered = False
        self.failed_keys = []

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return self.registered
        self._started.clear()
        self.registered = False
        self.failed_keys = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self._started.wait(timeout=2.0)
        return self.registered

    def _run(self):
        # RegisterHotKey must be called on the same thread that runs
        # the message loop.
        ids = {}
        try:
            self.thread_id = kernel32.GetCurrentThreadId()
            for i, (vk, cb) in enumerate(self.bindings.items(), start=1):
                if user32.RegisterHotKey(None, i, MOD_NOREPEAT, vk):
                    ids[i] = cb
                else:
                    self.failed_keys.append(vk)
                    print(
                        f"Warning: could not register global hotkey VK=0x{vk:02X} "
                        f"(already in use by another app?)")

            # Partial registration is confusing (for example Stop works but
            # Play does not), so expose hotkeys only when the full set succeeds.
            if self.failed_keys:
                return
            self.registered = True
            self._started.set()

            msg = MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    cb = ids.get(msg.wParam)
                    if cb:
                        try:
                            cb()
                        except Exception as e:
                            print(f"Hotkey callback error: {e}")
        finally:
            for hotkey_id in ids:
                user32.UnregisterHotKey(None, hotkey_id)
            self.registered = False
            self.thread_id = None
            self._started.set()

    def stop(self):
        thread = self.thread
        thread_id = self.thread_id
        if thread_id is not None:
            user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        if thread is not None:
            thread.join(timeout=1.0)
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self.thread = None
            self.thread_id = None
            self.registered = False
        return stopped
