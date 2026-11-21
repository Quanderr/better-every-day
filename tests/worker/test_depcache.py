import shutil

import pytest

from bed.testing.fakes import FakeCacheStore
from bed.worker.depcache import MAX_AGE_S, DependencyCache

pytestmark = pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd not installed")
NOW = 1_800_000_000.0


def cache(store, home, now=NOW, **kw):
    return DependencyCache(store, home, now=lambda: now, **kw)


def populate(home, content="left-pad"):
    (home / ".npm/_cacache").mkdir(parents=True)
    (home / ".npm/_cacache/pkg").write_text(content)
    (home / ".local/share/mise/installs/node").mkdir(parents=True)
    tool = home / ".local/share/mise/installs/node/node"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    (home / "unrelated").write_text("not cached")


def saved(tmp_path, fingerprint="fp1", now=NOW):
    store = FakeCacheStore()
    home = tmp_path / "first"
    populate(home)
    c = cache(store, home, now)
    c.restore(lambda: fingerprint)
    c.save()
    return store


def test_miss_then_save_uploads_cache_paths_only(tmp_path):
    store = saved(tmp_path)
    assert store.uploads == 1
    assert store.stored[0] == {"fingerprint": "fp1", "created": str(int(NOW))}
    home = tmp_path / "second"
    cache(store, home).restore(lambda: "fp1")
    assert (home / ".npm/_cacache/pkg").read_text() == "left-pad"
    assert (home / ".local/share/mise/installs/node/node").stat().st_mode & 0o100
    assert not (home / "unrelated").exists()


def test_exact_hit_is_not_uploaded_again(tmp_path):
    store = saved(tmp_path)
    c = cache(store, tmp_path / "second")
    c.restore(lambda: "fp1")
    c.save()
    assert store.uploads == 1


def test_changed_fingerprint_restores_then_uploads_keeping_creation_time(tmp_path):
    store = saved(tmp_path)
    home = tmp_path / "second"
    c = cache(store, home, NOW + 3600)
    c.restore(lambda: "fp2")
    assert (home / ".npm/_cacache/pkg").exists()
    c.save()
    assert store.uploads == 2
    assert store.stored[0] == {"fingerprint": "fp2", "created": str(int(NOW))}


def test_expired_cache_is_not_restored_and_is_rebuilt(tmp_path):
    store = saved(tmp_path)
    later = NOW + MAX_AGE_S + 1
    home = tmp_path / "second"
    c = cache(store, home, later)
    c.restore(lambda: "fp1")
    assert not (home / ".npm").exists()
    populate(home, "fresh")
    c.save()
    assert store.stored[0]["created"] == str(int(later))
    c.discard()
    assert store.deletes == 0


def test_corrupt_archive_is_wiped_and_replaced(tmp_path):
    store = FakeCacheStore()
    store.stored = ({"fingerprint": "fp1", "created": str(int(NOW))}, b"not an archive")
    home = tmp_path / "home"
    (home / ".npm").mkdir(parents=True)
    (home / ".npm/partial").write_text("x")
    c = cache(store, home)
    c.restore(lambda: "fp1")
    assert not (home / ".npm").exists()
    populate(home)
    c.save()
    assert store.uploads == 1


def test_store_failures_never_raise(tmp_path):
    store = FakeCacheStore()
    home = tmp_path / "home"
    populate(home)
    c = cache(store, home)
    store.fail_next("open")
    c.restore(lambda: "fp1")
    populate(home)
    store.fail_next("upload")
    c.save()
    assert store.stored is None


def test_failing_fingerprint_disables_save(tmp_path):
    store = FakeCacheStore()
    home = tmp_path / "home"
    populate(home)
    c = cache(store, home)

    def boom():
        raise RuntimeError("git failed")

    c.restore(boom)
    populate(tmp_path / "other")
    c.save()
    assert store.uploads == 0


def test_oversize_archive_is_not_stored(tmp_path):
    store = FakeCacheStore()
    home = tmp_path / "home"
    populate(home)
    c = cache(store, home, max_bytes=10)
    c.restore(lambda: "fp1")
    c.save()
    assert store.stored is None


def test_nothing_to_cache_skips_upload(tmp_path):
    store = FakeCacheStore()
    c = cache(store, tmp_path / "empty")
    c.restore(lambda: "fp1")
    c.save()
    assert store.uploads == 0


def test_discard_deletes_a_restored_cache_and_blocks_save(tmp_path):
    store = saved(tmp_path)
    c = cache(store, tmp_path / "second")
    c.restore(lambda: "fp2")
    c.discard()
    c.save()
    assert store.stored is None and store.deletes == 1 and store.uploads == 1


def crafted(tmp_path, build, paths):
    import subprocess

    source = tmp_path / "crafted"
    source.mkdir()
    build(source)
    tar = subprocess.run(["tar", "-C", str(source), "-cf", "-", *paths], check=True, capture_output=True)
    body = subprocess.run(["zstd", "-qc"], input=tar.stdout, check=True, capture_output=True).stdout
    store = FakeCacheStore()
    store.stored = ({"fingerprint": "fp1", "created": str(int(NOW))}, body)
    return store


def test_restore_takes_only_cache_paths_from_archive(tmp_path):
    def build(source):
        populate(source)
        (source / ".bashrc").write_text("evil")
        (source / ".config/mise").mkdir(parents=True)
        (source / ".config/mise/config.toml").write_text("evil")

    store = crafted(tmp_path, build, [".npm", ".bashrc", ".config"])
    home = tmp_path / "home"
    cache(store, home).restore(lambda: "fp1")
    assert (home / ".npm/_cacache/pkg").exists()
    assert sorted(p.name for p in home.iterdir()) == [".npm"]


def test_restore_skips_symlinked_cache_root(tmp_path):
    def build(source):
        (source / ".npm").symlink_to(".")
        (source / ".cache").mkdir()
        (source / ".cache/ok").write_text("ok")

    store = crafted(tmp_path, build, [".npm", ".cache"])
    home = tmp_path / "home"
    cache(store, home).restore(lambda: "fp1")
    assert sorted(p.name for p in home.iterdir()) == [".cache"]


def test_future_creation_time_counts_as_expired(tmp_path):
    store = saved(tmp_path, now=NOW + 10 * MAX_AGE_S)
    home = tmp_path / "second"
    c = cache(store, home)
    c.restore(lambda: "fp1")
    assert not (home / ".npm").exists()
    populate(home)
    c.save()
    assert store.stored[0]["created"] == str(int(NOW))


def test_save_survives_missing_archive_tools(tmp_path, monkeypatch):
    store = FakeCacheStore()
    home = tmp_path / "home"
    populate(home)
    c = cache(store, home)
    c.restore(lambda: "fp1")
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    c.save()
    assert store.uploads == 0
