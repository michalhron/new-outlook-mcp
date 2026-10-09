from __future__ import annotations

import json
import os
import plistlib
from datetime import datetime, timedelta, timezone

from new_outlook_mcp import cli, launchd, tools, watch
from new_outlook_mcp.watch import RunOutcome, Watcher

OK = RunOutcome("ok")
TORN = RunOutcome("warning", "3 of 10 HxStore blocks failed CRC/LZ4 even after a second copy (Outlook was writing)")


class Fake:
    """Injectable clock, sleep and runner."""

    def __init__(self, outcomes=None):
        self.t = 1000.0
        self.calls: list[float] = []
        self.outcomes = list(outcomes or [])

    def clock(self):
        return self.t

    def wall(self):
        return 1_700_000_000 + self.t

    def sleep(self, s):
        self.t += s

    def runner(self):
        self.calls.append(self.t)
        return self.outcomes.pop(0) if self.outcomes else OK


def make(tmp_path, fake, **kw):
    kw.setdefault("notifier", None)
    return Watcher([tmp_path / "HxStore.hxd"], fake.runner, clock=fake.clock, wall=fake.wall, sleep=fake.sleep,
                   state_path=tmp_path / "state.json", **kw)


def test_debounce_coalesces_burst(tmp_path):
    f = Fake()
    w = make(tmp_path, f)
    w.tick()  # start-up catch-up fires immediately
    assert len(f.calls) == 1
    f.t += 100
    for _ in range(10):  # burst of events, 5 s apart
        w.note_event()
        f.t += 5
        assert w.tick() is None
    f.t += 14
    assert w.tick() is None  # 19 s of quiet: not yet
    f.t += 1
    assert w.tick() is OK or len(f.calls) == 2
    assert len(f.calls) == 2


def test_rate_limit_spaces_syncs(tmp_path):
    f = Fake()
    w = make(tmp_path, f, debounce=5, min_interval=60)
    w.tick()
    f.t += 10
    w.note_event()
    f.t += 6  # debounce over, but only 16 s since the last sync
    assert w.tick() is None
    f.t += 45
    assert w.tick() is not None
    assert len(f.calls) == 2 and f.calls[1] - f.calls[0] >= 60


def test_backoff_after_torn_copy_then_retry(tmp_path):
    f = Fake([TORN])
    w = make(tmp_path, f, debounce=5, backoff=300)
    w.tick()
    assert w.state["backoff_until"] is not None and w.state["last_status"] == "warning"
    for _ in range(20):  # 200 s of new events: still backed off
        f.t += 10
        w.note_event()
        w.tick()
    assert len(f.calls) == 1
    f.t += 200
    w.tick()
    assert len(f.calls) == 2 and w.state["backoff_until"] is None


def test_runner_crash_does_not_stop_watcher(tmp_path):
    f = Fake()

    def boom():
        raise RuntimeError("x")

    w = make(tmp_path, f)
    w.runner = boom
    w.tick()
    assert w.state["last_status"] == "error" and w.state["backoff_until"]


def test_drift_is_recorded_and_notified(tmp_path):
    f = Fake([RunOutcome("error", "HxStore layout changed, refusing to import")])
    seen = []
    w = make(tmp_path, f, notifier=lambda t, m: seen.append(m))
    w.tick()
    assert "layout changed" in w.state["last_drift_warning"]["message"] and len(seen) == 1


def test_polling_detects_changes(tmp_path):
    store = tmp_path / "HxStore.hxd"
    files = tmp_path / "Files"
    files.mkdir()
    store.write_bytes(b"a")
    f = Fake()
    w = Watcher([store, tmp_path / "hxcore.hfl", files], f.runner, clock=f.clock, wall=f.wall, sleep=f.sleep,
                state_path=tmp_path / "s.json", notifier=None, debounce=20)
    w.tick()
    assert len(f.calls) == 1 and not w.poll_changes()
    (files / "x.dat").write_bytes(b"1")
    assert w.poll_changes()
    store.write_bytes(b"abc")
    assert w.poll_changes() and not w.poll_changes()
    (tmp_path / "hxcore.hfl").write_bytes(b"log")  # appears later
    assert w.poll_changes()
    f.t += 100
    w.run(max_ticks=3)  # 15 s sleeps; sync after 20 s quiet and 60 s limit
    assert len(f.calls) == 2


def test_state_file_and_watcher_status(tmp_path):
    f = Fake()
    w = make(tmp_path, f, poll=15)
    w.tick()
    st = json.loads((tmp_path / "state.json").read_text())
    assert st["pid"] == os.getpid() and st["last_status"] == "ok" and st["heartbeat_at"] and st["last_sync_at"]
    beat = f.wall()
    assert watch.watcher_status(tmp_path / "state.json", now=beat + 10)["alive"]
    assert not watch.watcher_status(tmp_path / "state.json", now=beat + 50)["alive"]  # > 3 x 15 s
    st["pid"] = 2**22 + 12345
    (tmp_path / "state.json").write_text(json.dumps(st))
    dead = watch.watcher_status(tmp_path / "state.json", now=beat + 1)
    assert not dead["alive"] and dead["reason"] == "process not running"
    assert not watch.watcher_status(tmp_path / "nope.json")["alive"]


def test_parse_sync_output():
    out = watch.parse_sync_output(
        "2026-01-01T00:00:00 [hxstore] warning: seen=1 new=0 merged=0 skipped=0 errors=0 | 2 of 9 blocks failed "
        "even after a second copy\n  blocks ok=7\n", 1)
    assert out.status == "warning" and out.torn and out.should_back_off
    assert watch.parse_sync_output("x [hxstore] ok: seen=1 new=1\n", 0).status == "ok"
    assert watch.parse_sync_output("", 2).status == "error"


def test_sync_health(loaded, tmp_path):
    newest = max(c["last"] for c in loaded.coverage())
    ts = datetime.fromisoformat(newest).timestamp()
    h = tools.sync_health(loaded, now=ts + 7200, state_path=tmp_path / "none.json")
    assert h["last_sync_at"] and h["watcher"]["alive"] is False
    assert h["last_drift_warning"] is None
    assert tools.archive_status(loaded)["sync_health"]["last_sync_at"]


def test_sync_health_lag_and_drift(archive, tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    run = archive.start_run("hxstore", "x")
    archive.finish_run(run, status="error", message="HxStore layout changed, refusing")
    archive.conn.execute(
        "INSERT INTO messages(dedup_key, date_utc, date_ts, first_source, imported_at, updated_at) "
        "VALUES ('k', ?, 0, 'hxstore', ?, ?)", (old, old, old))
    pk = archive.conn.execute("SELECT id FROM messages").fetchone()[0]
    archive.conn.execute("INSERT INTO message_sources(message_pk, source, source_key, first_seen, last_seen) "
        "VALUES (?, 'hxstore', 'a', ?, ?)", (pk, old, old))
    archive.conn.commit()
    state = tmp_path / "state.json"
    w = Watcher([], lambda: OK, state_path=state, notifier=None)
    w.tick()
    h = tools.sync_health(archive, state_path=state)
    assert 5 * 3600 - 60 < h["lag_vs_now_s"] < 5 * 3600 + 60
    assert h["lag_vs_last_sync_s"] > 4 * 3600
    assert h["watcher"]["alive"] and "layout changed" in h["last_drift_warning"]["message"]


def test_launchd_watch_plist():
    pl = plistlib.loads(launchd.render(watch=True, program=["/opt/bin/new-outlook"]))
    assert pl["Label"] == launchd.WATCH_LABEL == launchd.LABEL + ".watch"
    assert pl["ProgramArguments"] == ["/opt/bin/new-outlook", "watch"]
    assert pl["KeepAlive"] is True and pl["RunAtLoad"] is True and pl["ThrottleInterval"] == 60
    assert pl["ProcessType"] == "Background" and pl["LowPriorityIO"] is True and pl["Nice"] == 10
    assert pl["StandardOutPath"].endswith("watch.log")


def test_launchd_watch_install_is_separate(tmp_path):
    p = launchd.install(load=False, watch=True, program=["/x/new-outlook"])
    q = launchd.install(load=False, program=["/x/new-outlook"])
    assert p != q and p.name.endswith(".watch.plist") and p.exists() and q.exists()
    assert launchd.uninstall(watch=True) and not p.exists() and q.exists()


def test_cli_launchd_watch_print(capsys):
    assert cli.main(["launchd", "print", "--watch"]) == 0
    assert plistlib.loads(capsys.readouterr().out.encode())["KeepAlive"] is True


def test_cli_watch_once(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(watch, "subprocess_runner", lambda *a, **k: (lambda: OK))
    assert cli.main(["watch", "--once", "--hxstore", str(tmp_path / "HxStore.hxd")]) == 0
    assert json.loads((tmp_path / "app" / "watch-state.json").read_text())["last_status"] == "ok"


def test_cli_status_shows_sync_health(loaded, tmp_path, capsys):
    assert cli.main(["--db", str(loaded.path), "status"]) == 0
    assert "sync health:" in capsys.readouterr().out
