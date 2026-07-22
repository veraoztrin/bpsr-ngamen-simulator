import customtkinter as ctk
from tkinter import filedialog
import json
import os
import random
import tempfile
from midi_parser import parse_midi_full, get_channels_info, guess_channel_instrument
from arranger import ConversionSettings, convert, convert_drum, ABS_LOW, ABS_HIGH
from config import midi_to_note_name, note_name_to_midi, INSTRUMENTS
from player import MidiPlayer
from network_sync import NetworkManager
from live_midi import LiveMidiListener
try:
    from hotkeys import GlobalHotkeys, VK_F9, VK_F10, VK_F11
except Exception:
    # Non-Windows platform: run without global hotkeys.
    GlobalHotkeys = None

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# Same folder network_sync.py logs to - one place for this app's local state.
PREFS_PATH = os.path.join(os.path.expanduser("~"), ".bpsr_midi_player", "prefs.json")

class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Blue Protocol MIDI Bard Player - Multiplayer")
        self.geometry("720x900")
        self.player = MidiPlayer()
        self.live_midi = LiveMidiListener(self.player.simulator)
        self.network = NetworkManager(
            on_state_change=self.on_network_state,
            on_play_cmd=self.on_network_play,
            on_stop_cmd=self.on_network_stop,
            on_midi_received=self.on_network_midi,
            on_sync_update=self.on_network_sync,
            on_disband=self.on_network_disband,
            on_connection_status=self.on_network_connection_status,
            on_sync_stalled=self.on_network_sync_stalled,
            on_kicked=self.on_network_kicked
        )
        
        self.events = []
        self.raw_events = []          # untouched parse result (conversion source)
        self.orig_bpm = 120.0
        self.channel_programs = {}   # channel -> GM program number, from the raw MIDI
        self.beats_per_measure = 4
        self.channels = []
        self.host_checkbox_vars = {}
        self.my_ready_status = False
        
        self.playlist = [] # list of dicts: {"name": str, "path": str}
        self.current_song_idx = -1
        self.was_playing = False

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # App credit line
        self.credit_label = ctk.CTkLabel(
            self, text="BPSR Midi Player - optimised by Carmen",
            font=ctk.CTkFont(size=12), text_color="gray")
        self.credit_label.grid(row=0, column=0, pady=(8, 0))

        # Global File Header (Always visible)
        self.global_file_frame = ctk.CTkFrame(self)
        self.global_file_frame.grid(row=1, column=0, padx=20, pady=(5, 0), sticky="ew")
        
        self.led_label = ctk.CTkLabel(self.global_file_frame, text="🔴 Stopped", font=ctk.CTkFont(weight="bold"), width=90)
        self.led_label.pack(side="left", padx=10, pady=10)
        
        self.prev_btn = ctk.CTkButton(self.global_file_frame, text="⏮", width=30, command=self.prev_song)
        self.prev_btn.pack(side="left", padx=2, pady=10)
        
        self.song_var = ctk.StringVar(value="No file selected")
        self.song_menu = ctk.CTkOptionMenu(self.global_file_frame, values=["No file selected"], variable=self.song_var, command=self.on_song_select, dynamic_resizing=False)
        self.song_menu.pack(side="left", padx=10, pady=10, fill="x", expand=True)
        
        self.next_btn = ctk.CTkButton(self.global_file_frame, text="⏭", width=30, command=self.next_song)
        self.next_btn.pack(side="left", padx=2, pady=10)
        
        self.load_btn = ctk.CTkButton(self.global_file_frame, text="Load MIDI(s)", command=self.load_files)
        self.load_btn.pack(side="right", padx=10, pady=10)

        self.remove_song_btn = ctk.CTkButton(
            self.global_file_frame, text="🗑", width=30,
            fg_color="gray30", hover_color="darkred",
            command=self.remove_current_song)
        self.remove_song_btn.pack(side="right", padx=(0, 4), pady=10)
        
        # Timeline / Progress
        self.progress_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.progress_frame.grid(row=2, column=0, padx=20, pady=5, sticky="ew")
        
        self.time_label = ctk.CTkLabel(self.progress_frame, text="00:00 / 00:00", width=80)
        self.time_label.pack(side="left", padx=5)
        
        self.progress_bar = ctk.CTkProgressBar(self.progress_frame)
        self.progress_bar.pack(side="left", fill="x", expand=True, padx=5)
        self.progress_bar.set(0)
        # Click-to-seek: jump to wherever on the bar was clicked (feedback:
        # "click and it plays from there", like a YouTube timeline).
        self.progress_bar.bind("<Button-1>", self.on_progress_click)

        # Tabs
        self.tabview = ctk.CTkTabview(self)
        self.tabview.grid(row=3, column=0, padx=20, pady=10, sticky="nsew")

        self.tab_solo = self.tabview.add("Solo Play")
        self.tab_multi = self.tabview.add("Multiplayer Lobby")

        self.setup_solo_tab()
        self.setup_multi_tab()

        # Restore the conversion panel from the last session (feedback: it
        # used to silently reset to defaults every launch).
        self.load_prefs()

        # Global hotkeys: work even while the game window has focus.
        # Callbacks fire on the listener thread -> marshal onto the Tk loop.
        self.hotkeys = None
        if GlobalHotkeys:
            self.hotkeys = GlobalHotkeys({
                VK_F9:  lambda: self.after(0, self.hotkey_play),    # start / resume
                VK_F10: lambda: self.after(0, self.player.pause),   # pause
                VK_F11: lambda: self.after(0, self.player.stop),    # stop entirely
            })
            self.hotkeys.start()

        self.update_led_loop()

    def hotkey_play(self):
        # F9 starts playback, or resumes it when paused.
        if not self.player.is_playing:
            self.play_solo()

    def setup_solo_tab(self):
        self.tab_solo.grid_columnconfigure(0, weight=1)
        self.tab_solo.grid_rowconfigure(3, weight=1)

        # Live MIDI Keyboard
        self.live_midi_frame = ctk.CTkFrame(self.tab_solo)
        self.live_midi_frame.grid(row=0, column=0, padx=10, pady=(10, 0), sticky="ew")
        self.live_midi_frame.grid_columnconfigure(1, weight=1)
        
        ctk.CTkLabel(self.live_midi_frame, text="Live MIDI Keyboard:").grid(row=0, column=0, padx=10, pady=10)
        
        devices = ["None"] + self.live_midi.get_devices()
        self.device_var = ctk.StringVar(value="None")
        self.device_menu = ctk.CTkOptionMenu(self.live_midi_frame, values=devices, variable=self.device_var, command=self.on_midi_device_select)
        self.device_menu.grid(row=0, column=1, padx=10, pady=10, sticky="ew")

        # Play Controls
        self.control_frame = ctk.CTkFrame(self.tab_solo)
        self.control_frame.grid(row=1, column=0, padx=10, pady=10, sticky="ew")
        self.control_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)
        
        self.play_btn = ctk.CTkButton(self.control_frame, text="Play Solo (F9)", command=self.play_solo, fg_color="green", hover_color="darkgreen")
        self.play_btn.grid(row=0, column=0, padx=10, pady=10)
        self.pause_btn = ctk.CTkButton(self.control_frame, text="Pause (F10)", command=self.player.pause, fg_color="orange", hover_color="darkorange")
        self.pause_btn.grid(row=0, column=1, padx=10, pady=10)
        self.stop_btn = ctk.CTkButton(self.control_frame, text="Stop (F11)", command=self.player.stop, fg_color="red", hover_color="darkred")
        self.stop_btn.grid(row=0, column=2, padx=10, pady=10)
        
        # Transpose
        self.transpose_frame = ctk.CTkFrame(self.control_frame, fg_color="transparent")
        self.transpose_frame.grid(row=0, column=3, padx=10, pady=10)
        self.transpose_label = ctk.CTkLabel(self.transpose_frame, text="Transpose: 0")
        self.transpose_label.pack()
        self.transpose_slider = ctk.CTkSlider(self.transpose_frame, from_=-12, to=12, number_of_steps=24, command=self.on_transpose)
        self.transpose_slider.set(0)
        self.transpose_slider.pack(pady=5)

        # Autoplay toggle (default OFF): when off, playback stops and releases
        # all keys at the end of the current track instead of advancing to the
        # next loaded MIDI.
        self.autoplay_var = ctk.BooleanVar(value=False)
        self.autoplay_cb = ctk.CTkCheckBox(self.control_frame, text="Autoplay next track",
                                           variable=self.autoplay_var)
        self.autoplay_cb.grid(row=1, column=0, columnspan=3, padx=10, pady=(0, 10), sticky="w")

        # --- Conversion Settings Panel ---
        self.conv_frame = ctk.CTkFrame(self.tab_solo)
        self.conv_frame.grid(row=2, column=0, padx=10, pady=(0, 5), sticky="ew")

        header = ctk.CTkFrame(self.conv_frame, fg_color="transparent")
        header.pack(fill="x", padx=10, pady=(8, 0))
        ctk.CTkLabel(header, text="Conversion Settings",
                     font=ctk.CTkFont(weight="bold")).pack(side="left")
        self.song_info_label = ctk.CTkLabel(header, text="", text_color="gray")
        self.song_info_label.pack(side="left", padx=10)
        self.reconvert_btn = ctk.CTkButton(header, text="↻ Re-convert", width=110,
                                           command=self.reconvert)
        self.reconvert_btn.pack(side="right")

        # Instrument selector (sets the range MIDIs are fitted into).
        row_inst = ctk.CTkFrame(self.conv_frame, fg_color="transparent")
        row_inst.pack(fill="x", padx=10, pady=(8, 0))
        ctk.CTkLabel(row_inst, text="Instrument:").pack(side="left")
        self.instrument_var = ctk.StringVar(value="Piano")
        self.instrument_menu = ctk.CTkOptionMenu(
            row_inst, values=list(INSTRUMENTS.keys()), variable=self.instrument_var,
            width=120, command=self.on_instrument_change)
        self.instrument_menu.pack(side="left", padx=8)
        _pia = INSTRUMENTS["Piano"]
        self.instrument_hint = ctk.CTkLabel(
            row_inst, text=f"fits notes into {midi_to_note_name(_pia['low'])}–"
                           f"{midi_to_note_name(_pia['high'])}", text_color="gray")
        self.instrument_hint.pack(side="left", padx=6)

        # Row 1: numeric settings
        row1 = ctk.CTkFrame(self.conv_frame, fg_color="transparent")
        row1.pack(fill="x", padx=10, pady=(8, 0))

        ctk.CTkLabel(row1, text="BPM (override):").pack(side="left")
        self.bpm_entry = ctk.CTkEntry(row1, width=55, placeholder_text="—")
        self.bpm_entry.pack(side="left", padx=(4, 12))

        ctk.CTkLabel(row1, text="Speed:").pack(side="left")
        self.speed_entry = ctk.CTkEntry(row1, width=45)
        self.speed_entry.insert(0, "1.0")
        self.speed_entry.pack(side="left", padx=(4, 12))

        # Wrapped in its own subframe so it can be hidden in Drum mode (a
        # generated beat has no "chords" to limit) without disturbing BPM/Speed.
        self.maxchord_frame = ctk.CTkFrame(row1, fg_color="transparent")
        self.maxchord_frame.pack(side="left")
        ctk.CTkLabel(self.maxchord_frame, text="Max chord notes:").pack(side="left")
        self.max_chord_seg = ctk.CTkSegmentedButton(
            self.maxchord_frame, values=["1", "2", "3", "4", "5"],
            command=lambda _: self.reconvert())
        self.max_chord_seg.set("5")
        self.max_chord_seg.pack(side="left", padx=(4, 0))

        # Row 2: feature checkboxes
        self.conv_vars = {}
        checks = [
            ("note_thinning", "Note thinning"),
            ("cull_low_priority", "Cull low priority"),
            ("prioritize_melody", "Prioritize melody"),
            ("proportional_remap", "Proportional remap"),
            ("consistent_windows", "Consistent windows"),
            ("voice_aware", "Voice-aware placement"),
            ("phrase_gap_shifting", "Phrase gap shifting"),
            ("melody_lock", "Melody priority (octaves)"),
            ("disable_sustain", "Disable sustain pedal"),
            ("duet_mode", "Duet mode"),
        ]
        # 3 rows instead of 2 - 5-per-row was clipping the last label or two
        # against the window edge.
        self.checkbox_row_frames = []
        for row_checks in (checks[:4], checks[4:7], checks[7:]):
            row = ctk.CTkFrame(self.conv_frame, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=(8, 0))
            self.checkbox_row_frames.append(row)
            for key, label in row_checks:
                var = ctk.BooleanVar(value=False)
                self.conv_vars[key] = var
                ctk.CTkCheckBox(row, text=label, variable=var,
                                command=self.reconvert).pack(side="left", padx=(0, 14))

        # Row 3: range + timing
        self.row3 = row3 = ctk.CTkFrame(self.conv_frame, fg_color="transparent")
        row3.pack(fill="x", padx=10, pady=8)

        ctk.CTkLabel(row3, text="Range:").pack(side="left")
        self.range_low_entry = ctk.CTkEntry(row3, width=45)
        self.range_low_entry.insert(0, midi_to_note_name(ABS_LOW))
        self.range_low_entry.pack(side="left", padx=(4, 2))
        ctk.CTkLabel(row3, text="–").pack(side="left")
        self.range_high_entry = ctk.CTkEntry(row3, width=45)
        self.range_high_entry.insert(0, midi_to_note_name(ABS_HIGH))
        self.range_high_entry.pack(side="left", padx=(2, 12))

        ctk.CTkLabel(row3, text="Duet split:").pack(side="left")
        self.duet_split_entry = ctk.CTkEntry(row3, width=45)
        self.duet_split_entry.insert(0, "C4")
        self.duet_split_entry.pack(side="left", padx=(4, 12))

        ctk.CTkLabel(row3, text="Shift delay (ms):").pack(side="left")
        self.shift_delay_entry = ctk.CTkEntry(row3, width=45)
        self.shift_delay_entry.insert(0, "30")
        self.shift_delay_entry.pack(side="left", padx=(4, 12))

        ctk.CTkLabel(row3, text="Shift hold (ms):").pack(side="left")
        self.shift_hold_entry = ctk.CTkEntry(row3, width=45)
        self.shift_hold_entry.insert(0, "10")
        self.shift_hold_entry.pack(side="left", padx=(4, 0))

        # Row 4: automatic part categorization
        self.row4 = row4 = ctk.CTkFrame(self.conv_frame, fg_color="transparent")
        row4.pack(fill="x", padx=10, pady=(0, 8))
        self.autosplit_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row4, text="Auto-split parts", variable=self.autosplit_var,
                        command=self.reconvert).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(row4, text="into").pack(side="left")
        self.autosplit_seg = ctk.CTkSegmentedButton(row4, values=["2", "3"],
                                                    command=lambda _: self.reconvert())
        self.autosplit_seg.set("2")
        self.autosplit_seg.pack(side="left", padx=6)
        ctk.CTkLabel(row4, text="channels by role (melody / accomp / bass)",
                     text_color="gray").pack(side="left", padx=4)

        # Rows that only make sense for a pitch-based instrument (chord size,
        # note-shaping checkboxes, range/duet/timing, channel auto-split) -
        # hidden in Drum mode, which just auto-generates a beat instead.
        self._drum_hidden_widgets = [
            (self.maxchord_frame, {"side": "left"}),
        ]
        for row in self.checkbox_row_frames:
            self._drum_hidden_widgets.append((row, {"fill": "x", "padx": 10, "pady": (8, 0)}))
        self._drum_hidden_widgets.append((self.row3, {"fill": "x", "padx": 10, "pady": 8}))
        self._drum_hidden_widgets.append((self.row4, {"fill": "x", "padx": 10, "pady": (0, 8)}))

        self.channel_frame = ctk.CTkScrollableFrame(self.tab_solo, label_text="Solo Active Channels")
        self.channel_frame.grid(row=3, column=0, padx=10, pady=10, sticky="nsew")

    def on_transpose(self, val):
        val = int(val)
        self.transpose_label.configure(text=f"Transpose: {val:+d}")
        self.player.transpose = val

    def _inst(self):
        return INSTRUMENTS.get(self.instrument_var.get(), INSTRUMENTS["Piano"])

    def on_instrument_change(self, choice):
        """Apply an instrument's playable range to the Range fields, then
        re-fit the loaded MIDI into it. Drum is a different beast - it has
        no continuous playable range, so it gets its own hint text and hides
        every conversion control that doesn't apply to a generated beat."""
        rng = INSTRUMENTS.get(choice)
        is_drum = bool(rng and rng.get("is_drum"))

        for widget, pack_kwargs in self._drum_hidden_widgets:
            if is_drum:
                widget.pack_forget()
            else:
                widget.pack(**pack_kwargs)

        if rng:
            if is_drum:
                self.instrument_hint.configure(
                    text="writes a full-kit groove that follows the song's "
                         "sections; a real drum track is kept as-is")
            else:
                lo, hi = midi_to_note_name(rng["low"]), midi_to_note_name(rng["high"])
                self.range_low_entry.delete(0, "end"); self.range_low_entry.insert(0, lo)
                self.range_high_entry.delete(0, "end"); self.range_high_entry.insert(0, hi)
                self.instrument_hint.configure(text=f"fits notes into {lo}–{hi}")
        self.reconvert()

    def on_midi_device_select(self, choice):
        if choice == "None":
            self.live_midi.stop_listening()
        else:
            self.live_midi.start_listening(choice)

    def setup_multi_tab(self):
        self.tab_multi.grid_columnconfigure(0, weight=1)
        self.tab_multi.grid_rowconfigure(2, weight=1)

        # Connection Setup
        self.conn_frame = ctk.CTkFrame(self.tab_multi)
        self.conn_frame.grid(row=0, column=0, padx=10, pady=10, sticky="ew")
        self.conn_frame.grid_columnconfigure((0, 1), weight=1)
        
        self.nick_entry = ctk.CTkEntry(self.conn_frame, placeholder_text="Nickname")
        self.nick_entry.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        self.room_entry = ctk.CTkEntry(self.conn_frame, placeholder_text="Room Code")
        self.room_entry.grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        
        self.host_btn = ctk.CTkButton(self.conn_frame, text="Host Room", command=self.host_room)
        self.host_btn.grid(row=1, column=0, padx=5, pady=5, sticky="ew")
        self.join_btn = ctk.CTkButton(self.conn_frame, text="Join Room", command=self.join_room)
        self.join_btn.grid(row=1, column=1, padx=5, pady=5, sticky="ew")

        # Status + live clock-sync accuracy readout
        self.status_frame = ctk.CTkFrame(self.tab_multi, fg_color="transparent")
        self.status_frame.grid(row=1, column=0, pady=5)
        self.status_label = ctk.CTkLabel(self.status_frame, text="Not Connected", text_color="gray")
        self.status_label.pack(side="left", padx=8)
        self.sync_label = ctk.CTkLabel(self.status_frame, text="", text_color="gray")
        self.sync_label.pack(side="left", padx=8)

        # Lobby List
        self.lobby_frame = ctk.CTkScrollableFrame(self.tab_multi, label_text="Lobby Players (Host assigns channels here)")
        self.lobby_frame.grid(row=2, column=0, padx=10, pady=10, sticky="nsew")

        # Host Controls
        self.host_control_frame = ctk.CTkFrame(self.tab_multi)
        self.host_control_frame.grid(row=3, column=0, padx=10, pady=5, sticky="ew")
        self.host_control_frame.grid_columnconfigure((0, 1), weight=1)
        
        self.sync_play_btn = ctk.CTkButton(self.host_control_frame, text="SYNC PLAY (Waiting for Ready...)", command=self.sync_play, fg_color="purple", hover_color="#5e0082", state="disabled")
        self.sync_play_btn.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        
        self.sync_stop_btn = ctk.CTkButton(self.host_control_frame, text="STOP SINC", command=self.sync_stop, fg_color="red", hover_color="darkred", state="disabled")
        self.sync_stop_btn.grid(row=0, column=1, padx=5, pady=5, sticky="ew")

        self.disband_btn = ctk.CTkButton(self.host_control_frame, text="Disband Lobby",
                                         command=self.disband_room, fg_color="#8a5a00",
                                         hover_color="#6d4700", state="disabled")
        self.disband_btn.grid(row=1, column=0, columnspan=2, padx=5, pady=(0, 5), sticky="ew")

        # Client Controls
        self.client_control_frame = ctk.CTkFrame(self.tab_multi)
        self.client_control_frame.grid(row=4, column=0, padx=10, pady=5, sticky="ew")
        self.client_control_frame.grid_columnconfigure(0, weight=1)
        
        self.ready_btn = ctk.CTkButton(self.client_control_frame, text="I'm Ready!", command=self.toggle_ready, state="disabled")
        self.ready_btn.grid(row=0, column=0, padx=5, pady=5, sticky="ew")

        # Manual sync calibration: cancels the residual start offset that the
        # automatic clock sync can't remove (network path asymmetry + this
        # machine's input latency). Tune by ear until both players line up.
        nudge_row = ctk.CTkFrame(self.client_control_frame, fg_color="transparent")
        nudge_row.grid(row=1, column=0, padx=5, pady=(0, 6), sticky="ew")
        ctk.CTkLabel(nudge_row, text="Sync nudge (ms):").pack(side="left")
        self.nudge_entry = ctk.CTkEntry(nudge_row, width=60)
        self.nudge_entry.insert(0, "0")
        self.nudge_entry.pack(side="left", padx=6)
        ctk.CTkLabel(nudge_row, text="−earlier / +later · tune by ear, set once",
                     text_color="gray").pack(side="left", padx=6)

        self.leave_btn = ctk.CTkButton(self.client_control_frame, text="Leave Room",
                                       command=self.leave_room, fg_color="gray30",
                                       hover_color="gray20", state="disabled")
        self.leave_btn.grid(row=2, column=0, padx=5, pady=(0, 5), sticky="ew")

    # --- Actions ---

    def _refresh_progress_bar(self):
        current = self.player.get_current_time()
        total = self.player.get_total_time()

        if total > 0:
            progress = max(0.0, min(1.0, current / total))
            self.progress_bar.set(progress)

            curr_m = int(current // 60)
            curr_s = int(current % 60)
            tot_m = int(total // 60)
            tot_s = int(total % 60)
            self.time_label.configure(text=f"{curr_m:02d}:{curr_s:02d} / {tot_m:02d}:{tot_s:02d}")
        else:
            self.progress_bar.set(0)
            self.time_label.configure(text="00:00 / 00:00")

    def on_progress_click(self, event):
        """Click-to-seek: jump playback to wherever on the bar was clicked."""
        if not self.events:
            return
        total = self.player.get_total_time()
        if total <= 0:
            return
        width = self.progress_bar.winfo_width()
        if width <= 1:
            return
        frac = max(0.0, min(1.0, event.x / width))
        self.player.seek(frac * total)
        self._refresh_progress_bar()  # instant feedback, don't wait for the next 200ms tick

    def update_led_loop(self):
        if self.player.is_syncing:
            self.led_label.configure(text="🟡 Syncing...", text_color="yellow")
        elif self.player.is_playing:
            self.led_label.configure(text="🟢 Playing", text_color="green")
        else:
            self.led_label.configure(text="🔴 Stopped", text_color="gray")

        self._refresh_progress_bar()

        # Auto-advance song if finished naturally (only when Autoplay is on).
        # On natural finish the player already released all keys; with autoplay
        # off we simply stop here instead of loading the next track.
        is_playing_now = self.player.is_playing
        if self.was_playing and not is_playing_now and not self.player.stop_requested:
            if self.autoplay_var.get():
                self.next_song(autoplay=True)

        self.was_playing = is_playing_now
            
        self.after(200, self.update_led_loop)

    def load_files(self):
        file_paths = filedialog.askopenfilenames(filetypes=[("MIDI Files", "*.mid *.midi")])
        if file_paths:
            last_new_idx = None
            for p in file_paths:
                name = os.path.basename(p)
                if name not in [s["name"] for s in self.playlist]:
                    self.playlist.append({"name": name, "path": p})
                    last_new_idx = len(self.playlist) - 1

            self._update_playlist_ui()

            # Jump to whatever was just loaded instead of leaving it sitting
            # unselected in the dropdown while a different song stays active
            # (feedback: loading a MIDI should make it the selected one).
            if last_new_idx is not None:
                self.current_song_idx = last_new_idx
                self._load_current_song()

    def remove_current_song(self):
        """Drop the currently selected song from the playlist (feedback:
        no way to clear out songs once loaded)."""
        if not (0 <= self.current_song_idx < len(self.playlist)):
            return
        self.player.stop()
        del self.playlist[self.current_song_idx]

        if not self.playlist:
            self.current_song_idx = -1
            self.raw_events = []
            self.events = []
            self.channels = []
            self.channel_programs = {}
            self.song_info_label.configure(text="")
        else:
            self.current_song_idx = min(self.current_song_idx, len(self.playlist) - 1)

        self._update_playlist_ui()
        if self.playlist:
            self._load_current_song()

        if self.network.room_code:
            self._update_lobby_ui(self.network.room_state)

    def _update_playlist_ui(self):
        if not self.playlist:
            self.song_menu.configure(values=["No file selected"])
            self.song_var.set("No file selected")
        else:
            names = [s["name"] for s in self.playlist]
            self.song_menu.configure(values=names)

    def on_song_select(self, choice):
        for idx, s in enumerate(self.playlist):
            if s["name"] == choice:
                self.current_song_idx = idx
                self._load_current_song()
                break

    def prev_song(self):
        if not self.playlist: return
        self.current_song_idx = (self.current_song_idx - 1) % len(self.playlist)
        self._load_current_song(autoplay=self.player.is_playing)

    def next_song(self, autoplay=False):
        if not self.playlist: return
        was_playing = self.player.is_playing or autoplay
        self.current_song_idx = (self.current_song_idx + 1) % len(self.playlist)
        self._load_current_song(autoplay=was_playing)

    def _load_current_song(self, autoplay=False):
        if 0 <= self.current_song_idx < len(self.playlist):
            song = self.playlist[self.current_song_idx]
            self.song_var.set(song["name"])
            self.player.stop()
            self._parse_and_load(song["path"])
            
            if autoplay:
                self.play_solo()
            
            if self.network.room_code and self.network.is_host:
                self.network.share_midi(song["path"], song["name"])
            if self.network.room_code:
                self._update_lobby_ui(self.network.room_state)

    def _parse_and_load(self, file_path):
        parsed = parse_midi_full(file_path)
        self.raw_events = parsed['events']
        self.orig_bpm = parsed['bpm']
        self.beats_per_measure = parsed['beats_per_measure']
        self.channel_programs = parsed.get('channel_programs', {})
        self.song_info_label.configure(
            text=f"{self.orig_bpm:.0f} BPM · {self.beats_per_measure}/4")
        self.reconvert()

    def _get_float(self, entry, default):
        try:
            return float(entry.get().replace(",", "."))
        except (ValueError, AttributeError):
            return default

    def _get_note(self, entry, default):
        val = note_name_to_midi(entry.get())
        return val if val is not None else default

    def save_prefs(self):
        """Persist the conversion panel so it survives closing the app
        instead of silently resetting to defaults every launch."""
        try:
            prefs = {
                "checks": {k: v.get() for k, v in self.conv_vars.items()},
                "bpm": self.bpm_entry.get(),
                "speed": self.speed_entry.get(),
                "max_chord_notes": self.max_chord_seg.get(),
                "range_low": self.range_low_entry.get(),
                "range_high": self.range_high_entry.get(),
                "duet_split": self.duet_split_entry.get(),
                "shift_delay": self.shift_delay_entry.get(),
                "shift_hold": self.shift_hold_entry.get(),
                "autosplit": self.autosplit_var.get(),
                "autosplit_parts": self.autosplit_seg.get(),
                "instrument": self.instrument_var.get(),
                "autoplay": self.autoplay_var.get(),
            }
            os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
            with open(PREFS_PATH, "w", encoding="utf-8") as f:
                json.dump(prefs, f)
        except Exception:
            pass  # Persistence is a nicety - never worth crashing the app over.

    def load_prefs(self):
        """Load the last saved conversion panel, if any. Safe to call with no
        song loaded yet - it only touches the settings widgets."""
        try:
            with open(PREFS_PATH, "r", encoding="utf-8") as f:
                prefs = json.load(f)
        except Exception:
            return

        for key, val in prefs.get("checks", {}).items():
            if key in self.conv_vars:
                self.conv_vars[key].set(bool(val))

        def _set_entry(entry, val):
            if val is None:
                return
            entry.delete(0, "end")
            entry.insert(0, str(val))

        # Instrument first - on_instrument_change overwrites the range fields
        # with that instrument's default, so it must run before we reapply
        # any custom range the user had dialed in on top of it.
        instrument = prefs.get("instrument")
        if instrument in INSTRUMENTS:
            self.instrument_var.set(instrument)
            self.on_instrument_change(instrument)

        _set_entry(self.bpm_entry, prefs.get("bpm"))
        _set_entry(self.speed_entry, prefs.get("speed"))
        _set_entry(self.range_low_entry, prefs.get("range_low"))
        _set_entry(self.range_high_entry, prefs.get("range_high"))
        _set_entry(self.duet_split_entry, prefs.get("duet_split"))
        _set_entry(self.shift_delay_entry, prefs.get("shift_delay"))
        _set_entry(self.shift_hold_entry, prefs.get("shift_hold"))

        if prefs.get("max_chord_notes"):
            self.max_chord_seg.set(prefs["max_chord_notes"])
        if prefs.get("autosplit_parts"):
            self.autosplit_seg.set(prefs["autosplit_parts"])
        self.autosplit_var.set(bool(prefs.get("autosplit", False)))
        self.autoplay_var.set(bool(prefs.get("autoplay", False)))

    def build_settings(self):
        """Collect the conversion panel state into a ConversionSettings."""
        bpm_text = self.bpm_entry.get().strip()
        bpm_override = None
        if bpm_text:
            try:
                bpm_override = float(bpm_text.replace(",", "."))
            except ValueError:
                pass

        s = ConversionSettings(
            bpm_override=bpm_override,
            speed=max(0.1, self._get_float(self.speed_entry, 1.0)),
            max_chord_notes=int(self.max_chord_seg.get()),
            note_thinning=self.conv_vars["note_thinning"].get(),
            cull_low_priority=self.conv_vars["cull_low_priority"].get(),
            prioritize_melody=self.conv_vars["prioritize_melody"].get(),
            proportional_remap=self.conv_vars["proportional_remap"].get(),
            consistent_windows=self.conv_vars["consistent_windows"].get(),
            voice_aware=self.conv_vars["voice_aware"].get(),
            phrase_gap_shifting=self.conv_vars["phrase_gap_shifting"].get(),
            melody_lock=self.conv_vars["melody_lock"].get(),
            melody_lock_mode='drop',
            duet_mode=self.conv_vars["duet_mode"].get(),
            duet_split_note=self._get_note(self.duet_split_entry, 60),
            auto_split=self.autosplit_var.get(),
            auto_split_parts=int(self.autosplit_seg.get()),
            disable_sustain=self.conv_vars["disable_sustain"].get(),
            reach_low=self._inst()["low"],
            reach_high=self._inst()["high"],
            instrument_offset=self._inst()["offset"],
            range_low=self._get_note(self.range_low_entry, ABS_LOW),
            range_high=self._get_note(self.range_high_entry, ABS_HIGH),
        )
        self.save_prefs()
        return s

    def reconvert(self):
        """Re-run the conversion pipeline on the raw MIDI with current settings."""
        if not self.raw_events:
            return
        settings = self.build_settings()
        is_drum = self._inst().get("is_drum", False)
        if is_drum:
            self.events = convert_drum(self.raw_events, settings, orig_bpm=self.orig_bpm,
                                       beats_per_measure=self.beats_per_measure)
        else:
            self.events = convert(self.raw_events, settings, orig_bpm=self.orig_bpm)
        self.channels = get_channels_info(self.events)

        # Apply input timing knobs + the instrument's key offset
        if self.player.simulator:
            self.player.simulator.shift_delay_ms = self._get_float(self.shift_delay_entry, 30)
            self.player.simulator.shift_hold_ms = self._get_float(self.shift_hold_entry, 10)
            self.player.simulator.key_offset = self._inst()["offset"]

        # Drum output is always a single channel - ignore any leftover
        # duet/auto-split state from before the instrument was switched, so
        # the Solo panel doesn't show stale "Duet Low/High" style labels.
        self.build_solo_channel_ui(duet=(settings.duet_mode and not is_drum),
                                   auto=(settings.auto_split and not is_drum),
                                   parts=settings.auto_split_parts)
        self.player.load_events(self.events, self.channels)

        if self.network.room_code:
            self._update_lobby_ui(self.network.room_state)

    def _channel_ranges(self):
        """Lowest/highest MIDI note per channel in the converted events."""
        ranges = {}
        for ev in self.events:
            if ev.get('type') == 'note_on' and 'channel' in ev:
                lo, hi = ranges.get(ev['channel'], (999, -1))
                ranges[ev['channel']] = (min(lo, ev['note']), max(hi, ev['note']))
        return ranges

    def build_solo_channel_ui(self, duet=False, auto=False, parts=2):
        for widget in self.channel_frame.winfo_children():
            widget.destroy()
        self.channel_vars = []
        ranges = self._channel_ranges()

        def name_for(ch):
            if auto:
                if parts >= 3:
                    return {0: "Melody", 1: "Harmony", 2: "Bass"}.get(ch, f"Part {ch}")
                return {0: "Melody", 1: "Accompaniment"}.get(ch, f"Part {ch}")
            if duet:
                return {0: "Duet Low (bass)", 1: "Duet High (melody)"}.get(ch, f"Channel {ch}")
            # Plain (no auto-split/duet) mode: keep the raw channel number,
            # but tack on an educated instrument guess when we have one -
            # from the channel's GM Program Change, or "Drums" for the
            # reserved GM percussion channel. Channels with neither just
            # stay "Channel N" as before rather than showing a guess we
            # aren't confident in.
            guess = guess_channel_instrument(ch, self.channel_programs)
            if guess:
                return f"Channel {ch} - {guess}"
            return f"Channel {ch}"

        for ch in self.channels:
            row = ctk.CTkFrame(self.channel_frame, fg_color="transparent")
            row.pack(fill="x", pady=3, padx=6)
            if ch in ranges:
                lo, hi = ranges[ch]
                rng = f"{midi_to_note_name(lo)}–{midi_to_note_name(hi)}"
            else:
                rng = "—"
            ctk.CTkLabel(row, text=rng, width=95, anchor="w",
                         text_color="gray").pack(side="left")
            var = ctk.BooleanVar(value=True)
            cb = ctk.CTkCheckBox(row, text=name_for(ch), variable=var,
                                 command=self.update_solo_channels)
            cb.pack(side="left", padx=6)
            self.channel_vars.append((ch, var))

    def update_solo_channels(self):
        active = [ch for ch, var in self.channel_vars if var.get()]
        self.player.set_active_channels(active)

    def play_solo(self):
        self.update_solo_channels()
        if self.events:
            self.player.play()

    # --- Networking ---

    def host_room(self):
        nick = self.nick_entry.get() or "Host"
        # Random per-session code when left blank - a fixed "1234" fallback
        # meant two unrelated hosts who both leave the field empty would land
        # on the *exact same* public MQTT topic (broker.hivemq.com is shared
        # by anyone) and see/hear each other's rooms.
        room = self.room_entry.get() or f"{random.randint(1000, 9999)}"
        try:
            self.network.connect()
        except Exception as e:
            self.status_label.configure(text=f"Couldn't connect: {e}", text_color="red")
            return
        self.network.host_room(room, nick)
        self.status_label.configure(text=f"Hosting Room: {room} | Waiting for players...", text_color="green")
        self.sync_label.configure(text="🕐 Clock: host (reference)", text_color="gray")
        self.sync_play_btn.configure(state="normal")
        self.sync_stop_btn.configure(state="normal")
        self.disband_btn.configure(state="normal")
        self.host_btn.configure(state="disabled")
        self.join_btn.configure(state="disabled")

    def join_room(self):
        nick = self.nick_entry.get() or "Player"
        room = self.room_entry.get()
        if not room:
            return
        try:
            self.network.connect()
        except Exception as e:
            self.status_label.configure(text=f"Couldn't connect: {e}", text_color="red")
            return
        self.network.join_room(room, nick)
        self.status_label.configure(text=f"Joined Room: {room}", text_color="green")
        self.sync_label.configure(text="🕐 Syncing clock…", text_color="orange")
        self.host_btn.configure(state="disabled")
        self.join_btn.configure(state="disabled")
        self.leave_btn.configure(state="normal")
        # Ready stays locked until the clock is synced with the host, so nobody
        # can start a song before the timing is aligned.
        self.ready_btn.configure(state="disabled", text="Syncing clock…")
        self.my_ready_status = False

    def leave_room(self):
        self.network.leave_room()
        self.player.stop()
        self._reset_multiplayer_ui("You left the room.")

    def disband_room(self):
        self.network.disband_room()
        self.player.stop()
        self._reset_multiplayer_ui("Lobby disbanded.")

    def on_network_disband(self):
        # Host closed the room; runs on the network thread -> marshal to UI.
        self.after(0, self._on_host_disbanded)

    def _on_host_disbanded(self):
        self.player.stop()
        self._reset_multiplayer_ui("Host closed the room.")

    def kick_player(self, client_id):
        if self.network.is_host:
            self.network.kick_player(client_id)

    def on_network_kicked(self):
        # Runs on the network thread -> marshal to UI.
        self.after(0, self._on_kicked)

    def _on_kicked(self):
        self.player.stop()
        self._reset_multiplayer_ui("You were removed from the room by the host.")

    def _reset_multiplayer_ui(self, status="Not Connected"):
        self.status_label.configure(text=status, text_color="gray")
        self.sync_label.configure(text="")
        self.host_btn.configure(state="normal")
        self.join_btn.configure(state="normal")
        self.ready_btn.configure(state="disabled", text="I'm Ready!",
                                 fg_color=["#3a7ebf", "#1f538d"],
                                 hover_color=["#325882", "#14375e"])
        self.sync_play_btn.configure(state="disabled",
                                     text="SYNC PLAY (Waiting for Ready...)")
        self.sync_stop_btn.configure(state="disabled")
        self.disband_btn.configure(state="disabled")
        self.leave_btn.configure(state="disabled")
        self.my_ready_status = False
        for widget in self.lobby_frame.winfo_children():
            widget.destroy()

    def toggle_ready(self):
        self.my_ready_status = not self.my_ready_status
        if self.my_ready_status:
            self.ready_btn.configure(text="✅ Ready!", fg_color="green", hover_color="darkgreen")
        else:
            self.ready_btn.configure(text="I'm Ready!", fg_color=["#3a7ebf", "#1f538d"], hover_color=["#325882", "#14375e"])
        self.network.send_ready_status(self.my_ready_status)

    def sync_play(self):
        if self.events and self.network.is_host:
            self.player.stop()
            self.network.send_play(delay_seconds=4.0)
            
    def sync_stop(self):
        if self.network.is_host:
            self.network.send_stop()

    # --- Callbacks from NetworkManager (Run in background thread, schedule UI updates) ---

    def on_network_state(self, state):
        self.after(0, self._update_lobby_ui, state)

    def on_network_play(self, global_start_time, my_channels):
        self.after(0, self._trigger_play, global_start_time, my_channels)

    def on_network_stop(self):
        self.after(0, self.player.stop)

    def on_network_sync(self, rtt, offset):
        self.after(0, self._update_sync_label, rtt, offset)

    def _update_sync_label(self, rtt, offset):
        # Timing uncertainty is roughly half the round-trip delay.
        acc_ms = (rtt * 1000.0) / 2.0
        color = "green" if acc_ms < 30 else ("orange" if acc_ms < 80 else "red")
        self.sync_label.configure(text=f"🕐 Synced ±{acc_ms:.0f} ms", text_color=color)
        # Unlock Ready once we have a clock lock (only before the user readies up).
        if (not self.network.is_host and not self.my_ready_status
                and self.ready_btn.cget("state") == "disabled"):
            self.ready_btn.configure(state="normal", text="I'm Ready!")

    def on_network_sync_stalled(self):
        # Fired once from the background sync thread if ~8s pass in a room
        # with zero replies from the host. Previously "Syncing clock..." had
        # no timeout at all and would just sit there forever with no
        # explanation, even though Ready was hard-locked until it resolved.
        self.after(0, self._show_sync_stalled)

    def _show_sync_stalled(self):
        if self.network.is_host or not self.network.room_code or self.network.is_synced:
            return  # already resolved, or state changed since the watchdog fired
        self.sync_label.configure(
            text="🕐 Can't reach host clock (check firewall/port 1883)",
            text_color="red")

    def on_network_connection_status(self, status, detail):
        self.after(0, self._update_connection_status, status, detail)

    def _update_connection_status(self, status, detail):
        if status == "connected":
            # A fresh connect, or a recovered one after a drop. If we were
            # mid-sync when the link dropped, our samples are gone; let the
            # label reflect that we're re-measuring rather than silently
            # keeping a stale "Syncing clock..." with no further updates.
            if not self.network.is_host and self.network.room_code and not self.network.is_synced:
                self.sync_label.configure(text="🕐 Syncing clock…", text_color="orange")
        elif status == "reconnecting":
            if self.network.room_code:
                self.status_label.configure(text="Connection lost - reconnecting…", text_color="orange")
        elif status == "disconnected":
            if self.network.room_code:
                self.status_label.configure(
                    text=f"Disconnected from server ({detail or 'connection lost'}).",
                    text_color="red")

    def on_network_midi(self, filename, data):
        self.after(0, self._save_and_load_midi, filename, data)

    def _update_lobby_ui(self, state):
        for widget in self.lobby_frame.winfo_children():
            widget.destroy()
            
        fn = state.get("filename")
        if fn:
            ctk.CTkLabel(self.lobby_frame, text=f"🎵 Shared Song: {fn}", font=ctk.CTkFont(weight="bold")).pack(pady=5)

        self.host_checkbox_vars = {}

        for p in state["players"]:
            frame = ctk.CTkFrame(self.lobby_frame)
            frame.pack(fill="x", pady=5, padx=5)
            
            is_me = p['client_id'] == self.network.client_id
            name = f"{p['nickname']} (Me)" if is_me else p['nickname']
            
            # Status dot
            status_color = "green" if p.get("connected", True) else "red"
            status_text = "🟢" if p.get("connected", True) else "🔴"
            
            status_lbl = ctk.CTkLabel(frame, text=status_text, width=20)
            status_lbl.pack(side="left", padx=(10, 0), pady=10)
            
            ready_text = "✅" if p.get("ready", False) else "⏳"
            ready_lbl = ctk.CTkLabel(frame, text=ready_text, width=20)
            ready_lbl.pack(side="left", padx=(5, 0), pady=10)
            
            lbl = ctk.CTkLabel(frame, text=name, width=120, anchor="w", font=ctk.CTkFont(weight="bold"))
            lbl.pack(side="left", padx=(5, 10), pady=10)

            if self.network.is_host and not is_me:
                # Pack this before ch_frame so it reserves its space on the
                # right first; ch_frame's expand=True then fills what's left.
                kick_btn = ctk.CTkButton(
                    frame, text="Kick", width=50, fg_color="darkred", hover_color="red",
                    command=lambda cid=p['client_id']: self.kick_player(cid))
                kick_btn.pack(side="right", padx=(5, 10), pady=10)

            if self.network.is_host:
                ch_frame = ctk.CTkScrollableFrame(frame, height=40, fg_color="transparent", orientation="horizontal")
                ch_frame.pack(side="left", fill="x", expand=True, padx=5)
                
                if not self.channels:
                    ctk.CTkLabel(ch_frame, text="Load a MIDI file first to assign channels.", text_color="gray").pack(side="left")
                else:
                    self.host_checkbox_vars[p['client_id']] = {}
                    for ch in self.channels:
                        var = ctk.BooleanVar(value=(ch in p["channels"]))
                        self.host_checkbox_vars[p['client_id']][ch] = var
                        
                        def on_toggle(cid=p['client_id']):
                            chs = [c for c, v in self.host_checkbox_vars[cid].items() if v.get()]
                            self.network.assign_channels(cid, chs)
                            
                        cb = ctk.CTkCheckBox(ch_frame, text=f"Ch {ch}", variable=var, command=on_toggle)
                        cb.pack(side="left", padx=10, pady=5)
            else:
                assigned_text = ", ".join(map(str, p['channels'])) if p['channels'] else "None"
                lbl2 = ctk.CTkLabel(frame, text=f"Assigned Channels: {assigned_text}", text_color="cyan")
                lbl2.pack(side="left", padx=10, pady=10)
                
        if self.network.is_host:
            all_ready = all(p.get("ready", False) for p in state["players"])
            if all_ready and self.events:
                self.sync_play_btn.configure(state="normal", text="SYNC PLAY (All Ready!)")
            else:
                self.sync_play_btn.configure(state="disabled", text="SYNC PLAY (Waiting for Ready...)")

    def _trigger_play(self, global_start_time, my_channels):
        # Do playback cleanup FIRST. stop() may join a thread and release keys,
        # and that duration varies per machine — computing the start delay
        # afterwards keeps that variable latency out of the start moment.
        self.player.stop()
        self.player.set_active_channels(my_channels)

        delay = global_start_time - self.network.get_global_time()
        # Manual calibration nudge (ms): +later / -earlier.
        nudge = self._get_float(self.nudge_entry, 0.0) / 1000.0
        delay += nudge
        if delay < 0:
            delay = 0.0  # start immediately if the target moment already passed
        print(f"Network Play Triggered! Delaying start by {delay:.3f}s "
              f"(nudge {nudge*1000:.0f}ms) for Channels {my_channels}")
        self.player.play(delay_seconds=delay)

    def _save_and_load_midi(self, filename, data):
        temp_dir = tempfile.gettempdir()
        file_path = os.path.join(temp_dir, filename)
        with open(file_path, "wb") as f:
            f.write(data)
        
        # In client mode, we just override current song view (or add to playlist)
        # We will clear playlist and set this as the only song for the client
        self.playlist = [{"name": f"{filename} (Received)", "path": file_path}]
        self.current_song_idx = 0
        self._update_playlist_ui()
        self.song_var.set(self.playlist[0]["name"])
        
        self._parse_and_load(file_path)

    def destroy(self):
        self.save_prefs()  # catch any change that never triggered a reconvert
        if self.hotkeys:
            self.hotkeys.stop()
        self.player.stop()
        self.live_midi.stop_listening()
        self.network.disconnect()
        super().destroy()

if __name__ == "__main__":
    app = App()
    app.mainloop()
