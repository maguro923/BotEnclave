import os

from botmgr.procident import ProcessIdentity, read_identity, verify


def test_read_identity_of_current_process() -> None:
    ident = read_identity(os.getpid())
    assert ident is not None
    assert ident.pid == os.getpid()
    assert ident.start_time > 0
    assert ident.boot_id


def test_verify_accepts_live_identity() -> None:
    ident = read_identity(os.getpid())
    assert ident is not None
    assert verify(ident) is True


def test_verify_rejects_wrong_start_time() -> None:
    ident = read_identity(os.getpid())
    assert ident is not None
    forged = ProcessIdentity(
        pid=ident.pid, start_time=ident.start_time + 1, boot_id=ident.boot_id
    )
    assert verify(forged) is False


def test_verify_rejects_wrong_boot_id() -> None:
    ident = read_identity(os.getpid())
    assert ident is not None
    forged = ProcessIdentity(
        pid=ident.pid, start_time=ident.start_time, boot_id="not-this-boot"
    )
    assert verify(forged) is False


def test_missing_process() -> None:
    # Far above any realistic pid_max assignment for a live process.
    assert read_identity(2**22) is None
    ghost = ProcessIdentity(pid=2**22, start_time=1, boot_id="x")
    assert verify(ghost) is False
