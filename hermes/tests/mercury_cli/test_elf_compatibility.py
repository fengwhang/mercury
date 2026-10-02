"""Compatibility gates reject non-portable and malformed Linux binaries."""
import io

import pytest

from mercury_cli.elf import ElfInfo, check_elf, read_elf
from mercury_cli.update_release import _release_asset_candidates


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_asset_selection_never_falls_back_across_libc(arch):
    musl = _release_asset_candidates("0.3.7", arch, "musl")
    assert musl == [f"mercury-0.3.7-musl-{arch}.tar.gz", f"mercury-musl-{arch}.tar.gz"]
    assert all("musl" not in name for name in _release_asset_candidates("0.3.7", arch, "glibc"))


def test_nix_store_interpreter_cannot_be_published():
    info = ElfInfo(62, "/nix/store/host/lib/ld-linux-x86-64.so.2", (), ())
    with pytest.raises(ValueError, match="Non-portable"):
        check_elf(info, "x64", "glibc", portable=True)


def test_nix_library_path_cannot_be_published():
    info = ElfInfo(62, "/lib64/ld-linux-x86-64.so.2", (), ("/nix/store/host/lib",))
    with pytest.raises(ValueError, match="Non-portable"):
        check_elf(info, "x64", "glibc", portable=True)


def test_missing_loader_is_diagnosed_before_execution(monkeypatch):
    monkeypatch.setattr("mercury_cli.elf.os.path.exists", lambda _: False)
    info = ElfInfo(62, "/lib64/ld-linux-x86-64.so.2", (), ())
    with pytest.raises(ValueError, match="Missing ELF loader.*binary exists"):
        check_elf(info, "x64", "glibc", host=True)


@pytest.mark.parametrize("libc,needed", [("glibc", ("libc.so.6",)), ("musl", ("libc.so",))])
def test_native_addon_requires_matching_libc(libc, needed):
    info = ElfInfo(62, None, needed, ())
    check_elf(info, "x64", libc, addon=True, portable=True)
    other = "glibc" if libc == "musl" else "musl"
    with pytest.raises(ValueError, match="libc mismatch"):
        check_elf(info, "x64", other, addon=True)


@pytest.mark.parametrize("content", [b"", b"\x7fELF", b"\x7fELF\x02\x01" + b"\0" * 58])
def test_malformed_elf_is_rejected(content):
    with pytest.raises(ValueError):
        read_elf(io.BytesIO(content))


@pytest.mark.parametrize("loader,libc", [
    ("/nix/store/missing-glibc/lib/ld-linux-x86-64.so.2", "glibc"),
    ("/lib/ld-musl-x86_64.so.1", "glibc"),
    ("/lib64/ld-linux-x86-64.so.2", "musl"),
])
def test_real_updater_rejects_incompatible_archive_before_swap(tmp_path, monkeypatch, capsys, loader, libc):
    import hashlib
    import shutil
    import struct
    import tarfile
    from mercury_cli import update_release as ur

    interpreter = loader.encode() + b"\0"
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 18, 62)
    struct.pack_into("<Q", header, 32, 64)
    struct.pack_into("<HH", header, 54, 56, 1)
    binary = bytes(header) + struct.pack("<IIQQQQQQ", 3, 0, 120, 0, 0, len(interpreter), len(interpreter), 1) + interpreter
    tar = tmp_path / "bad.tar.gz"
    with tarfile.open(tar, "w:gz") as archive:
        member = tarfile.TarInfo("mercury/omp/packages/coding-agent/dist/omp")
        member.size = len(binary)
        archive.addfile(member, io.BytesIO(binary))
    name = _release_asset_candidates("0.3.7", "x64", libc)[0]
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(ur, "_project_root", lambda: tmp_path / "live")
    monkeypatch.setattr(ur, "_installed_version", lambda: "0.3.6")
    monkeypatch.setattr(ur, "host_arch", lambda: "x64")
    monkeypatch.setattr(ur, "host_libc", lambda: libc)
    monkeypatch.setattr(ur, "_latest_release", lambda **kw: {"tag_name":"v0.3.7", "assets":[
        {"name":name, "browser_download_url":"tar"},
        {"name":name + ".sha256", "browser_download_url":"checksum"}]})

    def download(url, path):
        if url == "tar":
            shutil.copy2(tar, path)
        else:
            path.write_text(hashlib.sha256(tar.read_bytes()).hexdigest())

    monkeypatch.setattr(ur, "_download", download)
    monkeypatch.setattr(ur, "_swap_tree", lambda *a: pytest.fail("Incompatible binary reached live tree swap"))
    assert ur.update_from_release(assume_yes=True) == 1
    assert "checksum verified" in capsys.readouterr().out
    assert not (tmp_path / "live").exists()
