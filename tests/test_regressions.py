import base64
import importlib
import hashlib
import json
import os
import sys
import threading
import time
import types

import mido
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arranger import (
    ConversionSettings,
    apply_melody_lock,
    assign_auto_parts,
    convert,
    thin_notes,
)
from midi_parser import parse_midi_full
from player import MidiPlayer


class MockSimulator:
    def __init__(self):
        self.log = []
        self.shift_delay_ms = 0
        self.shift_hold_ms = 0
        self.retrigger_gap_ms = 0
        self.key_offset = 0

    def press_note(self, note):
        self.log.append(("press", note, threading.get_ident()))

    def release_note(self, note):
        self.log.append(("release", note, threading.get_ident()))

    def release_all(self):
        self.log.append(("all", threading.get_ident()))

    def set_sustain(self, value):
        self.log.append(("sustain", value))

    def set_octave_shift(self, value):
        self.log.append(("zone", value))


def test_pause_resume_never_runs_two_workers():
    player = MidiPlayer()
    player.simulator = MockSimulator()
    player.load_events([
        {"time": 0.08, "type": "note_on", "note": 60, "channel": 0},
        {"time": 0.16, "type": "note_off", "note": 60, "channel": 0},
    ], [0])

    player.play()
    time.sleep(0.01)
    old_thread = player.thread
    player.pause()
    assert not old_thread.is_alive()
    player.play()
    time.sleep(0.2)

    assert [x[:2] for x in player.simulator.log].count(("press", 60)) == 1
    player.stop()


def test_stop_interrupts_long_countdown():
    player = MidiPlayer()
    player.simulator = MockSimulator()
    player.load_events([
        {"time": 0.0, "type": "note_on", "note": 60, "channel": 0},
    ], [0])
    player.play(delay_seconds=4.0)
    worker = player.thread
    started = time.perf_counter()
    player.stop()

    assert time.perf_counter() - started < 0.5
    assert not worker.is_alive()


def test_seek_keeps_boundary_event_and_sync_countdown():
    player = MidiPlayer()
    player.simulator = MockSimulator()
    player.load_events([
        {"time": 0.0, "type": "note_on", "note": 60, "channel": 0},
        {"time": 1.0, "type": "note_off", "note": 60, "channel": 0},
    ], [0])

    player.play(delay_seconds=0.5)
    player.seek(0.0)
    assert player.current_event_idx == 0
    assert player.is_syncing
    player.stop()


def test_seek_replays_sustain_only_from_active_channels():
    player = MidiPlayer()
    player.simulator = MockSimulator()
    player.load_events([
        {"time": 0.1, "type": "sustain", "value": False, "channel": 0},
        {"time": 0.2, "type": "sustain", "value": True, "channel": 1},
        {"time": 1.0, "type": "note_on", "note": 60, "channel": 0},
    ], [0])
    player.seek(0.5)
    assert ("sustain", False) in player.simulator.log
    assert ("sustain", True) not in player.simulator.log
    player.stop()


def test_transpose_and_channel_changes_release_the_pressed_note():
    player = MidiPlayer()
    player.simulator = MockSimulator()
    player.load_events([
        {"time": 0.0, "type": "note_on", "note": 60, "channel": 0},
        {"time": 0.2, "type": "note_off", "note": 60, "channel": 0},
    ], [0])
    player.play()
    time.sleep(0.03)
    player.transpose = 12
    time.sleep(0.22)
    assert not any(x[:2] == ("release", 72) for x in player.simulator.log)
    player.stop()

    player = MidiPlayer()
    player.simulator = MockSimulator()
    player.load_events([
        {"time": 0.0, "type": "note_on", "note": 60, "channel": 0},
        {"time": 0.3, "type": "note_off", "note": 60, "channel": 0},
    ], [0])
    player.play()
    time.sleep(0.03)
    player.set_active_channels([])
    assert any(x[:2] == ("release", 60) for x in player.simulator.log)
    player.stop()


def _note(start, end, pitch):
    return {
        "start": start, "end": end, "note": pitch,
        "channel": 0, "velocity": 80,
    }


def test_thinning_uses_retrigger_rate_not_legato_silence():
    notes = [_note(i * 2.0, i * 2.0 + 2.0, 60) for i in range(4)]
    assert len(thin_notes(notes, 0.03, 0.02)) == 4

    rapid = [_note(i * 0.015, i * 0.015 + 0.01, 60) for i in range(4)]
    assert len(thin_notes(rapid, 0.03, 0.005)) == 1


def test_melody_lock_selects_best_valid_zone_and_ends_incompatible_hold():
    chord = [
        _note(0.0, 0.5, 84),
        _note(1.0, 1.5, 48),
        _note(1.0, 1.5, 52),
        _note(1.0, 1.5, 55),
        _note(1.0, 1.5, 60),
    ]
    _zones, kept = apply_melody_lock(chord, 0.03, "drop")
    assert sorted(n["note"] for n in kept) == [48, 52, 55, 60, 84]

    held_low = _note(0.0, 2.0, 40)
    forced_high = _note(1.0, 1.5, 90)
    apply_melody_lock([held_low, forced_high], 0.03, "drop")
    assert held_low["end"] == 1.0


def test_auto_split_groups_humanized_chord_window():
    notes = [_note(0.000, 1.0, 60), _note(0.003, 1.0, 72)]
    assign_auto_parts(notes, [], 2, chord_window=0.03)
    assert [(n["note"], n["channel"]) for n in notes] == [(60, 1), (72, 0)]


def test_disjoint_range_clamps_without_reversing_melody():
    events = []
    for i, pitch in enumerate((60, 62, 64, 65, 67, 69, 71, 72)):
        events.extend([
            {"time": i, "type": "note_on", "note": pitch,
             "velocity": 80, "channel": 0},
            {"time": i + 0.5, "type": "note_off", "note": pitch, "channel": 0},
        ])
    out = convert(
        events,
        ConversionSettings(
            proportional_remap=True, range_low=0, range_high=10,
            reach_low=36, reach_high=95),
    )
    pitches = [e["note"] for e in out if e["type"] == "note_on"]
    assert pitches == sorted(pitches)
    assert all(36 <= pitch <= 95 for pitch in pitches)


def test_parser_keeps_close_note_offs(tmp_path):
    # Another legacy test temporarily replaces mido.MidiFile on the shared
    # module object; reload the installed package for this real parser probe.
    importlib.reload(mido)
    midi = mido.MidiFile(ticks_per_beat=1000)
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=1_000_000, time=0))
    track.append(mido.Message("note_on", note=60, velocity=80, time=0))
    track.append(mido.Message("note_on", note=60, velocity=80, time=1000))
    track.append(mido.Message("note_off", note=60, velocity=0, time=250))
    track.append(mido.Message("note_off", note=60, velocity=0, time=1))
    path = tmp_path / "overlap.mid"
    midi.save(path)

    events = parse_midi_full(path)["events"]
    assert len([e for e in events if e["type"] == "note_off"]) == 2


def _network_module():
    for name in ("paho", "paho.mqtt", "paho.mqtt.client"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["paho.mqtt"].client = sys.modules["paho.mqtt.client"]
    client_module = sys.modules["paho.mqtt.client"]
    client_module.Client = getattr(client_module, "Client", object)
    client_module.CallbackAPIVersion = getattr(
        client_module, "CallbackAPIVersion",
        types.SimpleNamespace(VERSION2=2))
    import network_sync
    return network_sync


class FakeNetworkClient:
    def __init__(self):
        self.unsubscribed = []

    def unsubscribe(self, topic):
        self.unsubscribed.append(topic)


def _client_manager():
    network_sync = _network_module()
    manager = network_sync.NetworkManager.__new__(network_sync.NetworkManager)
    client_private = Ed25519PrivateKey.generate()
    client_public = client_private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    manager._identity_private = client_private
    manager._identity_public_raw = client_public
    manager._identity_public_text = manager._b64url(client_public)
    manager.client_id = hashlib.sha256(client_public).hexdigest()[:32]
    manager.client = FakeNetworkClient()
    host_private = Ed25519PrivateKey.generate()
    host_public = host_private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    credential = (
        "bpsr3.correct-horse-battery-staple."
        + manager._b64url(host_public))
    manager._configure_room(credential)
    manager.is_host = False
    manager.host_id = hashlib.sha256(host_public).hexdigest()[:32]
    manager._test_host_private = host_private
    manager._test_host_public = host_public
    manager.nickname = "Client"
    manager.room_state = {
        "players": [{
            "client_id": manager.client_id,
            "nickname": "Client",
            "channels": [0],
            "connected": True,
            "last_seen": time.time(),
            "ready": True,
        }],
        "filename": "song.mid",
        "song_id": hashlib.sha256(b"MThd").hexdigest(),
        "revision": 1,
    }
    manager.host_offset = 0.0
    manager.sync_rtt = None
    manager.is_synced = False
    manager._sync_samples = []
    manager._room_joined_at = time.time()
    manager._sync_stall_reported = False
    manager.on_midi_received = None
    manager.on_state_change = None
    manager.on_play_cmd = None
    manager.on_stop_cmd = None
    manager.on_sync_update = None
    manager.on_disband = None
    manager.on_kicked = None
    manager.on_conversion_profile_received = None
    manager.on_connection_status = None
    manager.running = True
    return network_sync, manager


def _signed_message(manager, payload):
    payload = dict(payload)
    payload["_sender"] = manager.host_id
    payload["_msg_id"] = os.urandom(16).hex()
    payload["_sender_pub"] = manager._b64url(manager._test_host_public)
    payload["_proto"] = 3
    payload["_sig"] = manager._sign(payload)
    unsigned = json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")
    payload["_sender_sig"] = manager._b64url(
        manager._test_host_private.sign(unsigned))
    return types.SimpleNamespace(
        topic=manager._room_topic(),
        payload=json.dumps(payload).encode("utf-8"))


def test_network_rejects_unsigned_file_and_sanitizes_signed_filename(monkeypatch):
    network_sync, manager = _client_manager()
    monkeypatch.setattr(network_sync, "_log", lambda _message: None)
    received = []
    manager.on_midi_received = (
        lambda name, data, profile, revision:
        received.append((name, data, profile, revision)))
    encoded = base64.b64encode(b"MThd").decode("ascii")

    unsigned = types.SimpleNamespace(
        topic=manager._room_topic(),
        payload=json.dumps({
            "type": "midi_file", "_sender": "host",
            "filename": "../../evil.mid", "data": encoded,
        }).encode("utf-8"))
    manager._on_message(None, None, unsigned)
    assert received == []

    manager._on_message(None, None, _signed_message(manager, {
        "type": "midi_file",
        "filename": "../../evil.mid",
        "data": encoded,
        "sha256": hashlib.sha256(b"MThd").hexdigest(),
        "revision": 1,
    }))
    assert received == [("evil.mid", b"MThd", None, 1)]

    replay = _signed_message(manager, {
        "type": "midi_file",
        "filename": "song.mid",
        "data": encoded,
        "sha256": hashlib.sha256(b"MThd").hexdigest(),
        "revision": 1,
    })
    manager._on_message(None, None, replay)
    manager._on_message(None, None, replay)
    assert received.count(("song.mid", b"MThd", None, 1)) == 1


def test_disconnect_invalidates_client_clock_sync():
    _network_sync, manager = _client_manager()
    manager.is_synced = True
    manager.sync_rtt = 0.012
    manager.host_offset = 1.5
    manager._sync_samples = [(time.time(), 0.012, 1.5)]

    manager._on_disconnect(None, None, None, "network lost")

    assert not manager.is_synced
    assert manager.sync_rtt is None
    assert manager.host_offset == 0.0
    assert manager._sync_samples == []


def test_host_profile_change_clears_remote_ready_status():
    network_sync = _network_module()
    manager = network_sync.NetworkManager.__new__(network_sync.NetworkManager)
    manager.is_host = True
    manager.client_id = "host"
    manager.room_state = {
        "players": [
            {"client_id": "host", "channels": [0], "ready": True},
            {"client_id": "client", "channels": [1], "ready": True},
        ],
        "filename": "song.mid",
        "song_id": "a" * 64,
        "revision": 1,
    }
    published = []
    broadcasts = []
    manager._publish = lambda payload: published.append(payload)
    manager._broadcast_state = lambda: broadcasts.append(True)

    manager.share_conversion_profile({"version": 1})

    assert manager.room_state["players"][0]["ready"] is True
    assert manager.room_state["players"][1]["ready"] is False
    assert broadcasts == [True]
    assert published == [{
        "type": "conversion_profile",
        "profile": {"version": 1},
        "revision": 2,
    }]


def test_remote_disband_unsubscribes_before_reset():
    _network_sync, manager = _client_manager()
    old_topic = manager._room_topic()
    manager._on_message(
        None, None, _signed_message(manager, {"type": "disband"}))
    assert manager.client.unsubscribed == [old_topic]
    assert manager.room_code is None


def test_room_member_cannot_forge_host_command():
    network_sync, manager = _client_manager()
    received = []
    manager.on_stop_cmd = lambda: received.append("stop")
    payload = {
        "type": "stop",
        "_sender": manager.client_id,
        "_msg_id": os.urandom(16).hex(),
        "_sender_pub": manager._identity_public_text,
        "_proto": network_sync.PROTOCOL_VERSION,
    }
    payload["_sig"] = manager._sign(payload)
    unsigned = json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")
    payload["_sender_sig"] = manager._b64url(
        manager._identity_private.sign(unsigned))
    msg = types.SimpleNamespace(
        topic=manager._room_topic(),
        payload=json.dumps(payload).encode("utf-8"))

    manager._on_message(None, None, msg)
    assert received == []


def test_non_finite_network_start_is_rejected():
    _network_sync, manager = _client_manager()
    manager.is_synced = True
    played = []
    manager.on_play_cmd = lambda *args: played.append(args)
    message = _signed_message(manager, {
        "type": "play",
        "start_time": float("nan"),
        "revision": 1,
    })
    manager._on_message(None, None, message)
    assert played == []


def test_player_refuses_silent_play_with_no_active_channels():
    player = MidiPlayer()
    player.simulator = MockSimulator()
    player.load_events([{
        "time": 0.0, "type": "note_on", "note": 60, "channel": 0,
    }], [])

    assert player.play() is False
    assert not player.is_playing
    assert not any(entry[0] == "press" for entry in player.simulator.log)


def test_channel_assignment_invalidates_remote_ready():
    network_sync = _network_module()
    manager = network_sync.NetworkManager.__new__(network_sync.NetworkManager)
    manager.is_host = True
    manager.client_id = "host"
    manager.room_state = {
        "players": [
            {"client_id": "host", "channels": [0], "ready": True},
            {"client_id": "client", "channels": [1], "ready": True},
        ],
        "filename": "song.mid", "song_id": "a" * 64, "revision": 1,
    }
    broadcasts = []
    manager._broadcast_state = lambda: broadcasts.append(True)

    assert manager.assign_channels("client", [2]) is True
    assert manager.room_state["players"][1]["channels"] == [2]
    assert manager.room_state["players"][1]["ready"] is False
    assert broadcasts == [True]


def test_play_requires_sync_ready_and_assigned_channels():
    _network_sync, manager = _client_manager()
    calls = []
    manager.on_play_cmd = lambda start, channels: calls.append(channels)

    def send_play():
        manager._on_message(None, None, _signed_message(manager, {
            "type": "play",
            "start_time": manager.get_global_time() + 1.0,
            "revision": 1,
        }))

    manager.is_synced = False
    send_play()
    assert calls == []

    manager.is_synced = True
    manager.room_state["players"][0]["ready"] = False
    send_play()
    assert calls == []

    manager.room_state["players"][0]["ready"] = True
    manager.room_state["players"][0]["channels"] = []
    send_play()
    assert calls == []

    manager.room_state["players"][0]["channels"] = [0]
    send_play()
    assert calls == [[0]]


def test_clock_needs_five_good_samples_before_ready():
    _network_sync, manager = _client_manager()
    for sample in range(4):
        t0 = time.time() - 0.05
        manager._on_message(None, None, _signed_message(manager, {
            "type": "sync_pong", "to": manager.client_id,
            "id": sample, "t0": t0, "t1": t0 + 0.02,
            "t2": t0 + 0.021,
        }))
        assert manager.is_synced is False

    t0 = time.time() - 0.05
    manager._on_message(None, None, _signed_message(manager, {
        "type": "sync_pong", "to": manager.client_id,
        "id": 4, "t0": t0, "t1": t0 + 0.02,
        "t2": t0 + 0.021,
    }))
    assert manager.sync_sample_count == 5
    assert manager.is_synced is True


def test_host_start_rejects_disconnected_unready_or_unassigned_players():
    network_sync = _network_module()
    manager = network_sync.NetworkManager.__new__(network_sync.NetworkManager)
    manager.is_host = True
    manager.room_state = {
        "players": [{
            "client_id": "host", "nickname": "Host", "channels": [],
            "connected": True, "ready": False,
        }],
        "filename": "song.mid", "song_id": "a" * 64, "revision": 1,
    }

    assert "Assign" in manager.room_start_issue()
    manager.room_state["players"][0]["channels"] = [0]
    assert "not Ready" in manager.room_start_issue()
    manager.room_state["players"][0]["ready"] = True
    manager.room_state["players"][0]["connected"] = False
    assert "disconnected" in manager.room_start_issue()
    manager.room_state["players"][0]["connected"] = True
    assert manager.room_start_issue() is None


def test_host_play_starts_locally_without_waiting_for_broker_echo():
    network_sync = _network_module()
    manager = network_sync.NetworkManager.__new__(network_sync.NetworkManager)
    manager.is_host = True
    manager.client_id = "host"
    manager.room_state = {
        "players": [{
            "client_id": "host", "nickname": "Host", "channels": [0],
            "connected": True, "ready": True,
        }],
        "filename": "song.mid", "song_id": "a" * 64, "revision": 1,
    }
    published = []
    played = []
    manager._publish = lambda payload: published.append(payload) or True
    manager.on_play_cmd = lambda start, channels: played.append(channels)
    manager.get_global_time = lambda: 100.0

    assert manager.send_play(2.0) is True
    assert published == [{"type": "play", "start_time": 102.0,
                          "revision": 1}]
    assert played == [[0]]


def test_client_ready_requires_the_current_revision_and_valid_assignment():
    _network_sync, manager = _client_manager()
    manager.is_synced = True

    issue = manager.client_ready_issue(
        accepted_revision=0, has_playable_events=True,
        available_channels=[0])
    assert "current MIDI" in issue

    manager.room_state["players"][0]["channels"] = []
    issue = manager.client_ready_issue(
        accepted_revision=1, has_playable_events=True,
        available_channels=[0])
    assert "assign" in issue

    manager.room_state["players"][0]["channels"] = [1]
    issue = manager.client_ready_issue(
        accepted_revision=1, has_playable_events=True,
        available_channels=[0])
    assert "does not exist" in issue

    manager.room_state["players"][0]["channels"] = [0]
    assert manager.client_ready_issue(
        accepted_revision=1, has_playable_events=True,
        available_channels=[0]) is None


def test_host_assigns_each_client_from_their_own_conversion_parts():
    network_sync = _network_module()
    manager = network_sync.NetworkManager.__new__(network_sync.NetworkManager)
    manager.is_host = True
    manager.client_id = "host"
    manager.room_state = {
        "players": [
            {
                "client_id": "host", "nickname": "Drummer",
                "channels": [0],
                "available_parts": [{"channel": 0, "label": "Drum"}],
                "parts_revision": 1, "local_conversion": False,
                "connected": True, "ready": True,
            },
            {
                "client_id": "client", "nickname": "Pianist",
                "channels": [0], "available_parts": [],
                "parts_revision": 0, "local_conversion": False,
                "connected": True, "ready": True,
            },
        ],
        "filename": "song.mid", "song_id": "a" * 64, "revision": 1,
    }
    broadcasts = []
    manager._broadcast_state = lambda: broadcasts.append(True)
    local_parts = [
        {"channel": channel, "label": f"MIDI Ch. {channel + 1}"}
        for channel in (1, 2, 9, 14)]

    assert manager._store_part_manifest(
        "client", local_parts, True, revision=1)
    client = manager.room_state["players"][1]
    assert client["channels"] == []
    assert client["ready"] is False
    assert client["local_conversion"] is True
    assert manager.assign_channels("client", [0]) is False
    assert manager.assign_channels("client", [14]) is True
    client["ready"] = True

    assert manager.room_start_issue() is None
    assert broadcasts == [True, True]


def test_part_manifest_is_revision_bound_validated_and_state_safe():
    network_sync, client_manager = _client_manager()
    parts = [
        {"channel": 14, "label": "  Lead\x00\n  "},
        {"channel": 1, "label": "Piano"},
    ]
    cleaned = client_manager._validate_parts(parts)
    assert cleaned == [
        {"channel": 1, "label": "Piano"},
        {"channel": 14, "label": "Lead"},
    ]
    assert client_manager._validate_parts([
        {"channel": 1, "label": "One"},
        {"channel": 1, "label": "Duplicate"},
    ]) is None

    state = dict(client_manager.room_state)
    state["players"] = [dict(state["players"][0],
        available_parts=cleaned, parts_revision=1,
        local_conversion=True)]
    validated = client_manager._validate_state(state)
    assert validated["players"][0]["available_parts"] == cleaned
    assert validated["players"][0]["local_conversion"] is True

    host = network_sync.NetworkManager.__new__(network_sync.NetworkManager)
    host.is_host = True
    host.client_id = "host"
    host.room_code = "room"
    host.room_state = {
        "players": [{
            "client_id": "host", "channels": [], "ready": False,
            "available_parts": [], "parts_revision": 0,
            "local_conversion": False,
        }],
        "song_id": "a" * 64, "revision": 2,
    }
    host._broadcast_state = lambda: None
    assert not host.send_part_manifest(cleaned, revision=1)
