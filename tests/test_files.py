"""Label files on the disk: replaced in one step without following planted
links or losing permissions, read with a size limit, locked while printing."""

from __future__ import annotations

import fcntl
import json
import os
import stat

import pytest
from conftest import read_only

from tavletta import model
from tavletta.model import FORMAT_VERSION, CodeElement, Label, LabelError, TextElement


def test_save_through_a_symbolic_link_replaces_the_file_it_points_to(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.tavletta").write_text("{}")
    link = tmp_path / "link.tavletta"
    link.symlink_to(real / "a.tavletta")
    Label(tape_mm=12).save(link)
    assert link.is_symlink()
    assert Label.load(real / "a.tavletta").tape_mm == 12
    Label.store_counter(link, 9)
    assert link.is_symlink() and Label.load(real / "a.tavletta").counter_next == 9
    assert sorted(p.name for p in real.iterdir()) == ["a.tavletta"]


def test_save_keeps_the_permissions(tmp_path):
    path = tmp_path / "a.tavletta"
    Label().save(path)
    path.chmod(0o600)
    Label(tape_mm=12).save(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o640)
    Label.store_counter(path, 3)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_a_new_file_gets_the_usual_permissions(tmp_path):
    old = os.umask(0o022)
    try:
        Label().save(tmp_path / "new.tavletta")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "new.tavletta").stat().st_mode) == 0o644


def test_the_owner_is_kept_where_allowed(tmp_path, monkeypatch):
    path = tmp_path / "a.tavletta"
    Label().save(path)
    calls = []
    monkeypatch.setattr(model.os, "getuid", lambda: os.stat(path).st_uid + 1)  # someone else's
    monkeypatch.setattr(model.os, "fchown", lambda fd, uid, gid: calls.append((uid, gid)))
    Label(tape_mm=12).save(path)
    owner = os.stat(path)
    assert calls[0] == (owner.st_uid, owner.st_gid)


def test_a_planted_link_at_a_temporary_name_is_not_followed(tmp_path, monkeypatch):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    path = tmp_path / "a.tavletta"
    Label().save(path)
    (tmp_path / "a.tavletta.tmp").symlink_to(victim)  # the name earlier versions used
    Label(tape_mm=12).save(path)
    Label.store_counter(path, 5)
    assert victim.read_text() == "precious"
    assert Label.load(path).counter_next == 5
    # even a link at the random name itself is not followed: a new name is drawn
    names = iter(["planted", "fresh"])
    monkeypatch.setattr(model.secrets, "token_hex", lambda _n: next(names))
    (tmp_path / ".a.tavletta.planted.tmp").symlink_to(victim)
    Label(tape_mm=25).save(path)
    assert victim.read_text() == "precious"
    assert Label.load(path).tape_mm == 25


def test_store_counter_clamps_and_marks_the_version(tmp_path):
    path = tmp_path / "a.tavletta"
    Label().save(path)
    Label.store_counter(path, model.MAX_COUNTER + 10)
    data = json.loads(path.read_text())
    assert data["counter_next"] == model.MAX_COUNTER and data["version"] == 2
    assert Label.saved_counter(path) == model.MAX_COUNTER
    assert Label.saved_counter(tmp_path / "missing") is None
    path.write_text('{"counter_next": "x"}')
    assert Label.saved_counter(path) is None
    path.write_text("[]")
    assert Label.saved_counter(path) is None
    with pytest.raises(LabelError):
        Label.store_counter(path, 1)


@pytest.mark.parametrize(
    "label",
    [
        Label(elements=[TextElement(text="{=date}")]),
        Label(elements=[CodeElement(data="x{=uuid}")]),
        Label(counter_next=4),
        Label(counter_step=2),
    ],
)
def test_variables_and_counters_need_format_2(label):
    """An older Tavletta refuses such a file instead of printing "{=date}"."""
    assert label.to_dict()["version"] == 2 == FORMAT_VERSION
    assert Label.from_dict(label.to_dict()) == label


def test_reading_stops_at_the_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "MAX_FILE_BYTES", 10)
    path = tmp_path / "big.tavletta"
    path.write_text("x" * 11)
    with pytest.raises(LabelError) as e:
        Label.load(path)
    assert e.value.code == "damaged"


@pytest.mark.parametrize("special", ["/dev/zero", "fifo", "dir"])
def test_only_regular_files_are_read(tmp_path, special):
    if special == "fifo":
        special = tmp_path / "fifo"
        os.mkfifo(special)  # nobody writes: an open that waits would hang
    elif special == "dir":
        special = tmp_path
    try:
        Label.load(special)
    except LabelError as e:
        assert e.code == "not_label"
    except IsADirectoryError:
        assert special == tmp_path
    else:
        raise AssertionError("read %s" % special)
    with model.locked(special):  # nothing to lock
        pass


def test_a_lock_on_the_label_file(tmp_path):
    path = tmp_path / "a.tavletta"
    Label().save(path)
    with model.locked(path):
        fd = os.open(path, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):  # another process has to wait
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        Label.store_counter(path, 2)  # replaces the file under the lock
    with model.locked(tmp_path / "missing"):  # nothing to lock: goes on
        pass


def test_a_waiting_lock_follows_the_replaced_file(tmp_path, monkeypatch):
    path = tmp_path / "a.tavletta"
    Label().save(path)
    real_flock = fcntl.flock
    calls = []

    def flock(fd, operation):
        calls.append(fd)
        real_flock(fd, operation)
        if len(calls) == 1:  # meanwhile another process saved the label
            Label(tape_mm=12).save(path)

    monkeypatch.setattr(model.fcntl, "flock", flock)
    with model.locked(path):
        pass
    assert len(calls) == 2  # locked again, the new file


def test_without_flock_it_goes_on(tmp_path, monkeypatch):
    path = tmp_path / "a.tavletta"
    Label().save(path)

    def unsupported(*_args):
        raise OSError("not supported")

    monkeypatch.setattr(model.fcntl, "flock", unsupported)
    with model.locked(path):
        pass


def test_a_read_only_file_is_not_replaced(tmp_path):
    path = tmp_path / "a.tavletta"
    Label().save(path)
    with read_only(path):
        assert not model.writable(path)
        with pytest.raises(PermissionError):
            Label(tape_mm=12).save(path)
    assert Label.load(path).tape_mm == 50
    assert model.writable(path) and model.writable(tmp_path / "new.tavletta")


def test_a_lock_on_a_file_removed_meanwhile(tmp_path, monkeypatch):
    path = tmp_path / "a.tavletta"
    Label().save(path)
    real_flock = fcntl.flock

    def flock(fd, operation):
        real_flock(fd, operation)
        if path.exists():
            path.unlink()  # gone after the lock: try again, then go on without

    monkeypatch.setattr(model.fcntl, "flock", flock)
    with model.locked(path):
        pass


def test_the_file_lists_the_features_it_needs(tmp_path):
    label = Label(elements=[TextElement(text="{=date}")])
    data = label.to_dict()
    assert (data["version"], data["features"]) == (2, ["variables"])
    assert "features" not in Label().to_dict()
    assert Label.from_dict(dict(data, features=[])).elements == label.elements
    with pytest.raises(LabelError) as e:
        Label.from_dict(dict(data, features=["variables", "teleport"]))
    assert e.value.code == "newer"
    with pytest.raises(LabelError) as e:
        Label.from_dict(dict(data, features="variables"))
    assert e.value.code == "damaged"
    path = tmp_path / "a.tavletta"
    path.write_text('{"version": 1, "features": ["other"]}')
    with pytest.raises(LabelError):  # an unknown feature is refused in any version
        Label.load(path)
    path.write_text('{"version": 1, "tape_mm": 12, "printer": "vc500w"}')
    Label.store_counter(path, 7)
    data = json.loads(path.read_text())
    assert (data["version"], data["features"], data["counter_next"]) == (2, ["variables"], 7)
    Label.store_counter(path, 8)
    assert json.loads(path.read_text())["features"] == ["variables"]


def test_mode_and_fsync_are_best_effort(tmp_path, monkeypatch):
    """vfat or NTFS: fixed owner and mode, fsync maybe refused; saving works."""
    path = tmp_path / "a.tavletta"
    Label().save(path)

    def refuse(*_args):
        raise PermissionError("operation not permitted")

    def unsupported(*_args):
        raise OSError(22, "invalid argument")

    monkeypatch.setattr(model.os, "fchmod", refuse)
    monkeypatch.setattr(model.os, "fsync", unsupported)
    Label(tape_mm=12).save(path)
    Label.store_counter(path, 4)
    assert Label.load(path).counter_next == 4


def test_the_lock_opens_for_writing_where_it_can(tmp_path, monkeypatch):
    path = tmp_path / "a.tavletta"
    Label().save(path)
    opened = []
    real_open = os.open

    def record(target, flags, *args):
        opened.append(flags & (os.O_RDWR | os.O_WRONLY))
        return real_open(target, flags, *args)

    monkeypatch.setattr(model.os, "open", record)
    with model.locked(path):
        pass
    assert opened == [os.O_RDWR]
    path.chmod(0o444)
    try:
        opened.clear()
        with model.locked(path):
            pass
    finally:
        path.chmod(0o644)
    assert opened == [os.O_RDWR, os.O_RDONLY] or opened == [os.O_RDWR]  # root may write


def test_the_lock_gives_up_after_the_timeout(tmp_path, monkeypatch):
    path = tmp_path / "a.tavletta"
    Label().save(path)
    waited = []
    monkeypatch.setattr(model.time, "sleep", lambda _s: None)
    with (
        model.locked(path),
        pytest.raises(model.LockTimeout),
        model.locked(path, lambda: waited.append(1), timeout=0),
    ):
        pass
    assert waited == [1]


def test_reading_a_folder_leaves_no_open_file(tmp_path):
    before = len(os.listdir("/proc/self/fd"))
    for _n in range(5):
        with pytest.raises(LabelError):
            Label.load(tmp_path)
    assert len(os.listdir("/proc/self/fd")) == before


def test_a_read_only_file_system_is_locked_for_reading(tmp_path, monkeypatch):
    import errno

    path = tmp_path / "a.tavletta"
    Label().save(path)
    real_open = os.open
    opened = []

    def read_only_fs(target, flags, *args):
        if flags & os.O_RDWR:
            raise OSError(errno.EROFS, "read-only file system")
        opened.append(flags)
        return real_open(target, flags, *args)

    monkeypatch.setattr(model.os, "open", read_only_fs)
    with model.locked(path):
        assert len(opened) == 1  # locked, opened for reading
