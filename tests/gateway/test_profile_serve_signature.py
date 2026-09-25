"""#111105: the profile re-scan watcher must rebuild adapters after a config.yaml replacement
that keeps mtime and size (``cp -p``, ``rsync -t``, a timestamp-pinning writer)."""
import os
import shutil
import time

from gateway.run_profile_reconcile import profile_serve_signature


def test_profile_serve_signature_changes_on_replacement_with_pinned_mtime(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("x" * 64, encoding="utf-8")
    before = profile_serve_signature(tmp_path)
    assert profile_serve_signature(tmp_path) == before  # unchanged file: stable
    st = cfg.stat()
    # ctime is the signal here; on a loaded runner the copy can land in the same filesystem
    # timestamp tick as the original write, so wait until the clock visibly moves first.
    probe = tmp_path / "tick"
    while True:
        probe.write_text("t", encoding="utf-8")
        if probe.stat().st_ctime_ns != st.st_ctime_ns:
            break
        time.sleep(0.005)
    other = tmp_path / "other"
    other.write_text("y" * 64, encoding="utf-8")
    shutil.copy2(other, cfg)
    os.utime(cfg, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert (cfg.stat().st_mtime_ns, cfg.stat().st_size) == (st.st_mtime_ns, st.st_size)
    assert profile_serve_signature(tmp_path) != before
