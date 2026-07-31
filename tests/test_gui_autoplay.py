from types import SimpleNamespace

from gui import App


class _Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class _Widget:
    def __init__(self):
        self.last = {}

    def configure(self, **kwargs):
        self.last = kwargs


class _Player:
    def __init__(self):
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1


class _Network:
    def __init__(self, *, is_host, state):
        self.room_code = "room"
        self.is_host = is_host
        self.client_id = "me"
        self.room_state = state
        self.ready_calls = []
        self.play_delays = []

    def send_ready_status(self, ready):
        self.ready_calls.append(ready)
        return True

    def send_play(self, delay_seconds):
        self.play_delays.append(delay_seconds)
        return True


def _client_app(state):
    network = _Network(is_host=False, state=state)
    return SimpleNamespace(
        autoplay_var=_Var(True),
        network=network,
        events=[{"type": "note_on"}],
        _accepted_network_revision=state["revision"],
        _multiplayer_auto_ready_revision=None,
        _client_ready_issue=lambda: None,
        my_ready_status=False,
        ready_btn=_Widget(),
        status_label=_Widget(),
    )


def test_multiplayer_autoplay_auto_readies_only_after_manifest_and_assignment():
    state = {
        "revision": 4,
        "players": [{
            "client_id": "me",
            "ready": False,
            "channels": [2],
            "parts_revision": 4,
            "available_parts": [{"channel": 2, "label": "Melody"}],
        }],
    }
    app = _client_app(state)

    assert App._maybe_auto_ready_multiplayer(app, state)
    assert app.network.ready_calls == [True]
    assert app.my_ready_status is True
    assert app._multiplayer_auto_ready_revision == 4


def test_multiplayer_autoplay_does_not_ready_a_stale_assignment():
    state = {
        "revision": 5,
        "players": [{
            "client_id": "me",
            "ready": False,
            "channels": [9],
            "parts_revision": 5,
            "available_parts": [{"channel": 2, "label": "Melody"}],
        }],
    }
    app = _client_app(state)

    assert not App._maybe_auto_ready_multiplayer(app, state)
    assert app.network.ready_calls == []


def test_per_track_profile_keeps_conversion_and_solo_part_selection():
    profile = {"version": 2, "instrument": "Piano", "settings": {}}
    song = {}
    app = SimpleNamespace(
        per_track_settings_var=_Var(True),
        playlist=[song],
        current_song_idx=0,
        _current_conversion_profile=profile,
        channel_vars=[(1, _Var(True)), (2, _Var(False))],
        network=SimpleNamespace(room_code=None, is_host=False),
    )
    app._current_song = lambda: App._current_song(app)

    App._remember_current_track_profile(app)

    assert song["conversion_profile"] == profile
    assert song["conversion_profile"] is not profile
    assert song["active_channels"] == [1]


def test_host_autoplay_starts_once_when_current_revision_is_ready():
    state = {"revision": 7, "players": []}
    network = _Network(is_host=True, state=state)
    player = _Player()
    app = SimpleNamespace(
        _multiplayer_autoplay_pending_revision=7,
        autoplay_var=_Var(True),
        network=network,
        _host_start_issue=lambda: None,
        player=player,
        status_label=_Widget(),
    )

    assert App._maybe_start_multiplayer_autoplay(app)
    assert app._multiplayer_autoplay_pending_revision is None
    assert player.stop_calls == 1
    assert network.play_delays == [1.5]
    assert not App._maybe_start_multiplayer_autoplay(app)
