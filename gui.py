import json
import math
import os
import secrets
import tempfile
import threading
from copy import deepcopy
from tkinter import filedialog, messagebox

import customtkinter as ctk

from arranger import ABS_HIGH, ABS_LOW, ConversionSettings, convert, convert_drum
from config import INSTRUMENTS, midi_to_note_name, note_name_to_midi
from conversion_profile import (
    load_conversion_profile,
    make_conversion_profile,
    should_use_host_conversion,
)
from live_midi import LiveMidiListener
from midi_parser import get_channels_info, guess_channel_instrument, parse_midi_full
from network_sync import (
    MAX_MIDI_BYTES,
    MAX_PART_START_SECONDS,
    MAX_READY_SYNC_RTT,
    MIN_ROOM_CREDENTIAL_LENGTH,
    MIN_SYNC_SAMPLES,
    ROOM_CREDENTIAL_PREFIX,
    NetworkManager,
)
from player import MidiPlayer
from track_preparation import prepare_track, prepared_track_matches

try:
    from hotkeys import VK_F9, VK_F10, VK_F11, GlobalHotkeys
except Exception:
    # Non-Windows platform: run without global hotkeys.
    GlobalHotkeys = None

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# Same folder network_sync.py logs to - one place for this app's local state.
PREFS_PATH = os.path.join(os.path.expanduser("~"), ".bpsr_midi_player", "prefs.json")
GROUPING_LABELS = {
    "Musical roles": "roles",
    "Original tracks": "tracks",
    "MIDI channels": "channels",
    "Instrument families": "families",
}

class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Blue Protocol MIDI Bard Player - Multiplayer")
        self.geometry("820x940")
        # Conversion controls need this width, but the Solo page now scrolls
        # vertically, so compact/laptop layouts no longer need a tall minimum.
        self.minsize(720, 520)
        self.player = MidiPlayer(on_output_error=self.on_player_output_error)
        self.live_midi = LiveMidiListener(
            self.player.simulator, on_state_release=self._restore_playback_state)
        self.network = NetworkManager(
            on_state_change=self.on_network_state,
            on_play_cmd=self.on_network_play,
            on_stop_cmd=self.on_network_stop,
            on_midi_received=self.on_network_midi,
            on_sync_update=self.on_network_sync,
            on_disband=self.on_network_disband,
            on_connection_status=self.on_network_connection_status,
            on_sync_stalled=self.on_network_sync_stalled,
            on_kicked=self.on_network_kicked,
            on_conversion_profile_received=self.on_network_conversion_profile,
        )
        
        self.events = []
        self.raw_events = []          # untouched parse result (conversion source)
        self.orig_bpm = 120.0
        self.tempo_map = [{'beat': 0.0, 'bpm': 120.0}]
        self.time_signature_map = [
            {'beat': 0.0, 'numerator': 4, 'denominator': 4}]
        self.channel_programs = {}   # channel -> GM program number, from the raw MIDI
        self.beats_per_measure = 4
        self.channels = []
        self.channel_vars = []
        self.host_checkbox_vars = {}
        self.host_start_time_vars = {}
        self.my_ready_status = False
        self._known_room_players = set()
        self._received_temp_files = set()
        self._parse_generation = 0
        self._closing = False
        
        self.playlist = [] # list of dicts: {"name": str, "path": str}
        self.current_song_idx = -1
        self.was_playing = False
        self._focus_was_blocked = False
        self._play_wait_message_active = False
        self._network_conversion_profile = None
        self._current_conversion_profile = None
        self._accepted_network_revision = None
        self._loading_network_revision = None
        self._applying_track_profile = False
        self._preparation_generation = 0
        self._multiplayer_autoplay_pending_revision = None
        self._multiplayer_auto_ready_revision = None
        self._network_track_profiles = {}

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
        if GlobalHotkeys and self.global_hotkeys_var.get():
            self._start_global_hotkeys()

        self.update_led_loop()

    def _start_global_hotkeys(self):
        if GlobalHotkeys and self.hotkeys is None:
            candidate = GlobalHotkeys({
                VK_F9:  lambda: self.after(0, self.hotkey_play),    # start / resume
                VK_F10: lambda: self.after(0, self.hotkey_pause),
                VK_F11: lambda: self.after(0, self.hotkey_stop),
            })
            if candidate.start():
                self.hotkeys = candidate
            else:
                candidate.stop()
                self.global_hotkeys_var.set(False)
                self.settings_error_label.configure(
                    text="F9–F11 could not be registered. Another app may be using them.",
                    text_color="#ff6b6b")

    def toggle_global_hotkeys(self):
        if self.global_hotkeys_var.get():
            self._start_global_hotkeys()
        elif self.hotkeys:
            if self.hotkeys.stop():
                self.hotkeys = None
            else:
                self.global_hotkeys_var.set(True)
                self.settings_error_label.configure(
                    text="Global hotkeys are still stopping; try again in a moment.",
                    text_color="#ff6b6b")

    def hotkey_play(self):
        # F9 starts playback, or resumes it when paused.
        if self.network.room_code:
            if self.network.is_host:
                self.sync_play()
            else:
                self.status_label.configure(
                    text="Only the host can start multiplayer playback.",
                    text_color="orange")
            return
        if not self.player.is_playing:
            self.play_solo()

    def hotkey_pause(self):
        if self.network.room_code:
            self.status_label.configure(
                text=("Pause is disabled in multiplayer because a local "
                      "pause would desynchronize the room. Use Stop instead."),
                text_color="orange")
            return
        self.player.pause()

    def hotkey_stop(self):
        if self.network.room_code and self.network.is_host:
            self.sync_stop()
        else:
            # Clients retain an emergency local stop for input safety, but
            # cannot forge a room-wide command.
            self.player.stop()
            if self.network.room_code:
                self.status_label.configure(
                    text="Stopped locally. Only the host can stop the room.",
                    text_color="orange")

    def on_player_output_error(self, message):
        if not self._closing:
            self.after(0, self._show_player_output_error, message)

    def _show_player_output_error(self, message):
        self.status_label.configure(text=message, text_color="red")

    def setup_solo_tab(self):
        self.tab_solo.grid_columnconfigure(0, weight=1)
        self.tab_solo.grid_rowconfigure(0, weight=1)

        # Scroll the entire Solo page, not just the channel list. Conversion
        # settings have grown over time and used to push "Solo Active
        # Channels" below the visible area when the window was shortened.
        # One page-level scrollbar keeps every section reachable and avoids
        # competing nested mouse-wheel regions.
        self.solo_scroll = ctk.CTkScrollableFrame(
            self.tab_solo, fg_color="transparent", corner_radius=0)
        self.solo_scroll.grid(
            row=0, column=0, padx=0, pady=0, sticky="nsew")
        self.solo_scroll.grid_columnconfigure(0, weight=1)

        # Live MIDI Keyboard
        self.live_midi_frame = ctk.CTkFrame(self.solo_scroll)
        self.live_midi_frame.grid(row=0, column=0, padx=10, pady=(10, 0), sticky="ew")
        self.live_midi_frame.grid_columnconfigure(1, weight=1)
        
        ctk.CTkLabel(self.live_midi_frame, text="Live MIDI Keyboard:").grid(row=0, column=0, padx=10, pady=10)
        
        devices = ["None"] + self.live_midi.get_devices()
        self.device_var = ctk.StringVar(value="None")
        self.device_menu = ctk.CTkOptionMenu(self.live_midi_frame, values=devices, variable=self.device_var, command=self.on_midi_device_select)
        self.device_menu.grid(row=0, column=1, padx=10, pady=10, sticky="ew")
        self.refresh_devices_btn = ctk.CTkButton(
            self.live_midi_frame, text="Refresh", width=70,
            command=self.refresh_midi_devices)
        self.refresh_devices_btn.grid(row=0, column=2, padx=(0, 10), pady=10)

        # Play Controls
        self.control_frame = ctk.CTkFrame(self.solo_scroll)
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
        self.autoplay_cb = ctk.CTkCheckBox(
            self.control_frame,
            text="Autoplay next track (host-led in multiplayer)",
            variable=self.autoplay_var, command=self._on_autoplay_toggle)
        self.autoplay_cb.grid(
            row=1, column=0, columnspan=2, padx=10, pady=(0, 10), sticky="w")

        # Optional session profiles: each loaded playlist item can retain its
        # own arrangement instead of inheriting whichever settings were used
        # by the previous track.
        self.per_track_settings_var = ctk.BooleanVar(value=False)
        self.per_track_settings_cb = ctk.CTkCheckBox(
            self.control_frame, text="Remember settings for each MIDI",
            variable=self.per_track_settings_var,
            command=self._on_per_track_settings_toggle)
        self.per_track_settings_cb.grid(
            row=1, column=2, columnspan=2, padx=10, pady=(0, 10), sticky="w")

        # --- Conversion Settings Panel ---
        self.conv_frame = ctk.CTkFrame(self.solo_scroll)
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
            command=self._on_max_chord_change)
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
            ("double_melody_octave", "Double single-note melody (octave)"),
        ]
        # Keep long labels on comfortably sized rows so the conversion panel
        # remains usable at its compact minimum width.
        self.checkbox_row_frames = []
        for row_checks in (checks[:4], checks[4:7], checks[7:10], checks[10:]):
            row = ctk.CTkFrame(self.conv_frame, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=(8, 0))
            self.checkbox_row_frames.append(row)
            for key, label in row_checks:
                var = ctk.BooleanVar(value=False)
                self.conv_vars[key] = var
                checkbox = ctk.CTkCheckBox(
                    row, text=label, variable=var,
                    command=lambda k=key: self._on_conversion_toggle(k))
                checkbox.pack(side="left", padx=(0, 14))
                if key == "double_melody_octave":
                    self.double_melody_octave_cb = checkbox

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

        # Row 4: source-aware part grouping
        self.row4 = row4 = ctk.CTkFrame(self.conv_frame, fg_color="transparent")
        row4.pack(fill="x", padx=10, pady=(0, 8))
        self.autosplit_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row4, text="Group parts", variable=self.autosplit_var,
                        command=self.reconvert).pack(side="left", padx=(0, 8))
        self.grouping_mode_var = ctk.StringVar(value="Musical roles")
        ctk.CTkOptionMenu(
            row4, values=list(GROUPING_LABELS),
            variable=self.grouping_mode_var, width=150,
            command=lambda _value: self.reconvert()
        ).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(row4, text="Role parts:").pack(side="left")
        self.autosplit_seg = ctk.CTkSegmentedButton(row4, values=["2", "3"],
                                                    command=lambda _: self.reconvert())
        self.autosplit_seg.set("2")
        self.autosplit_seg.pack(side="left", padx=6)
        ctk.CTkLabel(row4, text="(used by Musical roles)",
                     text_color="gray").pack(side="left", padx=4)

        # Drum-specific controls. They replace the pitch/chord controls when
        # Drum is selected, while BPM, speed, retrigger and safety remain useful.
        self.drum_controls_frame = ctk.CTkFrame(
            self.conv_frame, fg_color="transparent")
        drum_row1 = ctk.CTkFrame(self.drum_controls_frame, fg_color="transparent")
        drum_row1.pack(fill="x", pady=(8, 0))
        drum_row2 = ctk.CTkFrame(self.drum_controls_frame, fg_color="transparent")
        drum_row2.pack(fill="x", pady=(6, 8))

        self.drum_source_var = ctk.StringVar(value="Auto")
        self.drum_style_var = ctk.StringVar(value="Auto")
        self.drum_hat_var = ctk.StringVar(value="Eighth")
        ctk.CTkLabel(drum_row1, text="Drum source:").pack(side="left")
        ctk.CTkOptionMenu(
            drum_row1, values=["Auto", "Preserve", "Augment", "Generate"],
            variable=self.drum_source_var, width=105,
            command=lambda _value: self.reconvert()).pack(side="left", padx=(4, 12))
        ctk.CTkLabel(drum_row1, text="Style:").pack(side="left")
        ctk.CTkOptionMenu(
            drum_row1, values=["Auto", "Rock", "Pop", "Ballad", "Dance"],
            variable=self.drum_style_var, width=90,
            command=lambda _value: self.reconvert()).pack(side="left", padx=(4, 12))
        ctk.CTkLabel(drum_row1, text="Intensity:").pack(side="left")
        self.drum_intensity_entry = ctk.CTkEntry(drum_row1, width=45)
        self.drum_intensity_entry.insert(0, "1.0")
        self.drum_intensity_entry.pack(side="left", padx=(4, 12))
        ctk.CTkLabel(drum_row1, text="Fill every (bars):").pack(side="left")
        self.drum_fill_entry = ctk.CTkEntry(drum_row1, width=40)
        self.drum_fill_entry.insert(0, "8")
        self.drum_fill_entry.pack(side="left", padx=(4, 12))
        ctk.CTkLabel(drum_row1, text="Hats:").pack(side="left")
        ctk.CTkOptionMenu(
            drum_row1, values=["Quarter", "Eighth", "Sixteenth"],
            variable=self.drum_hat_var, width=105,
            command=lambda _value: self.reconvert()).pack(side="left", padx=4)

        ctk.CTkLabel(drum_row2, text="Bass follow (%):").pack(side="left")
        self.drum_bass_follow_entry = ctk.CTkEntry(drum_row2, width=45)
        self.drum_bass_follow_entry.insert(0, "100")
        self.drum_bass_follow_entry.pack(side="left", padx=(4, 12))
        ctk.CTkLabel(drum_row2, text="Swing (%):").pack(side="left")
        self.drum_swing_entry = ctk.CTkEntry(drum_row2, width=45)
        self.drum_swing_entry.insert(0, "0")
        self.drum_swing_entry.pack(side="left", padx=(4, 12))
        ctk.CTkLabel(drum_row2, text="Quantize (%):").pack(side="left")
        self.drum_quantize_entry = ctk.CTkEntry(drum_row2, width=45)
        self.drum_quantize_entry.insert(0, "0")
        self.drum_quantize_entry.pack(side="left", padx=(4, 12))
        ctk.CTkLabel(drum_row2, text="Min spacing (ms):").pack(side="left")
        self.drum_spacing_entry = ctk.CTkEntry(drum_row2, width=45)
        self.drum_spacing_entry.insert(0, "0")
        self.drum_spacing_entry.pack(side="left", padx=(4, 8))
        ctk.CTkLabel(
            drum_row2, text="0 = automatic", text_color="gray").pack(side="left")

        # Row 5: retrigger gap. How long a key is held UP before the same note
        # sounds again. Applies to every instrument - drums re-press the same 9
        # keys constantly - so unlike the rows above it stays visible in Drum
        # mode and is not added to _drum_hidden_widgets.
        row5 = ctk.CTkFrame(self.conv_frame, fg_color="transparent")
        row5.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkLabel(row5, text="Retrigger gap (ms):").pack(side="left")
        self.retrigger_gap_entry = ctk.CTkEntry(row5, width=45)
        self.retrigger_gap_entry.insert(0, "25")
        self.retrigger_gap_entry.pack(side="left", padx=(4, 8))
        ctk.CTkLabel(row5, text="raise if repeated notes sound like one long note",
                     text_color="gray").pack(side="left")

        safety_row = ctk.CTkFrame(self.conv_frame, fg_color="transparent")
        safety_row.pack(fill="x", padx=10, pady=(0, 8))
        self.focus_guard_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            safety_row, text="Only send keys when this window is focused:",
            variable=self.focus_guard_var, command=self.reconvert).pack(side="left")
        self.target_window_entry = ctk.CTkEntry(safety_row, width=150)
        self.target_window_entry.insert(0, "Blue Protocol")
        self.target_window_entry.pack(side="left", padx=6)
        self.global_hotkeys_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            safety_row, text="Global F9–F11",
            variable=self.global_hotkeys_var,
            command=self.toggle_global_hotkeys).pack(side="left", padx=8)
        ctk.CTkLabel(safety_row, text="Start delay:").pack(side="left")
        self.solo_delay_entry = ctk.CTkEntry(safety_row, width=40)
        self.solo_delay_entry.insert(0, "2")
        self.solo_delay_entry.pack(side="left", padx=4)
        ctk.CTkButton(
            safety_row, text="Reset settings", width=100,
            command=self.reset_settings).pack(side="right")

        self.settings_error_label = ctk.CTkLabel(
            self.conv_frame, text="", text_color="#ff6b6b")
        self.settings_error_label.pack(fill="x", padx=10, pady=(0, 6))

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
        self.drum_controls_frame.pack_forget()

        self.channel_section = ctk.CTkFrame(self.solo_scroll)
        self.channel_section.grid(
            row=3, column=0, padx=10, pady=10, sticky="ew")
        ctk.CTkLabel(
            self.channel_section, text="Solo Active Channels",
            font=ctk.CTkFont(weight="bold")).pack(
                fill="x", padx=10, pady=(8, 2))
        self.channel_frame = ctk.CTkFrame(
            self.channel_section, fg_color="transparent")
        self.channel_frame.pack(fill="x", padx=4, pady=(0, 8))

    def on_transpose(self, val):
        val = int(val)
        self.transpose_label.configure(text=f"Transpose: {val:+d}")
        self.player.set_transpose(val)

    def _inst(self):
        return INSTRUMENTS.get(self.instrument_var.get(), INSTRUMENTS["Piano"])

    def _restore_playback_state(self):
        """Reconcile shared pedal/modifier state after live MIDI releases."""
        if self.player.is_playing:
            self.player.restore_output_state()

    def _on_conversion_toggle(self, key):
        # These features plan the same global octave modifier differently.
        if key == "melody_lock" and self.conv_vars[key].get():
            self.conv_vars["phrase_gap_shifting"].set(False)
        elif key == "phrase_gap_shifting" and self.conv_vars[key].get():
            self.conv_vars["melody_lock"].set(False)
        self.reconvert()

    def _refresh_double_melody_control(self):
        enabled = bool(
            self.instrument_var.get() == "Piano"
            and self.max_chord_seg.get() != "1")
        self.double_melody_octave_cb.configure(
            state="normal" if enabled else "disabled")

    def _on_max_chord_change(self, _choice=None):
        self._refresh_double_melody_control()
        self.reconvert()

    def on_instrument_change(self, choice):
        """Apply an instrument's playable range to the Range fields, then
        re-fit the loaded MIDI into it. Drum is a different beast - it has
        no continuous playable range, so it gets its own hint text and hides
        every conversion control that doesn't apply to a generated beat."""
        rng = INSTRUMENTS.get(choice)
        is_drum = bool(rng and rng.get("is_drum"))
        self._refresh_double_melody_control()

        for widget, pack_kwargs in self._drum_hidden_widgets:
            if is_drum:
                widget.pack_forget()
            else:
                widget.pack(**pack_kwargs)
        if is_drum:
            self.drum_controls_frame.pack(
                fill="x", padx=10, pady=(0, 0), before=self.retrigger_gap_entry.master)
        else:
            self.drum_controls_frame.pack_forget()

        if rng:
            if is_drum:
                self.instrument_hint.configure(
                    text="preserve, enhance, or generate a meter-aware groove")
            else:
                lo, hi = midi_to_note_name(rng["low"]), midi_to_note_name(rng["high"])
                if not self._applying_track_profile:
                    self.range_low_entry.delete(0, "end")
                    self.range_low_entry.insert(0, lo)
                    self.range_high_entry.delete(0, "end")
                    self.range_high_entry.insert(0, hi)
                self.instrument_hint.configure(text=f"fits notes into {lo}–{hi}")
        if not self._applying_track_profile:
            self.reconvert()

    def on_midi_device_select(self, choice):
        if choice == "None":
            self.live_midi.stop_listening()
        else:
            self.live_midi.start_listening(choice)

    def refresh_midi_devices(self):
        current = self.device_var.get()
        devices = ["None"] + self.live_midi.get_devices()
        self.device_menu.configure(values=devices)
        if current not in devices:
            self.device_var.set("None")
            self.live_midi.stop_listening()

    def reset_settings(self):
        defaults = {
            self.bpm_entry: "",
            self.speed_entry: "1.0",
            self.range_low_entry: midi_to_note_name(self._inst()["low"]),
            self.range_high_entry: midi_to_note_name(self._inst()["high"]),
            self.duet_split_entry: "C4",
            self.shift_delay_entry: "30",
            self.shift_hold_entry: "10",
            self.retrigger_gap_entry: "25",
            self.drum_intensity_entry: "1.0",
            self.drum_fill_entry: "8",
            self.drum_bass_follow_entry: "100",
            self.drum_swing_entry: "0",
            self.drum_quantize_entry: "0",
            self.drum_spacing_entry: "0",
            self.solo_delay_entry: "2",
        }
        for entry, value in defaults.items():
            entry.delete(0, "end")
            entry.insert(0, value)
        for var in self.conv_vars.values():
            var.set(False)
        self.max_chord_seg.set("5")
        self._refresh_double_melody_control()
        self.autosplit_var.set(False)
        self.autosplit_seg.set("2")
        self.grouping_mode_var.set("Musical roles")
        self.drum_source_var.set("Auto")
        self.drum_style_var.set("Auto")
        self.drum_hat_var.set("Eighth")
        self.autoplay_var.set(False)
        self.per_track_settings_var.set(False)
        self.focus_guard_var.set(True)
        self.global_hotkeys_var.set(True)
        self.toggle_global_hotkeys()
        self.target_window_entry.delete(0, "end")
        self.target_window_entry.insert(0, "Blue Protocol")
        self.reconvert()

    def setup_multi_tab(self):
        self.tab_multi.grid_columnconfigure(0, weight=1)
        self.tab_multi.grid_rowconfigure(2, weight=1)

        # Connection Setup
        self.conn_frame = ctk.CTkFrame(self.tab_multi)
        self.conn_frame.grid(row=0, column=0, padx=10, pady=10, sticky="ew")
        self.conn_frame.grid_columnconfigure((0, 1), weight=1)
        
        self.nick_entry = ctk.CTkEntry(self.conn_frame, placeholder_text="Nickname")
        self.nick_entry.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        self.room_entry = ctk.CTkEntry(
            self.conn_frame, placeholder_text="Room Credential")
        self.room_entry.grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        self.copy_room_btn = ctk.CTkButton(
            self.conn_frame, text="Copy", width=55,
            command=self.copy_room_credential)
        self.copy_room_btn.grid(row=0, column=2, padx=(0, 5), pady=5)
        
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
        self.lobby_frame = ctk.CTkScrollableFrame(
            self.tab_multi,
            label_text="Lobby Players (Host assigns each player's parts here)")
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

        override_row = ctk.CTkFrame(
            self.client_control_frame, fg_color="transparent")
        override_row.grid(row=1, column=0, padx=5, pady=(0, 4), sticky="ew")
        self.client_conversion_override_var = ctk.BooleanVar(value=False)
        self.client_conversion_override_check = ctk.CTkCheckBox(
            override_row,
            text="Use my conversion settings (advanced)",
            variable=self.client_conversion_override_var,
            command=self.toggle_client_conversion,
            state="disabled")
        self.client_conversion_override_check.pack(side="left")
        ctk.CTkLabel(
            override_row,
            text="default: host arrangement · local parts are shared with host",
            text_color="gray").pack(side="left", padx=8)

        # Manual sync calibration: cancels the residual start offset that the
        # automatic clock sync can't remove (network path asymmetry + this
        # machine's input latency). Tune by ear until both players line up.
        nudge_row = ctk.CTkFrame(self.client_control_frame, fg_color="transparent")
        nudge_row.grid(row=2, column=0, padx=5, pady=(0, 6), sticky="ew")
        ctk.CTkLabel(nudge_row, text="Sync nudge (ms):").pack(side="left")
        self.nudge_entry = ctk.CTkEntry(nudge_row, width=60)
        self.nudge_entry.insert(0, "0")
        self.nudge_entry.pack(side="left", padx=6)
        ctk.CTkLabel(nudge_row, text="−earlier / +later · tune by ear, set once",
                     text_color="gray").pack(side="left", padx=6)

        self.leave_btn = ctk.CTkButton(self.client_control_frame, text="Leave Room",
                                       command=self.leave_room, fg_color="gray30",
                                       hover_color="gray20", state="disabled")
        self.leave_btn.grid(row=3, column=0, padx=5, pady=(0, 5), sticky="ew")

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
        simulator = self.player.simulator
        focus_blocked = bool(
            self.player.is_playing and simulator
            and simulator.focus_guard_enabled and not simulator.is_target_focused())
        if self.player.is_syncing:
            self.led_label.configure(
                text="🟡 Starting — switch to game", text_color="yellow")
        elif focus_blocked:
            if (simulator.key_refs or simulator.sustain_active
                    or simulator.current_octave_shift):
                self.player.release_output_state()
            self.led_label.configure(
                text="🟠 Waiting for game focus", text_color="orange")
        elif self._focus_was_blocked and self.player.is_playing:
            # Focus safety released the pedal/modifier state. Reapply it once
            # the game regains focus; in-progress notes stay released.
            self.player.restore_output_state()
            self.led_label.configure(text="🟢 Playing", text_color="green")
            if self._play_wait_message_active:
                self.settings_error_label.configure(
                    text="", text_color="#ff6b6b")
                self._play_wait_message_active = False
        elif self.player.is_playing:
            self.led_label.configure(text="🟢 Playing", text_color="green")
        else:
            self.led_label.configure(text="🔴 Stopped", text_color="gray")

        if simulator and not self.player.is_playing:
            # Apply any pedal-off deferred by focus safety, but only once the
            # simulator confirms the intended game window is focused.
            simulator.flush_pending_sustain()

        self._focus_was_blocked = focus_blocked
        if (self._play_wait_message_active and self.player.is_playing
                and not focus_blocked):
            self.settings_error_label.configure(
                text="", text_color="#ff6b6b")
            self._play_wait_message_active = False
        self._refresh_progress_bar()

        # Auto-advance song if finished naturally (only when Autoplay is on).
        # On natural finish the player already released all keys; with autoplay
        # off we simply stop here instead of loading the next track.
        is_playing_now = self.player.is_playing
        if (self.was_playing and not is_playing_now
                and not self.player.stop_requested
                and not self.player.is_paused
                and self.autoplay_var.get()
                and (not self.network.room_code or self.network.is_host)):
            # Multiplayer transitions are host-owned. Clients wait for the
            # authenticated next file and synchronized Play command.
            self.next_song(autoplay=True)

        self.was_playing = is_playing_now
            
        self.after(200, self.update_led_loop)

    def load_files(self):
        file_paths = filedialog.askopenfilenames(filetypes=[("MIDI Files", "*.mid *.midi")])
        if file_paths:
            last_new_idx = None
            existing_paths = {
                os.path.normcase(os.path.abspath(song["path"]))
                for song in self.playlist
            }
            for p in file_paths:
                normalized = os.path.normcase(os.path.abspath(p))
                if normalized in existing_paths:
                    continue
                filename = os.path.basename(p)
                name = self._unique_song_label(filename, p)
                self.playlist.append({
                    "name": name, "filename": filename, "path": p})
                existing_paths.add(normalized)
                last_new_idx = len(self.playlist) - 1

            self._update_playlist_ui()

            # Jump to whatever was just loaded instead of leaving it sitting
            # unselected in the dropdown while a different song stays active
            # (feedback: loading a MIDI should make it the selected one).
            if last_new_idx is not None:
                self._remember_current_track_profile()
                self.current_song_idx = last_new_idx
                self._load_current_song()

    def _unique_song_label(self, filename, path):
        used = {song["name"] for song in self.playlist}
        if filename not in used:
            return filename
        parent = os.path.basename(os.path.dirname(os.path.abspath(path)))
        base = f"{filename} — {parent or 'folder'}"
        label = base
        suffix = 2
        while label in used:
            label = f"{base} ({suffix})"
            suffix += 1
        return label

    @staticmethod
    def _playlist_filename(song):
        return song.get("filename") or os.path.basename(song["path"])

    def remove_current_song(self):
        """Drop the currently selected song from the playlist (feedback:
        no way to clear out songs once loaded)."""
        if not (0 <= self.current_song_idx < len(self.playlist)):
            return
        self._parse_generation += 1
        self.player.stop()
        del self.playlist[self.current_song_idx]

        if not self.playlist:
            self.current_song_idx = -1
            self.raw_events = []
            self.events = []
            self.channels = []
            self.channel_programs = {}
            self.song_info_label.configure(text="")
            self.player.load_events([], [])
            self.build_solo_channel_ui()
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
                self._remember_current_track_profile()
                self.current_song_idx = idx
                self._load_current_song()
                break

    def prev_song(self):
        if not self.playlist: return
        self._remember_current_track_profile()
        self.current_song_idx = (self.current_song_idx - 1) % len(self.playlist)
        self._load_current_song(autoplay=self.player.is_playing)

    def next_song(self, autoplay=False):
        if not self.playlist: return
        was_playing = self.player.is_playing or autoplay
        self._remember_current_track_profile()
        self.current_song_idx = (self.current_song_idx + 1) % len(self.playlist)
        self._load_current_song(autoplay=was_playing)

    def _load_current_song(self, autoplay=False):
        if 0 <= self.current_song_idx < len(self.playlist):
            song = self.playlist[self.current_song_idx]
            self.song_var.set(song["name"])
            self.player.stop()
            profile = self._profile_for_song(song)
            if self.per_track_settings_var.get() and profile:
                try:
                    self._apply_conversion_profile_to_ui(profile)
                except ValueError as exc:
                    song.pop("conversion_profile", None)
                    song.pop("prepared", None)
                    self.settings_error_label.configure(
                        text=f"Saved track settings were invalid: {exc}",
                        text_color="#ff6b6b")
                    profile = self._current_conversion_profile
            on_loaded = self._on_autoplay_track_loaded if autoplay else None
            if (profile and prepared_track_matches(
                    song.get("prepared"), song["path"], profile)):
                self._activate_prepared_song(
                    song, song["prepared"], on_loaded=on_loaded)
                return
            self._parse_and_load(
                song["path"], on_loaded=on_loaded)
            if self.network.room_code:
                self._update_lobby_ui(self.network.room_state)

    def _activate_prepared_song(self, song, prepared, on_loaded=None):
        """Install a background-prepared item without parsing/converting again."""
        self._parse_generation += 1
        profile = prepared["profile"]
        settings, instrument_name = load_conversion_profile(profile)
        if self.per_track_settings_var.get():
            self._apply_conversion_profile_to_ui(profile)
        self._current_conversion_profile = deepcopy(profile)
        parsed = prepared["parsed"]
        self.raw_events = parsed.get("events", [])
        self.orig_bpm = parsed.get("bpm", 120.0)
        self.beats_per_measure = parsed.get("beats_per_measure", 4)
        self.tempo_map = parsed.get(
            "tempo_map", [{"beat": 0.0, "bpm": self.orig_bpm}])
        self.time_signature_map = parsed.get("time_signature_map", [{
            "beat": 0.0, "numerator": self.beats_per_measure,
            "denominator": 4,
        }])
        self.channel_programs = parsed.get("channel_programs", {})
        self.events = prepared["events"]
        self.channels = list(prepared["channels"])

        instrument = INSTRUMENTS[instrument_name]
        if self.player.simulator:
            self.player.simulator.shift_delay_ms = min(max(
                self._get_float(self.shift_delay_entry, 30.0), 0.0), 500.0)
            self.player.simulator.shift_hold_ms = min(max(
                self._get_float(self.shift_hold_entry, 10.0), 0.0), 500.0)
            self.player.simulator.retrigger_gap_ms = (
                settings.retrigger_gap * 1000.0)
            self.player.simulator.key_offset = instrument["offset"]
            self.player.simulator.focus_guard_enabled = self.focus_guard_var.get()
            self.player.simulator.target_window_text = (
                self.target_window_entry.get().strip() or "Blue Protocol")

        is_drum = bool(instrument.get("is_drum", False))
        self.build_solo_channel_ui(
            duet=(settings.duet_mode and not is_drum),
            auto=(settings.auto_split and not is_drum),
            parts=settings.auto_split_parts,
            grouping_mode=settings.grouping_mode)
        self._apply_song_channel_selection(song)
        selected_channels = [
            channel for channel, variable in self.channel_vars
            if variable.get()]
        self.player.load_events(self.events, selected_channels)
        raw_notes = sum(
            event.get("type") == "note_on" for event in self.raw_events)
        output_notes = sum(
            event.get("type") == "note_on" for event in self.events)
        duration = self.events[-1]["time"] if self.events else 0.0
        self.song_info_label.configure(
            text=f"{self.orig_bpm:.0f} BPM · {self.beats_per_measure}/4 · "
                 f"{output_notes}/{raw_notes} notes · {duration:.1f}s · prepared")
        parse_error = parsed.get("error")
        if parse_error:
            self.settings_error_label.configure(
                text=parse_error, text_color="#ff6b6b")
        elif not self.raw_events:
            self.settings_error_label.configure(
                text="The file has no playable MIDI notes.",
                text_color="#ff6b6b")
        else:
            self.settings_error_label.configure(
                text="", text_color="#ff6b6b")
        self._remember_current_track_profile(profile)
        if self.network.room_code and self.network.is_host and self.events:
            self._share_current_midi()
            self._update_lobby_ui(self.network.room_state)
        if self.events and on_loaded:
            on_loaded()
        self._schedule_next_preparation()

    def _share_current_midi(self, target_client_id=None):
        if (not self.network.is_host
                or not (0 <= self.current_song_idx < len(self.playlist))
                or not self._current_conversion_profile):
            return
        song = self.playlist[self.current_song_idx]
        shared = self.network.share_midi(
            song["path"], self._playlist_filename(song),
            self._current_conversion_profile,
            target_client_id=target_client_id)
        if shared and target_client_id is None:
            self._publish_part_manifest()

    def _parse_and_load(self, file_path, on_loaded=None,
                        network_revision=None):
        """Parse potentially large files without freezing Tk's event loop."""
        self._parse_generation += 1
        generation = self._parse_generation
        self.player.stop()
        self.raw_events = []
        self.events = []
        self.player.load_events([], [])
        self.song_info_label.configure(text="Loading and checking MIDI…")
        if self.network.room_code and not self.network.is_host:
            if self.my_ready_status:
                self.network.send_ready_status(False)
            self.my_ready_status = False
            self._accepted_network_revision = None
            self._loading_network_revision = network_revision
            self.ready_btn.configure(state="disabled", text="Loading MIDI…")

        def worker():
            try:
                parsed = parse_midi_full(file_path)
            except Exception as exc:
                print(f"Unexpected MIDI parser error: {exc}")
                parsed = {
                    "events": [],
                    "bpm": 120.0,
                    "beats_per_measure": 4,
                    "tempo_map": [{"beat": 0.0, "bpm": 120.0}],
                    "time_signature_map": [{
                        "beat": 0.0, "numerator": 4, "denominator": 4}],
                    "channel_programs": {},
                    "error": f"Could not parse MIDI: {exc}",
                }
            if not self._closing:
                self.after(
                    0, self._finish_parse, generation, parsed, on_loaded,
                    network_revision)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_parse(self, generation, parsed, on_loaded=None,
                      network_revision=None):
        if generation != self._parse_generation:
            return
        self.raw_events = parsed['events']
        self.orig_bpm = parsed['bpm']
        self.beats_per_measure = parsed['beats_per_measure']
        self.tempo_map = parsed.get(
            'tempo_map', [{'beat': 0.0, 'bpm': self.orig_bpm}])
        self.time_signature_map = parsed.get('time_signature_map', [{
            'beat': 0.0, 'numerator': self.beats_per_measure, 'denominator': 4
        }])
        self.channel_programs = parsed.get('channel_programs', {})
        self.song_info_label.configure(
            text=f"{self.orig_bpm:.0f} BPM · {self.beats_per_measure}/4")
        converted = self.reconvert(broadcast_profile=False)
        if (network_revision is not None and converted
                and self.raw_events and self.events):
            self._accepted_network_revision = network_revision
            self._publish_part_manifest()
        self._loading_network_revision = None
        parse_error = parsed.get("error")
        if parse_error:
            self.settings_error_label.configure(
                text=parse_error, text_color="#ff6b6b")
        elif not self.raw_events:
            self.settings_error_label.configure(
                text="The file has no playable MIDI notes.",
                text_color="#ff6b6b")
        if (converted and self.raw_events
                and self.network.room_code and self.network.is_host):
            self._share_current_midi()
        if converted and self.events and on_loaded:
            on_loaded()
        if self.network.room_code and not self.network.is_host:
            self._refresh_client_ready_button()

    def _get_float(self, entry, default):
        try:
            value = float(entry.get().replace(",", "."))
            return value if math.isfinite(value) else default
        except (ValueError, AttributeError):
            return default

    @staticmethod
    def _set_entry(entry, value):
        entry.delete(0, "end")
        entry.insert(0, str(value))

    @staticmethod
    def _number_text(value):
        return f"{float(value):g}"

    def _apply_conversion_profile_to_ui(self, profile):
        """Show a playlist item's validated conversion profile in the panel."""
        settings, instrument_name = load_conversion_profile(profile)
        reverse_grouping = {value: label for label, value in GROUPING_LABELS.items()}
        self._applying_track_profile = True
        try:
            self.instrument_var.set(instrument_name)
            self.on_instrument_change(instrument_name)
            for key, variable in self.conv_vars.items():
                variable.set(bool(getattr(settings, key)))
            self._set_entry(
                self.bpm_entry,
                "" if settings.bpm_override is None
                else self._number_text(settings.bpm_override))
            self._set_entry(self.speed_entry, self._number_text(settings.speed))
            self.max_chord_seg.set(str(settings.max_chord_notes))
            self._refresh_double_melody_control()
            self._set_entry(
                self.range_low_entry, midi_to_note_name(settings.range_low))
            self._set_entry(
                self.range_high_entry, midi_to_note_name(settings.range_high))
            self._set_entry(
                self.duet_split_entry,
                midi_to_note_name(settings.duet_split_note))
            self.autosplit_var.set(bool(settings.auto_split))
            self.autosplit_seg.set(str(settings.auto_split_parts))
            self.grouping_mode_var.set(
                reverse_grouping.get(settings.grouping_mode, "Musical roles"))
            self._set_entry(
                self.retrigger_gap_entry,
                self._number_text(settings.retrigger_gap * 1000.0))
            self.drum_source_var.set(settings.drum_source_mode.title())
            self.drum_style_var.set(settings.drum_style.title())
            self.drum_hat_var.set(settings.drum_hat_density.title())
            self._set_entry(
                self.drum_intensity_entry,
                self._number_text(settings.drum_intensity))
            self._set_entry(
                self.drum_fill_entry, str(settings.drum_fill_frequency))
            self._set_entry(
                self.drum_bass_follow_entry,
                self._number_text(settings.drum_bass_follow * 100.0))
            self._set_entry(
                self.drum_swing_entry,
                self._number_text(settings.drum_swing * 100.0))
            self._set_entry(
                self.drum_quantize_entry,
                self._number_text(settings.drum_quantize * 100.0))
            self._set_entry(
                self.drum_spacing_entry,
                self._number_text(settings.drum_min_spacing * 1000.0))
        finally:
            self._applying_track_profile = False

    def _current_song(self):
        if 0 <= self.current_song_idx < len(self.playlist):
            return self.playlist[self.current_song_idx]
        return None

    def _remember_current_track_profile(self, profile=None):
        """Keep the current conversion and selected parts with this MIDI."""
        if not self.per_track_settings_var.get():
            return
        song = self._current_song()
        profile = profile or self._current_conversion_profile
        if song is None or not profile:
            return
        previous = song.get("conversion_profile")
        song["conversion_profile"] = deepcopy(profile)
        song["active_channels"] = [
            channel for channel, variable in self.channel_vars
            if variable.get()]
        if previous != profile:
            song.pop("prepared", None)

        # A client playlist contains only the host's current temporary file.
        # Keep local-override profiles by authenticated MIDI hash so returning
        # to a song during the same room session restores its own arrangement.
        if (self.network.room_code and not self.network.is_host
                and self.client_conversion_override_var.get()):
            song_id = self.network.room_state.get("song_id")
            if song_id:
                self._network_track_profiles[song_id] = deepcopy(profile)
                while len(self._network_track_profiles) > 64:
                    self._network_track_profiles.pop(
                        next(iter(self._network_track_profiles)))

    def _profile_for_song(self, song):
        if self.per_track_settings_var.get():
            profile = song.get("conversion_profile")
            if not profile and self._current_conversion_profile:
                profile = deepcopy(self._current_conversion_profile)
                song["conversion_profile"] = profile
            return profile
        return self._current_conversion_profile

    def _apply_song_channel_selection(self, song):
        if "active_channels" not in song:
            return
        selected = set(song.get("active_channels", []))
        for channel, variable in self.channel_vars:
            variable.set(channel in selected)
        self.update_solo_channels(remember=False)

    def _on_autoplay_toggle(self):
        if not self.autoplay_var.get():
            self._multiplayer_autoplay_pending_revision = None
            self._multiplayer_auto_ready_revision = None
            self._preparation_generation += 1
        else:
            self._schedule_next_preparation()
            self._maybe_auto_ready_multiplayer()
        self.save_prefs()

    def _on_per_track_settings_toggle(self):
        if self.per_track_settings_var.get():
            self._remember_current_track_profile()
            self._schedule_next_preparation()
        else:
            self._preparation_generation += 1
        self.save_prefs()

    def _schedule_next_preparation(self):
        """Prepare the next playlist item off the UI thread for autoplay."""
        if (not self.autoplay_var.get() or len(self.playlist) < 2
                or not (0 <= self.current_song_idx < len(self.playlist))
                or (self.network.room_code and not self.network.is_host)):
            return
        song = self.playlist[(self.current_song_idx + 1) % len(self.playlist)]
        profile = self._profile_for_song(song)
        if not profile:
            return
        if prepared_track_matches(song.get("prepared"), song["path"], profile):
            return

        self._preparation_generation += 1
        generation = self._preparation_generation
        profile = deepcopy(profile)

        def worker():
            prepared = None
            error = None
            try:
                prepared = prepare_track(song["path"], profile)
            except Exception as exc:  # noqa: BLE001 - preparation is non-fatal
                error = str(exc)
            if not self._closing:
                self.after(
                    0, self._finish_track_preparation,
                    generation, song, prepared, error)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_track_preparation(self, generation, song, prepared, error):
        if (generation != self._preparation_generation
                or not any(candidate is song for candidate in self.playlist)):
            return
        if prepared is not None:
            song["prepared"] = prepared
            current = self._current_song()
            # Keep memory bounded even for a very long set list.  Only the
            # active item and the one prepared ahead are useful for a smooth
            # transition; older parsed/conversion copies can be reclaimed.
            for candidate in self.playlist:
                if candidate is not song and candidate is not current:
                    candidate.pop("prepared", None)
        elif error:
            # Normal loading will still report the parse/conversion error if
            # this item is selected; background preparation stays non-fatal.
            song.pop("prepared", None)

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
                "retrigger_gap": self.retrigger_gap_entry.get(),
                "drum_source": self.drum_source_var.get(),
                "drum_style": self.drum_style_var.get(),
                "drum_intensity": self.drum_intensity_entry.get(),
                "drum_fills": self.drum_fill_entry.get(),
                "drum_hats": self.drum_hat_var.get(),
                "drum_bass_follow": self.drum_bass_follow_entry.get(),
                "drum_swing": self.drum_swing_entry.get(),
                "drum_quantize": self.drum_quantize_entry.get(),
                "drum_spacing": self.drum_spacing_entry.get(),
                "autosplit": self.autosplit_var.get(),
                "autosplit_parts": self.autosplit_seg.get(),
                "grouping_mode": self.grouping_mode_var.get(),
                "instrument": self.instrument_var.get(),
                "autoplay": self.autoplay_var.get(),
                "per_track_settings": self.per_track_settings_var.get(),
                "focus_guard": self.focus_guard_var.get(),
                "target_window": self.target_window_entry.get(),
                "global_hotkeys": self.global_hotkeys_var.get(),
                "solo_delay": self.solo_delay_entry.get(),
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
        if (self.conv_vars["melody_lock"].get()
                and self.conv_vars["phrase_gap_shifting"].get()):
            # Older preferences could persist this ambiguous combination.
            self.conv_vars["phrase_gap_shifting"].set(False)

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
        _set_entry(self.retrigger_gap_entry, prefs.get("retrigger_gap"))
        self.drum_source_var.set(prefs.get("drum_source", "Auto"))
        self.drum_style_var.set(prefs.get("drum_style", "Auto"))
        self.drum_hat_var.set(prefs.get("drum_hats", "Eighth"))
        _set_entry(self.drum_intensity_entry, prefs.get("drum_intensity", "1.0"))
        _set_entry(self.drum_fill_entry, prefs.get("drum_fills", "8"))
        _set_entry(
            self.drum_bass_follow_entry, prefs.get("drum_bass_follow", "100"))
        _set_entry(self.drum_swing_entry, prefs.get("drum_swing", "0"))
        _set_entry(self.drum_quantize_entry, prefs.get("drum_quantize", "0"))
        _set_entry(self.drum_spacing_entry, prefs.get("drum_spacing", "0"))

        if prefs.get("max_chord_notes"):
            self.max_chord_seg.set(prefs["max_chord_notes"])
        self._refresh_double_melody_control()
        if prefs.get("autosplit_parts"):
            self.autosplit_seg.set(prefs["autosplit_parts"])
        grouping = prefs.get("grouping_mode", "Musical roles")
        if grouping in GROUPING_LABELS:
            self.grouping_mode_var.set(grouping)
        self.autosplit_var.set(bool(prefs.get("autosplit", False)))
        self.autoplay_var.set(bool(prefs.get("autoplay", False)))
        self.per_track_settings_var.set(bool(
            prefs.get("per_track_settings", False)))
        self.focus_guard_var.set(bool(prefs.get("focus_guard", True)))
        self.global_hotkeys_var.set(bool(prefs.get("global_hotkeys", True)))
        _set_entry(
            self.target_window_entry,
            prefs.get("target_window", "Blue Protocol"))
        _set_entry(self.solo_delay_entry, prefs.get("solo_delay", "2"))
        # Re-apply after every field has been restored. The instrument callback
        # above runs before the timing entries are populated.
        self.reconvert()

    def build_settings(self):
        """Collect the conversion panel state into a ConversionSettings."""
        def finite_float(entry, label, low, high, allow_blank=False):
            text = entry.get().strip().replace(",", ".")
            if allow_blank and not text:
                return None
            try:
                value = float(text)
            except ValueError:
                raise ValueError(f"{label} must be a number.")
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{label} must be between {low:g} and {high:g}.")
            return value

        def midi_note(entry, label):
            value = note_name_to_midi(entry.get())
            if value is None or not 0 <= value <= 127:
                raise ValueError(
                    f"{label} must be a note such as C4 or a MIDI number from 0 to 127.")
            return value

        bpm_override = finite_float(
            self.bpm_entry, "BPM", 20, 400, allow_blank=True)
        speed = finite_float(self.speed_entry, "Speed", 0.1, 4)
        retrigger_ms = finite_float(
            self.retrigger_gap_entry, "Retrigger gap", 0, 500)
        is_drum = self._inst().get("is_drum", False)
        if is_drum:
            range_low, range_high = self._inst()["low"], self._inst()["high"]
            duet_split = 60
            shift_delay, shift_hold = 30.0, 10.0
            drum_intensity = finite_float(
                self.drum_intensity_entry, "Drum intensity", 0.25, 2)
            drum_fills = finite_float(
                self.drum_fill_entry, "Fill frequency", 0, 32)
            if drum_fills != int(drum_fills):
                raise ValueError("Fill frequency must be a whole number.")
            drum_bass_follow = finite_float(
                self.drum_bass_follow_entry, "Bass follow", 0, 100)
            drum_swing = finite_float(
                self.drum_swing_entry, "Swing", 0, 50)
            drum_quantize = finite_float(
                self.drum_quantize_entry, "Quantize", 0, 100)
            drum_spacing = finite_float(
                self.drum_spacing_entry, "Minimum spacing", 0, 500)
        else:
            range_low = midi_note(self.range_low_entry, "Range start")
            range_high = midi_note(self.range_high_entry, "Range end")
            if range_low > range_high:
                raise ValueError("Range start must not be above range end.")
            duet_enabled = (
                self.conv_vars["duet_mode"].get()
                and not self.autosplit_var.get())
            duet_split = (
                midi_note(self.duet_split_entry, "Duet split")
                if duet_enabled else 60)
            shift_delay = finite_float(
                self.shift_delay_entry, "Shift delay", 0, 500)
            shift_hold = finite_float(
                self.shift_hold_entry, "Shift hold", 0, 500)
            drum_intensity, drum_fills = 1.0, 8
            drum_bass_follow, drum_swing = 100.0, 0.0
            drum_quantize, drum_spacing = 0.0, 0.0
        s = ConversionSettings(
            bpm_override=bpm_override,
            speed=speed,
            max_chord_notes=int(self.max_chord_seg.get()),
            note_thinning=self.conv_vars["note_thinning"].get(),
            cull_low_priority=self.conv_vars["cull_low_priority"].get(),
            prioritize_melody=self.conv_vars["prioritize_melody"].get(),
            double_melody_octave=(
                self.instrument_var.get() == "Piano"
                and self.conv_vars["double_melody_octave"].get()),
            proportional_remap=self.conv_vars["proportional_remap"].get(),
            consistent_windows=self.conv_vars["consistent_windows"].get(),
            voice_aware=self.conv_vars["voice_aware"].get(),
            phrase_gap_shifting=self.conv_vars["phrase_gap_shifting"].get(),
            melody_lock=self.conv_vars["melody_lock"].get(),
            melody_lock_mode='drop',
            duet_mode=self.conv_vars["duet_mode"].get(),
            duet_split_note=duet_split,
            auto_split=self.autosplit_var.get(),
            auto_split_parts=int(self.autosplit_seg.get()),
            grouping_mode=GROUPING_LABELS.get(
                self.grouping_mode_var.get(), "roles"),
            disable_sustain=self.conv_vars["disable_sustain"].get(),
            reach_low=self._inst()["low"],
            reach_high=self._inst()["high"],
            instrument_offset=self._inst()["offset"],
            range_low=range_low,
            range_high=range_high,
            retrigger_gap=retrigger_ms / 1000.0,
            drum_source_mode=self.drum_source_var.get().lower(),
            drum_style=self.drum_style_var.get().lower(),
            drum_intensity=drum_intensity,
            drum_fill_frequency=int(drum_fills),
            drum_hat_density=self.drum_hat_var.get().lower(),
            drum_bass_follow=drum_bass_follow / 100.0,
            drum_swing=drum_swing / 100.0,
            drum_quantize=drum_quantize / 100.0,
            drum_min_spacing=drum_spacing / 1000.0,
        )
        s._shift_delay_ms = shift_delay
        s._shift_hold_ms = shift_hold
        self.save_prefs()
        return s

    def reconvert(self, broadcast_profile=True):
        """Re-run the conversion pipeline on the raw MIDI with current settings."""
        using_local_override = bool(
            self.network.room_code and not self.network.is_host
            and self.client_conversion_override_var.get())
        using_host_profile = should_use_host_conversion(
            self.network.room_code, self.network.is_host,
            self._network_conversion_profile, using_local_override)
        local_change_requires_ready = bool(
            using_local_override and broadcast_profile)
        if local_change_requires_ready:
            self._mark_client_unready("Applying my settings…")
        try:
            if using_host_profile:
                settings, instrument_name = load_conversion_profile(
                    self._network_conversion_profile)
                # Modifier timing is hardware-specific and remains local.
                shift_delay = min(max(
                    self._get_float(self.shift_delay_entry, 30.0), 0.0), 500.0)
                shift_hold = min(max(
                    self._get_float(self.shift_hold_entry, 10.0), 0.0), 500.0)
            else:
                settings = self.build_settings()
                instrument_name = self.instrument_var.get()
                shift_delay = settings._shift_delay_ms
                shift_hold = settings._shift_hold_ms
                self._current_conversion_profile = make_conversion_profile(
                    settings, instrument_name)
        except ValueError as exc:
            self.settings_error_label.configure(
                text=str(exc), text_color="#ff6b6b")
            return False
        self.settings_error_label.configure(text="", text_color="#ff6b6b")
        instrument = INSTRUMENTS[instrument_name]

        # Apply input timing knobs + the instrument's key offset even when no
        # song is loaded; live-MIDI-only users rely on restored preferences.
        if self.player.simulator:
            self.player.simulator.shift_delay_ms = shift_delay
            self.player.simulator.shift_hold_ms = shift_hold
            self.player.simulator.retrigger_gap_ms = settings.retrigger_gap * 1000.0
            self.player.simulator.key_offset = instrument["offset"]
            self.player.simulator.focus_guard_enabled = self.focus_guard_var.get()
            self.player.simulator.target_window_text = (
                self.target_window_entry.get().strip() or "Blue Protocol")

        if not self.raw_events:
            self.events = []
            self.channels = []
            self.build_solo_channel_ui()
            self.player.load_events([], [])
            return True
        is_drum = instrument.get("is_drum", False)
        if is_drum:
            self.events = convert_drum(self.raw_events, settings, orig_bpm=self.orig_bpm,
                                       beats_per_measure=self.beats_per_measure,
                                       tempo_map=self.tempo_map,
                                       time_signature_map=self.time_signature_map)
        else:
            self.events = convert(self.raw_events, settings, orig_bpm=self.orig_bpm)
        self.channels = get_channels_info(self.events)

        # Drum output is always a single channel - ignore any leftover
        # duet/auto-split state from before the instrument was switched, so
        # the Solo panel doesn't show stale "Duet Low/High" style labels.
        self.build_solo_channel_ui(duet=(settings.duet_mode and not is_drum),
                                   auto=(settings.auto_split and not is_drum),
                                   parts=settings.auto_split_parts,
                                   grouping_mode=settings.grouping_mode)
        song = self._current_song()
        if self.per_track_settings_var.get() and song is not None:
            self._apply_song_channel_selection(song)
        selected_channels = [
            channel for channel, variable in self.channel_vars
            if variable.get()]
        self.player.load_events(self.events, selected_channels)
        raw_notes = sum(
            ev.get("type") == "note_on" for ev in self.raw_events)
        output_notes = sum(
            ev.get("type") == "note_on" for ev in self.events)
        duration = self.events[-1]["time"] if self.events else 0
        profile_suffix = (
            f" · host settings: {instrument_name}"
            if using_host_profile else (
                f" · my settings: {instrument_name}"
                if using_local_override else ""))
        self.song_info_label.configure(
            text=f"{self.orig_bpm:.0f} BPM · {self.beats_per_measure}/4 · "
                 f"{output_notes}/{raw_notes} notes · {duration:.1f}s"
                 f"{profile_suffix}")

        if not using_host_profile:
            self._remember_current_track_profile(
                self._current_conversion_profile)
        self._schedule_next_preparation()

        if self.network.room_code and self.network.is_host:
            if broadcast_profile and self._current_conversion_profile:
                if self.network.share_conversion_profile(
                        self._current_conversion_profile):
                    self._publish_part_manifest()
            self._update_lobby_ui(self.network.room_state)
        elif local_change_requires_ready:
            if self.events:
                self._publish_part_manifest()
                self._refresh_client_ready_button()
                self.status_label.configure(
                    text="My conversion updated. Mark Ready again.",
                    text_color="orange")
            else:
                self.status_label.configure(
                    text="My conversion produced no playable notes.",
                    text_color="red")
        return True

    def _channel_ranges(self):
        """Lowest/highest MIDI note per channel in the converted events."""
        ranges = {}
        for ev in self.events:
            if ev.get('type') == 'note_on' and 'channel' in ev:
                lo, hi = ranges.get(ev['channel'], (999, -1))
                ranges[ev['channel']] = (min(lo, ev['note']), max(hi, ev['note']))
        return ranges

    def _channel_labels(self):
        """User-facing converted part names, shared by Solo and lobby UI."""
        labels = {}
        for event in self.events:
            if event.get('type') != 'note_on':
                continue
            channel = event.get('channel', 0)
            if event.get('group_name'):
                labels.setdefault(channel, event['group_name'])
        for channel in self.channels:
            if channel in labels:
                continue
            guess = guess_channel_instrument(channel, self.channel_programs)
            labels[channel] = (
                f"MIDI Ch. {channel + 1} - likely {guess}"
                if guess else f"MIDI Ch. {channel + 1}")
        return labels

    def _publish_part_manifest(self):
        """Tell the host which parts this machine can actually play."""
        if not self.network.room_code:
            return False
        revision = self.network.room_state.get("revision", 0)
        if revision <= 0:
            return False
        if (not self.network.is_host
                and self._accepted_network_revision != revision):
            return False
        labels = self._channel_labels()
        parts = [{"channel": channel,
                  "label": labels.get(channel, f"Part {channel + 1}")}
                 for channel in self.channels]
        using_local = bool(
            not self.network.is_host
            and self.client_conversion_override_var.get())
        return self.network.send_part_manifest(
            parts, local_conversion=using_local, revision=revision)

    def _player_part_options(self, player, state):
        """Part choices for one lobby member, with legacy-host fallback."""
        revision = state.get("revision", 0)
        if player.get("parts_revision") == revision:
            return [
                (part["channel"], part["label"])
                for part in player.get("available_parts", [])]
        labels = self._channel_labels()
        return [(channel, labels.get(channel, f"Part {channel + 1}"))
                for channel in self.channels]

    def build_solo_channel_ui(self, duet=False, auto=False, parts=2,
                              grouping_mode='roles'):
        for widget in self.channel_frame.winfo_children():
            widget.destroy()
        self.channel_vars = []
        ranges = self._channel_ranges()
        group_metadata = {}
        confidence_rank = {
            'unknown': 0, 'low': 1, 'medium': 2, 'inferred': 2, 'high': 3}
        for event in self.events:
            if event.get('type') != 'note_on':
                continue
            channel = event.get('channel', 0)
            info = group_metadata.setdefault(channel, {
                'name': event.get('group_name'),
                'is_drum': bool(event.get('group_is_drum', False)),
                'confidence': event.get('group_confidence', 'unknown'),
            })
            confidence = event.get('group_confidence', 'unknown')
            if confidence_rank.get(confidence, 0) < confidence_rank.get(
                    info['confidence'], 0):
                info['confidence'] = confidence

        def name_for(ch):
            if auto:
                info = group_metadata.get(ch, {})
                name = info.get('name')
                if not name and grouping_mode == 'roles':
                    if parts >= 3:
                        name = {0: "Melody", 1: "Harmony", 2: "Bass"}.get(ch)
                    else:
                        name = {0: "Melody", 1: "Accompaniment"}.get(ch)
                name = name or f"Part {ch + 1}"
                if (grouping_mode == 'families'
                        and info.get('confidence') not in (None, 'unknown')):
                    name += f" ({info['confidence']} confidence)"
                return name
            if duet:
                return {0: "Duet Low (bass)", 1: "Duet High (melody)"}.get(
                    ch, f"Part {ch + 1}")
            # Plain (no auto-split/duet) mode: keep the raw channel number,
            # but tack on an educated instrument guess when we have one -
            # from the channel's GM Program Change, or "Drums" for the
            # reserved GM percussion channel. Channels with neither just
            # stay "Channel N" as before rather than showing a guess we
            # aren't confident in.
            guess = guess_channel_instrument(ch, self.channel_programs)
            if guess:
                return f"MIDI Ch. {ch + 1} - likely {guess}"
            return f"MIDI Ch. {ch + 1}"

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
            default_selected = not (
                auto and group_metadata.get(ch, {}).get('is_drum', False))
            var = ctk.BooleanVar(value=default_selected)
            cb = ctk.CTkCheckBox(row, text=name_for(ch), variable=var,
                                 command=self.update_solo_channels)
            cb.pack(side="left", padx=6)
            self.channel_vars.append((ch, var))

    def update_solo_channels(self, remember=True):
        active = [ch for ch, var in self.channel_vars if var.get()]
        self.player.set_active_channels(active)
        # Entrance gates are a multiplayer assignment feature. A later Solo
        # play must always use the MIDI's authored timing with no stale gates.
        self.player.set_channel_start_times({})
        if remember:
            self._remember_current_track_profile()

    def play_solo(self, autoplay=False):
        self.update_solo_channels()
        if not self.events:
            self.settings_error_label.configure(
                text="No playable MIDI is ready. Load a file and wait for conversion.",
                text_color="#ff6b6b")
            return
        if not self.player.active_channels:
            self.settings_error_label.configure(
                text="Select at least one Solo Active Channel before playing.",
                text_color="#ff6b6b")
            return

        if autoplay:
            # The user already armed the first track; don't insert the manual
            # start countdown between prepared playlist items.
            delay = 0.0
        else:
            text = self.solo_delay_entry.get().strip().replace(",", ".")
            try:
                delay = float(text)
            except ValueError:
                delay = -1
            if not math.isfinite(delay) or not 0 <= delay <= 10:
                self.settings_error_label.configure(
                    text="Start delay must be between 0 and 10 seconds.",
                    text_color="#ff6b6b")
                return

        simulator = self.player.simulator
        target_focused = bool(simulator and simulator.is_target_focused())
        if target_focused:
            delay = 0.0
        if self.player.play(delay_seconds=delay):
            if (simulator and simulator.focus_guard_enabled
                    and not target_focused):
                target = simulator.target_window_text or "the game"
                self.settings_error_label.configure(
                    text=f"Playback armed — switch to {target}. "
                         "The song will wait instead of skipping notes.",
                    text_color="orange")
                self._play_wait_message_active = True
            else:
                self.settings_error_label.configure(
                    text="", text_color="#ff6b6b")
                self._play_wait_message_active = False

    def _on_autoplay_track_loaded(self):
        if self.network.room_code:
            if not self.network.is_host:
                return
            if not self.autoplay_var.get():
                self.status_label.configure(
                    text=("Next track shared. Review assignments and use "
                          "SYNC PLAY when everyone is Ready."),
                    text_color="orange")
                return
            revision = self.network.room_state.get("revision", 0)
            self._multiplayer_autoplay_pending_revision = revision
            self.status_label.configure(
                text=("Next track prepared. Waiting for everyone to convert "
                      "and become Ready…"),
                text_color="orange")
            self._maybe_start_multiplayer_autoplay()
            return
        self.play_solo(autoplay=True)

    # --- Networking ---

    def copy_room_credential(self):
        room = self.room_entry.get().strip()
        if not room:
            return
        self.clipboard_clear()
        self.clipboard_append(room)
        self.status_label.configure(
            text="Room invitation copied to clipboard.", text_color="green")

    def host_room(self):
        nick = self.nick_entry.get() or "Host"
        self._multiplayer_autoplay_pending_revision = None
        self._multiplayer_auto_ready_revision = None
        self._network_track_profiles.clear()
        # This credential doubles as the room's message-signing secret. Keep it
        # high entropy; only a hash appears in the public MQTT topic.
        room = self.room_entry.get().strip() or secrets.token_urlsafe(24)
        if len(room) < MIN_ROOM_CREDENTIAL_LENGTH:
            self.status_label.configure(
                text=f"Room credential must be at least "
                     f"{MIN_ROOM_CREDENTIAL_LENGTH} characters.",
                text_color="red")
            return
        try:
            self.network.connect()
        except Exception as e:
            self.status_label.configure(text=f"Couldn't connect: {e}", text_color="red")
            return
        try:
            room = self.network.host_room(room, nick)
        except ValueError as exc:
            self.status_label.configure(text=str(exc), text_color="red")
            return
        self.room_entry.delete(0, "end")
        self.room_entry.insert(0, room)
        self.status_label.configure(
            text="Hosting securely. Copy the invitation to your players.",
            text_color="green")
        self.sync_label.configure(text="🕐 Clock: host (reference)", text_color="gray")
        self.sync_play_btn.configure(state="disabled")
        self.sync_stop_btn.configure(state="normal")
        self.disband_btn.configure(state="normal")
        self.host_btn.configure(state="disabled")
        self.join_btn.configure(state="disabled")
        self._share_current_midi()

    def join_room(self):
        nick = self.nick_entry.get() or "Player"
        room = self.room_entry.get().strip()
        if not room.startswith(f"{ROOM_CREDENTIAL_PREFIX}."):
            self.status_label.configure(
                text=(f"Enter the complete {ROOM_CREDENTIAL_PREFIX} room "
                      "invitation from the host."),
                text_color="red")
            return
        try:
            self.network.connect()
        except Exception as e:
            self.status_label.configure(text=f"Couldn't connect: {e}", text_color="red")
            return
        try:
            self.network.join_room(room, nick)
        except ValueError as exc:
            self.status_label.configure(text=str(exc), text_color="red")
            return
        self.status_label.configure(
            text="Joined secure room.", text_color="green")
        self.sync_label.configure(text="🕐 Syncing clock…", text_color="orange")
        self.host_btn.configure(state="disabled")
        self.join_btn.configure(state="disabled")
        self.leave_btn.configure(state="normal")
        self.client_conversion_override_var.set(False)
        self.client_conversion_override_check.configure(state="normal")
        # Ready stays locked until the clock is synced with the host, so nobody
        # can start a song before the timing is aligned.
        self.ready_btn.configure(state="disabled", text="Syncing clock…")
        self.my_ready_status = False
        self._accepted_network_revision = None
        self._loading_network_revision = None
        self._known_room_players = set()
        self._multiplayer_autoplay_pending_revision = None
        self._multiplayer_auto_ready_revision = None
        self._network_track_profiles.clear()

    def leave_room(self):
        self.network.leave_room()
        self.player.stop()
        self._network_conversion_profile = None
        self._reset_multiplayer_ui("You left the room.")

    def disband_room(self):
        self.network.disband_room()
        self.player.stop()
        self._network_conversion_profile = None
        self._reset_multiplayer_ui("Lobby disbanded.")

    def on_network_disband(self):
        # Host closed the room; runs on the network thread -> marshal to UI.
        self.after(0, self._on_host_disbanded)

    def _on_host_disbanded(self):
        self.player.stop()
        self._network_conversion_profile = None
        self._reset_multiplayer_ui("Host closed the room.")

    def kick_player(self, client_id):
        if self.network.is_host:
            self.network.kick_player(client_id)

    def on_network_kicked(self):
        # Runs on the network thread -> marshal to UI.
        self.after(0, self._on_kicked)

    def _on_kicked(self):
        self.player.stop()
        self._network_conversion_profile = None
        self._reset_multiplayer_ui("You were removed from the room by the host.")

    def _reset_multiplayer_ui(self, status="Not Connected"):
        self._network_conversion_profile = None
        self._accepted_network_revision = None
        self._loading_network_revision = None
        self._multiplayer_autoplay_pending_revision = None
        self._multiplayer_auto_ready_revision = None
        self._network_track_profiles.clear()
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
        self.client_conversion_override_var.set(False)
        self.client_conversion_override_check.configure(state="disabled")
        self.my_ready_status = False
        self._known_room_players = set()
        for widget in self.lobby_frame.winfo_children():
            widget.destroy()

    def toggle_ready(self):
        next_ready = not self.my_ready_status
        if next_ready:
            issue = self._client_ready_issue()
            if issue:
                self.my_ready_status = False
                self.status_label.configure(text=issue, text_color="red")
                self._refresh_client_ready_button()
                return
        self.my_ready_status = next_ready
        if next_ready:
            self.ready_btn.configure(text="✅ Ready!", fg_color="green", hover_color="darkgreen")
        else:
            self.ready_btn.configure(text="I'm Ready!", fg_color=["#3a7ebf", "#1f538d"], hover_color=["#325882", "#14375e"])
        if not self.network.send_ready_status(self.my_ready_status):
            self.my_ready_status = False
            self.ready_btn.configure(
                state="disabled", text="Connection error",
                fg_color=["#3a7ebf", "#1f538d"],
                hover_color=["#325882", "#14375e"])
            self.status_label.configure(
                text="Ready status could not be delivered. Reconnecting…",
                text_color="red")

    def _client_ready_issue(self):
        return self.network.client_ready_issue(
            self._accepted_network_revision,
            bool(self.raw_events and self.events),
            self.channels)

    def _refresh_client_ready_button(self):
        if not self.network.room_code or self.network.is_host:
            return
        if self.my_ready_status:
            self.ready_btn.configure(
                state="normal", text="✅ Ready!", fg_color="green",
                hover_color="darkgreen")
            return
        issue = self._client_ready_issue()
        if issue:
            if not self.network.is_synced:
                text = "Syncing clock…"
            elif self._accepted_network_revision != self.network.room_state.get("revision"):
                text = "Waiting for current MIDI…"
            else:
                text = "Waiting for channel assignment…"
            self.ready_btn.configure(
                state="disabled", text=text,
                fg_color=["#3a7ebf", "#1f538d"],
                hover_color=["#325882", "#14375e"])
        else:
            self.ready_btn.configure(
                state="normal", text="I'm Ready!",
                fg_color=["#3a7ebf", "#1f538d"],
                hover_color=["#325882", "#14375e"])

    def _mark_client_unready(self, text="Applying conversion…"):
        if not self.network.room_code or self.network.is_host:
            return
        if self.my_ready_status:
            self.network.send_ready_status(False)
        self.my_ready_status = False
        self._multiplayer_auto_ready_revision = None
        self.ready_btn.configure(
            state="disabled", text=text,
            fg_color=["#3a7ebf", "#1f538d"],
            hover_color=["#325882", "#14375e"])

    def toggle_client_conversion(self):
        if not self.network.room_code or self.network.is_host:
            self.client_conversion_override_var.set(False)
            return

        use_local = self.client_conversion_override_var.get()
        self.player.stop()
        self._mark_client_unready("Applying my settings…" if use_local
                                  else "Applying host settings…")

        if not self.raw_events:
            self.status_label.configure(
                text=("My conversion will be used when the MIDI arrives."
                      if use_local else
                      "The host conversion will be used when the MIDI arrives."),
                text_color="orange" if use_local else "green")
            return
        if not use_local and not self._network_conversion_profile:
            self.status_label.configure(
                text="The host conversion profile has not arrived yet.",
                text_color="red")
            return

        if self.reconvert(broadcast_profile=False) and self.events:
            self._publish_part_manifest()
            self._refresh_client_ready_button()
            self.status_label.configure(
                text=("Using my conversion. The host can now assign from "
                      "my local part list."
                      if use_local else
                      "Using the host conversion settings."),
                text_color="orange" if use_local else "green")
        else:
            self.status_label.configure(
                text="Fix the conversion settings before marking Ready.",
                text_color="red")

    def sync_play(self):
        if not self.network.is_host:
            return
        self._multiplayer_autoplay_pending_revision = None
        issue = self._host_start_issue()
        if issue:
            self.status_label.configure(text=issue, text_color="red")
            return
        self.player.stop()
        if not self.network.send_play(delay_seconds=4.0):
            self.status_label.configure(
                text="Could not send the synchronized Play command.",
                text_color="red")

    def _host_start_issue(self):
        if not self.events:
            return "Load and finish converting a MIDI file first."
        issue = self.network.room_start_issue()
        if issue:
            return issue
        return None
            
    def sync_stop(self):
        self._multiplayer_autoplay_pending_revision = None
        if self.network.is_host:
            if not self.network.send_stop():
                self.status_label.configure(
                    text=("Stopped locally, but the room-wide Stop command "
                          "could not be delivered."),
                    text_color="red")

    # --- Callbacks from NetworkManager (Run in background thread, schedule UI updates) ---

    def on_network_state(self, state):
        self.after(0, self._handle_network_state, state)

    def _handle_network_state(self, state):
        player_ids = {
            p.get("client_id") for p in state.get("players", [])
            if isinstance(p, dict)
        }
        newcomers = player_ids - self._known_room_players - {self.network.client_id}
        self._known_room_players = player_ids
        if (newcomers and self.network.is_host
                and 0 <= self.current_song_idx < len(self.playlist)):
            for client_id in newcomers:
                self._share_current_midi(target_client_id=client_id)
        if (not self.network.is_host
                and self._accepted_network_revision == state.get("revision")
                and self.events):
            me = next((player for player in state.get("players", [])
                       if player.get("client_id") == self.network.client_id),
                      None)
            advertised = (
                sorted(part["channel"]
                       for part in me.get("available_parts", []))
                if me and me.get("parts_revision") == state.get("revision")
                else None)
            using_local = self.client_conversion_override_var.get()
            if (advertised != sorted(self.channels)
                    or bool(me and me.get("local_conversion")) != using_local):
                self._publish_part_manifest()
        self._update_lobby_ui(state)
        self._maybe_auto_ready_multiplayer(state)
        self._maybe_start_multiplayer_autoplay()

    def _maybe_auto_ready_multiplayer(self, state=None):
        """Opt-in client readiness for an authenticated autoplay handoff."""
        if (not self.autoplay_var.get() or not self.network.room_code
                or self.network.is_host or not self.events):
            return False
        state = state or self.network.room_state
        revision = state.get("revision", 0)
        if self._accepted_network_revision != revision:
            return False
        me = next((
            player for player in state.get("players", [])
            if player.get("client_id") == self.network.client_id), None)
        if (not me or me.get("ready")
                or me.get("parts_revision") != revision):
            return False
        advertised = {
            part.get("channel") for part in me.get("available_parts", [])
            if isinstance(part, dict)
        }
        assigned = set(me.get("channels", []))
        if not assigned or not assigned.issubset(advertised):
            return False
        if self._client_ready_issue() is not None:
            return False
        if self._multiplayer_auto_ready_revision == revision:
            return False
        if not self.network.send_ready_status(True):
            return False
        self._multiplayer_auto_ready_revision = revision
        self.my_ready_status = True
        self.ready_btn.configure(
            state="normal", text="✅ Ready!", fg_color="green",
            hover_color="darkgreen")
        self.status_label.configure(
            text="Autoplay prepared this track and marked you Ready.",
            text_color="green")
        return True

    def _maybe_start_multiplayer_autoplay(self):
        """Start the prepared revision once the whole room is safely ready."""
        revision = self._multiplayer_autoplay_pending_revision
        if (revision is None or not self.autoplay_var.get()
                or not self.network.is_host
                or revision != self.network.room_state.get("revision")):
            return False
        if self._host_start_issue() is not None:
            return False
        self._multiplayer_autoplay_pending_revision = None
        self.player.stop()
        if not self.network.send_play(delay_seconds=1.5):
            self._multiplayer_autoplay_pending_revision = revision
            self.status_label.configure(
                text="The autoplay synchronized start could not be delivered.",
                text_color="red")
            return False
        self.status_label.configure(
            text="Everyone is ready — starting the next track together.",
            text_color="green")
        return True

    def on_network_play(self, global_start_time, my_channels,
                        part_start_times=None):
        self.after(
            0, self._trigger_play, global_start_time, my_channels,
            part_start_times or {})

    def on_network_stop(self):
        self.after(0, self.player.stop)

    def on_network_sync(self, rtt, offset):
        self.after(0, self._update_sync_label, rtt, offset)

    def _update_sync_label(self, rtt, offset):
        # Timing uncertainty is roughly half the round-trip delay.
        acc_ms = (rtt * 1000.0) / 2.0
        samples = self.network.sync_sample_count
        if self.network.is_synced:
            color = "green" if acc_ms < 30 else (
                "orange" if acc_ms < 80 else "red")
            self.sync_label.configure(
                text=f"🕐 Synced ±{acc_ms:.0f} ms", text_color=color)
        elif samples < MIN_SYNC_SAMPLES:
            self.sync_label.configure(
                text=f"🕐 Measuring clock {samples}/{MIN_SYNC_SAMPLES}…",
                text_color="orange")
        else:
            self.sync_label.configure(
                text=(f"🕐 Connection too unstable ({rtt * 1000:.0f} ms "
                      f"RTT; need ≤{MAX_READY_SYNC_RTT * 1000:.0f} ms)"),
                text_color="red")
        self._refresh_client_ready_button()

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
            text="🕐 Can't reach host clock (check firewall/TLS port 8883)",
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
                if not self.network.is_host:
                    self.my_ready_status = False
                    self._multiplayer_auto_ready_revision = None
                    self.ready_btn.configure(
                        state="disabled", text="Syncing clock…",
                        fg_color=["#3a7ebf", "#1f538d"],
                        hover_color=["#325882", "#14375e"])
                    self.sync_label.configure(
                        text="🕐 Re-syncing clock…", text_color="orange")
        elif status == "disconnected":
            if self.network.room_code:
                self.status_label.configure(
                    text=f"Disconnected from server ({detail or 'connection lost'}).",
                    text_color="red")

    def on_network_midi(self, filename, data, conversion_profile, revision):
        self.after(
            0, self._save_and_load_midi,
            filename, data, conversion_profile, revision)

    def on_network_conversion_profile(self, conversion_profile, revision):
        self.after(
            0, self._apply_network_conversion_profile,
            conversion_profile, revision)

    def _apply_network_conversion_profile(self, conversion_profile, revision):
        try:
            _settings, _instrument_name = load_conversion_profile(
                conversion_profile)
        except ValueError as exc:
            self.status_label.configure(
                text=f"Rejected host conversion settings: {exc}",
                text_color="red")
            return
        self._network_conversion_profile = conversion_profile
        if self.network.room_code and not self.network.is_host:
            use_local = self.client_conversion_override_var.get()
            self.player.stop()
            self._mark_client_unready(
                "Keeping my settings…" if use_local
                else "Applying host settings…")
            converted = bool(self.raw_events) and (
                bool(self.events) if use_local
                else self.reconvert(broadcast_profile=False))
            if converted and self.events:
                self._accepted_network_revision = revision
                self._publish_part_manifest()
                self._refresh_client_ready_button()
                self.status_label.configure(
                    text=("Host settings updated; my conversion override is "
                          "still active."
                          if use_local else
                          "Host conversion settings updated."),
                    text_color="orange" if use_local else "green")

    def _update_lobby_ui(self, state):
        for widget in self.lobby_frame.winfo_children():
            widget.destroy()
            
        fn = state.get("filename")
        if fn:
            ctk.CTkLabel(self.lobby_frame, text=f"🎵 Shared Song: {fn}", font=ctk.CTkFont(weight="bold")).pack(pady=5)

        self.host_checkbox_vars = {}
        self.host_start_time_vars = {}

        if not self.network.is_host:
            me = next((p for p in state.get("players", [])
                       if p.get("client_id") == self.network.client_id), None)
            if me is not None:
                ready = bool(me.get("ready", False))
                self.my_ready_status = ready
                if ready:
                    self.ready_btn.configure(
                        text="✅ Ready!", fg_color="green",
                        hover_color="darkgreen")
                else:
                    self._refresh_client_ready_button()

        for p in state["players"]:
            part_options = self._player_part_options(p, state)
            player_labels = dict(part_options)
            frame = ctk.CTkFrame(self.lobby_frame)
            frame.pack(fill="x", pady=5, padx=5)
            
            is_me = p['client_id'] == self.network.client_id
            name = f"{p['nickname']} (Me)" if is_me else p['nickname']
            
            # Status dot
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
                
                if not part_options:
                    message = (
                        "This conversion has no playable parts."
                        if p.get("parts_revision") == state.get("revision")
                        else "Waiting for this player's converted part listâ€¦")
                    ctk.CTkLabel(
                        ch_frame, text=message,
                        text_color="gray").pack(side="left")
                else:
                    self.host_checkbox_vars[p['client_id']] = {}
                    self.host_start_time_vars[p['client_id']] = {}
                    start_times = self.network.player_part_start_times(p) or {}
                    for ch, part_label in part_options:
                        var = ctk.BooleanVar(value=(ch in p["channels"]))
                        self.host_checkbox_vars[p['client_id']][ch] = var
                        
                        def on_toggle(cid=p['client_id']):
                            chs = [c for c, v in self.host_checkbox_vars[cid].items() if v.get()]
                            self.network.assign_channels(cid, chs)
                            
                        part_frame = ctk.CTkFrame(
                            ch_frame, fg_color="transparent")
                        part_frame.pack(side="left", padx=10, pady=3)
                        cb = ctk.CTkCheckBox(
                            part_frame,
                            text=(f"{part_label} (local conversion)"
                                  if p.get("local_conversion")
                                  else part_label),
                            variable=var, command=on_toggle)
                        cb.pack(side="left", pady=5)

                        start_var = ctk.StringVar(
                            value=f"{start_times.get(ch, 0.0):g}")
                        self.host_start_time_vars[p['client_id']][ch] = start_var
                        ctk.CTkLabel(
                            part_frame, text="Start at", text_color="gray"
                        ).pack(side="left", padx=(8, 3))
                        start_entry = ctk.CTkEntry(
                            part_frame, width=58, textvariable=start_var,
                            placeholder_text="0")
                        start_entry.pack(side="left")
                        ctk.CTkLabel(
                            part_frame, text="s", text_color="gray"
                        ).pack(side="left", padx=(3, 0))
                        if ch not in p["channels"]:
                            start_entry.configure(state="disabled")

                        def commit_start(_event=None, cid=p['client_id'],
                                         channel=ch, value_var=start_var):
                            self._commit_part_start_time(
                                cid, channel, value_var)

                        start_entry.bind("<Return>", commit_start)
                        start_entry.bind("<FocusOut>", commit_start)
            else:
                start_times = self.network.player_part_start_times(p) or {}
                assigned_text = (
                    ", ".join(
                        player_labels.get(
                            channel, f"MIDI Ch. {channel + 1}")
                        + (f" (starts at {start_times[channel]:g}s)"
                           if start_times.get(channel, 0.0) > 0 else "")
                        for channel in p['channels'])
                    if p['channels'] else "None")
                lbl2 = ctk.CTkLabel(
                    frame, text=f"Assigned Parts: {assigned_text}",
                    text_color="cyan")
                lbl2.pack(side="left", padx=10, pady=10)
                
        if self.network.is_host:
            issue = self._host_start_issue()
            if issue is None:
                self.sync_play_btn.configure(state="normal", text="SYNC PLAY (All Ready!)")
            else:
                self.sync_play_btn.configure(state="disabled", text="SYNC PLAY (Waiting for Ready...)")

    def _commit_part_start_time(self, client_id, channel, value_var):
        text = value_var.get().strip().replace(",", ".")
        try:
            start_time = float(text)
        except ValueError:
            start_time = -1.0
        if (not math.isfinite(start_time) or start_time < 0.0
                or start_time > MAX_PART_START_SECONDS):
            player = next((
                item for item in self.network.room_state.get("players", [])
                if item.get("client_id") == client_id), {})
            previous = (
                self.network.player_part_start_times(player) or {}).get(
                    channel, 0.0)
            value_var.set(f"{previous:g}")
            self.status_label.configure(
                text=("Part entrance must be a number from 0 to "
                      f"{MAX_PART_START_SECONDS:g} seconds."),
                text_color="red")
            return False
        if not self.network.assign_part_start_time(
                client_id, channel, start_time):
            self.status_label.configure(
                text="Select the part before setting its entrance time.",
                text_color="red")
            return False
        value_var.set(f"{start_time:g}")
        self.status_label.configure(
            text=(f"Part entrance set to {start_time:g}s."
                  if start_time else "Part will play from the beginning."),
            text_color="green")
        return True

    def _trigger_play(self, global_start_time, my_channels,
                      part_start_times=None):
        if not self.events:
            self.status_label.configure(
                text="Play was rejected because no converted MIDI is loaded.",
                text_color="red")
            return
        if not my_channels:
            self.status_label.configure(
                text="Play was rejected because no channels are assigned.",
                text_color="red")
            return
        if (not self.network.is_host
                and self._accepted_network_revision
                != self.network.room_state.get("revision")):
            self.status_label.configure(
                text="Play was rejected because the current host MIDI is not ready.",
                text_color="red")
            return
        if not set(my_channels).issubset(set(self.channels)):
            self.status_label.configure(
                text="Play was rejected because the assigned channels are stale.",
                text_color="red")
            return
        # Do playback cleanup FIRST. stop() may join a thread and release keys,
        # and that duration varies per machine — computing the start delay
        # afterwards keeps that variable latency out of the start moment.
        self.player.stop()
        self.player.set_active_channels(my_channels)
        try:
            self.player.set_channel_start_times(part_start_times or {})
        except (TypeError, ValueError):
            self.status_label.configure(
                text="Play was rejected because a part entrance time is invalid.",
                text_color="red")
            return

        delay = global_start_time - self.network.get_global_time()
        if delay < -0.25:
            self.status_label.configure(
                text=("Synchronized start was missed by more than 250 ms. "
                      "Ask the host to start again."),
                text_color="red")
            return
        # Manual calibration nudge (ms): +later / -earlier.
        nudge = min(max(self._get_float(self.nudge_entry, 0.0), -500), 500) / 1000.0
        delay += nudge
        if delay < 0:
            delay = 0.0  # start immediately if the target moment already passed
        print(f"Network Play Triggered! Delaying start by {delay:.3f}s "
              f"(nudge {nudge*1000:.0f}ms) for Channels {my_channels}, "
              f"entrances {part_start_times or {}}")
        if not self.player.play(delay_seconds=delay, strict_timing=True):
            self.status_label.configure(
                text="Synchronized playback could not be armed.",
                text_color="red")

    def _save_and_load_midi(self, filename, data, conversion_profile, revision):
        filename = os.path.basename(str(filename).replace("\\", "/"))
        ext = os.path.splitext(filename)[1].lower()
        if ext not in {".mid", ".midi"} or not filename:
            self.status_label.configure(
                text="Rejected a received non-MIDI file.", text_color="red")
            return
        if len(data) > MAX_MIDI_BYTES:
            self.status_label.configure(
                text="Rejected an oversized received MIDI.", text_color="red")
            return
        try:
            _settings, _instrument_name = load_conversion_profile(
                conversion_profile)
        except ValueError as exc:
            self.status_label.configure(
                text=f"Rejected host conversion settings: {exc}",
                text_color="red")
            return
        self.player.stop()
        self._accepted_network_revision = None
        self._loading_network_revision = revision
        self._mark_client_unready("Waiting for current MIDI…")
        auto_accept = bool(
            self.autoplay_var.get() and self.network.room_code
            and not self.network.is_host)
        if (not auto_accept and not messagebox.askyesno(
                "Accept shared MIDI?",
                f"The authenticated host shared “{filename}” "
                f"({len(data) / 1024:.1f} KiB).\n\nLoad it now?")):
            self.status_label.configure(
                text="Shared MIDI declined.", text_color="gray")
            self._loading_network_revision = None
            self._refresh_client_ready_button()
            return
        if auto_accept:
            self.status_label.configure(
                text=f"Autoplay accepted authenticated host track “{filename}”.",
                text_color="orange")
        self._network_conversion_profile = conversion_profile
        temp_file = tempfile.NamedTemporaryFile(
            mode="wb", prefix="bpsr_received_", suffix=ext, delete=False)
        file_path = temp_file.name
        with temp_file as f:
            f.write(data)
        self._received_temp_files.add(file_path)

        # Multiplayer autoplay can run for a long set.  A client only needs the
        # current received file, so remove superseded temporary copies instead
        # of accumulating the whole performance on disk.
        for old_path in list(self._received_temp_files):
            if old_path == file_path:
                continue
            try:
                os.remove(old_path)
            except FileNotFoundError:
                self._received_temp_files.discard(old_path)
            except OSError:
                continue
            else:
                self._received_temp_files.discard(old_path)

        song_id = self.network.room_state.get("song_id")
        saved_local_profile = (
            self._network_track_profiles.get(song_id)
            if (song_id and self.per_track_settings_var.get()
                and self.client_conversion_override_var.get())
            else None)
        if saved_local_profile:
            try:
                self._apply_conversion_profile_to_ui(saved_local_profile)
                self._current_conversion_profile = deepcopy(saved_local_profile)
            except ValueError:
                self._network_track_profiles.pop(song_id, None)
                saved_local_profile = None
        
        # In client mode, we just override current song view (or add to playlist)
        # We will clear playlist and set this as the only song for the client
        self.playlist = [{
            "name": f"{filename} (Received)",
            "filename": filename,
            "path": file_path,
            "temporary": True,
            **({"conversion_profile": deepcopy(saved_local_profile)}
               if saved_local_profile else {}),
        }]
        self.current_song_idx = 0
        self._update_playlist_ui()
        self.song_var.set(self.playlist[0]["name"])
        
        self._parse_and_load(file_path, network_revision=revision)

    def destroy(self):
        self._closing = True
        self._parse_generation += 1
        self.save_prefs()  # catch any change that never triggered a reconvert
        if self.hotkeys:
            self.hotkeys.stop()
        self.player.stop()
        self.live_midi.stop_listening()
        self.network.disconnect()
        for path in self._received_temp_files:
            try:
                os.remove(path)
            except OSError:
                pass
        super().destroy()

if __name__ == "__main__":
    app = App()
    app.mainloop()
