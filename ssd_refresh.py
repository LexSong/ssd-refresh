"""Find files that read slowly from an aging SSD, and rewrite them so they read fast again."""

import ctypes
import hashlib
import os
import shutil
import time
from ctypes import wintypes
from pathlib import Path
from typing import Annotated

import typer

MIB = 1024 * 1024
CHUNK = 8 * MIB
TMP_SUFFIX = ".ssd-refresh.tmp"

GENERIC_READ = 0x80000000
FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
FILE_BEGIN = 0
MEM_COMMIT_RESERVE = 0x1000 | 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, wintypes.LPVOID, wintypes.DWORD]
kernel32.SetFilePointerEx.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD, wintypes.LPVOID]
kernel32.ReadFile.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.VirtualAlloc.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
kernel32.VirtualAlloc.restype = wintypes.LPVOID
kernel32.VirtualFree.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD]
kernel32.VirtualFree.restype = wintypes.BOOL
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


def unbuffered_chunks(path: Path, offset: int, length: int):
    """Yield the bytes of path from offset, up to length bytes, read past the Windows file cache.

    A cached read measures RAM, not the drive. FILE_FLAG_NO_BUFFERING skips the cache,
    but then every read must start on a sector boundary and land in a sector-aligned
    buffer. So offset must be a multiple of 1 MiB, and VirtualAlloc gives a page-aligned
    buffer.
    """
    handle = kernel32.CreateFileW(
        str(path),
        GENERIC_READ,
        FILE_SHARE_ALL,
        None,
        OPEN_EXISTING,
        FILE_FLAG_NO_BUFFERING | FILE_FLAG_SEQUENTIAL_SCAN,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = kernel32.VirtualAlloc(None, CHUNK, MEM_COMMIT_RESERVE, PAGE_READWRITE)
    try:
        if not kernel32.SetFilePointerEx(handle, offset, None, FILE_BEGIN):
            raise ctypes.WinError(ctypes.get_last_error())
        remaining = length
        got = wintypes.DWORD()
        while remaining > 0:
            if not kernel32.ReadFile(handle, buffer, CHUNK, ctypes.byref(got), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if got.value == 0:
                return
            n = min(got.value, remaining)
            yield ctypes.string_at(buffer, n)
            remaining -= n
    finally:
        kernel32.VirtualFree(buffer, 0, MEM_RELEASE)
        kernel32.CloseHandle(handle)


def read_speed(path: Path, sample: int) -> float:
    """Return the drive's read speed in MB/s for a sample from the middle of path."""
    size = path.stat().st_size
    offset = max(0, (size // 2 - sample // 2) // MIB * MIB)
    total = 0
    start = time.perf_counter()
    for chunk in unbuffered_chunks(path, offset, sample):
        total += len(chunk)
    elapsed = time.perf_counter() - start
    return total / MIB / elapsed


def refresh(path: Path) -> None:
    """Rewrite path as new data on the drive, then put it back under the same name.

    We copy with our own read/write loop. shutil.copyfile calls CopyFile2 on Windows,
    and on ReFS that can block-clone: the copy then shares the old blocks, so nothing
    gets rewritten. The original stays in place until the copy reads back with the
    same hash, and os.replace swaps the names in one step.
    """
    tmp = path.with_name(path.name + TMP_SUFFIX)
    written = hashlib.blake2b()
    with path.open("rb") as src, tmp.open("wb") as dst:
        while chunk := src.read(CHUNK):
            written.update(chunk)
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    read_back = hashlib.blake2b()
    for chunk in unbuffered_chunks(tmp, 0, tmp.stat().st_size):
        read_back.update(chunk)
    if read_back.digest() != written.digest():
        tmp.unlink()
        raise RuntimeError(f"copy of {path} did not read back intact; original kept")
    shutil.copystat(path, tmp)
    os.replace(tmp, path)


def large_files(root: Path, min_size: int):
    """Yield files under root (or root itself) of at least min_size bytes."""
    if root.is_file():
        candidates = [root]
    else:
        candidates = sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    for path in candidates:
        if path.name.endswith(TMP_SUFFIX):
            continue
        if path.stat().st_size >= min_size:
            yield path


app = typer.Typer()


@app.command()
def scan(
    root: Annotated[Path, typer.Argument(exists=True, help="Folder to scan, or one file.")],
    min_size_mb: Annotated[int, typer.Option(help="Skip files smaller than this.")] = 1024,
    sample_mb: Annotated[int, typer.Option(help="How much of each file to read for the speed test.")] = 512,
    slow_mbs: Annotated[float, typer.Option(help="Files reading below this many MB/s count as slow.")] = 300,
    fix: Annotated[bool, typer.Option(help="Rewrite each slow file, then measure it again.")] = False,
) -> None:
    """Measure how fast the drive reads each large file, and optionally rewrite the slow ones."""
    slow = []
    for path in large_files(root, min_size_mb * MIB):
        speed = read_speed(path, sample_mb * MIB)
        label = "ok  "
        if speed < slow_mbs:
            label = "SLOW"
            slow.append(path)
        size_gb = path.stat().st_size / 1e9
        typer.echo(f"{speed:7.0f} MB/s  {label}  {size_gb:6.1f} GB  {path}")
    typer.echo(f"{len(slow)} slow file(s) below {slow_mbs:.0f} MB/s.")
    if not fix:
        return
    for path in slow:
        typer.echo(f"rewriting {path} ...")
        start = time.perf_counter()
        refresh(path)
        elapsed = time.perf_counter() - start
        speed = read_speed(path, sample_mb * MIB)
        typer.echo(f"{speed:7.0f} MB/s  after rewrite ({elapsed:.0f} s)  {path}")


def main() -> None:
    app()
