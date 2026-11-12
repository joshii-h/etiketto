"""Label files on the disk: replaced in one step without following planted
links or losing permissions, read with a size limit, locked while printing."""

from __future__ import annotations

import fcntl
import json
import os
import stat

import pytest

from etiketto import model
from etiketto.model import FORMAT_VERSION, CodeElement, Label, LabelError, TextElement


def test_save_through_a_symbolic_link_replaces_the_file_it_points_to(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.etiketto").write_text("{}")
    link = tmp_path / "link.etiketto"
    link.symlink_to(real / "a.etiketto")
    Label(tape_mm=12).save(link)
    assert link.is_symlink()
    assert Label.load(real / "a.etiketto").tape_mm == 12
    Label.store_counter(link, 9)
    assert link.is_symlink() and Label.load(real / "a.etiketto").counter_next == 9
    assert sorted(p.name for p in real.iterdir()) == ["a.etiketto"]


def test_save_keeps_the_permissions(tmp_path):
    path = tmp_path / "a.etiketto"
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
        Label().save(tmp_path / "new.etiketto")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "new.etiketto").stat().st_mode) == 0o644


def test_the_owner_is_kept_where_allowed(tmp_path, monkeypatch):
    path = tmp_path / "a.etiketto"
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
    path = tmp_path / "a.etiketto"
    Label().save(path)
    (tmp_path / "a.etiketto.tmp").symlink_to(victim)  # the name earlier versions used
    Label(tape_mm=12).save(path)
    Label.store_counter(path, 5)
    assert victim.read_text() == "precious"
    assert Label.load(path).counter_next == 5
    # even a link at the random name itself is not followed: a new name is drawn
    names = iter(["planted", "fresh"])
    monkeypatch.setattr(model.secrets, "token_hex", lambda _n: next(names))
    (tmp_path / ".a.etiketto.planted.tmp").symlink_to(victim)
    Label(tape_mm=25).save(path)
    assert victim.read_text() == "precious"
    assert Label.load(path).tape_mm == 25


def test_store_counter_clamps_and_marks_the_version(tmp_path):
    path = tmp_path / "a.etiketto"
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
    """An older Etiketto refuses such a file instead of printing "{=date}"."""
    assert label.to_dict()["version"] == 2 == FORMAT_VERSION
    assert Label.from_dict(label.to_dict()) == label


def test_reading_stops_at_the_size_limit(monkeypatch):
    monkeypatch.setattr(model, "MAX_FILE_BYTES", 10)
    with pytest.raises(LabelError) as e:
        Label.load("/dev/zero")  # its size says 0, it never ends
    assert e.value.code == "damaged"


def test_a_lock_on_the_label_file(tmp_path):
    path = tmp_path / "a.etiketto"
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
    path = tmp_path / "a.etiketto"
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
    path = tmp_path / "a.etiketto"
    Label().save(path)

    def unsupported(*_args):
        raise OSError("not supported")

    monkeypatch.setattr(model.fcntl, "flock", unsupported)
    with model.locked(path):
        pass


def test_a_read_only_file_is_not_replaced(tmp_path):
    path = tmp_path / "a.etiketto"
    Label().save(path)
    path.chmod(0o444)
    try:
        assert not model.writable(path)
        with pytest.raises(PermissionError):
            Label(tape_mm=12).save(path)
    finally:
        path.chmod(0o644)
    assert Label.load(path).tape_mm == 50
    assert model.writable(path) and model.writable(tmp_path / "new.etiketto")


def test_a_lock_on_a_file_removed_meanwhile(tmp_path, monkeypatch):
    path = tmp_path / "a.etiketto"
    Label().save(path)
    real_flock = fcntl.flock

    def flock(fd, operation):
        real_flock(fd, operation)
        if path.exists():
            path.unlink()  # gone after the lock: try again, then go on without

    monkeypatch.setattr(model.fcntl, "flock", flock)
    with model.locked(path):
        pass
