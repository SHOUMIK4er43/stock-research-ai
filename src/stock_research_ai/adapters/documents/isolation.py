"""Run untrusted document parsing in a child process with hard time and memory bounds.

A hostile PDF, HTML page or workbook can drive a parser into unbounded CPU, memory or
recursion. A thread cannot be killed and shares the server's heap, so parsing happens in
a separate process instead:

- Process: a fresh interpreter (`sys.executable -P -c ...`, argument list, never a
  shell) - not a fork of the multi-threaded server, and it never re-imports the
  caller's `__main__`. It talks to the parent over its own stdin/stdout pipes only.
- Time: the parent enforces a wall-clock deadline covering the whole child (start-up,
  parsing and result transfer) and kills it when the deadline passes, however the
  parser is stuck.
- Memory: Linux sets a kernel RLIMIT_AS in the child, so allocation past the ceiling
  fails inside it. macOS refuses lowering RLIMIT_AS/RLIMIT_DATA/RLIMIT_RSS, so there the
  parent samples the child's physical footprint (libproc) every `_SAMPLE_SECONDS` and
  kills it once over the ceiling - a watchdog, not a kernel limit: a child can briefly
  overshoot by what it allocates within one sample interval. On any other platform
  (e.g. Windows) no memory ceiling is enforced; `MEMORY_ENFORCEMENT` says so and the
  time limit still applies.
- Data: no pickle in either direction. The parent sends a JSON header (target name,
  JSON arguments, limits) and the raw document bytes; the child replies with JSON,
  capped at `output_bytes`.

No temporary files. The child is always killed if still running and then reaped, so
none outlives a call or is left as a zombie. Failures are typed codes, never paths or
exception text (the child's stderr is discarded).
"""

import ctypes
import ctypes.util
import importlib
import json
import os
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import IO, Any

#: Ceiling for one parser child. Real annual reports (350-383 pages, 7-11 MB, ~1.3M
#: characters) peak near 100 MB in-process; 1 GiB leaves ~10x headroom (and ~2.5x over a
#: linear extrapolation to the 50 MB / 1,500-page retrieval ceilings) while keeping a
#: hostile document far away from the server's own memory.
DEFAULT_MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024
#: Largest JSON result accepted from a child (20M characters of text plus structure).
DEFAULT_OUTPUT_LIMIT_BYTES = 256 * 1024 * 1024
_SAMPLE_SECONDS = 0.02
_REAP_SECONDS = 5.0
_LENGTH = struct.Struct(">Q")
_CHILD_CODE = (
    "from stock_research_ai.adapters.documents.isolation import _child_main; _child_main()"
)


class IsolationFailure(StrEnum):
    TIMEOUT = "PARSER_TIMEOUT"
    MEMORY_LIMIT = "PARSER_MEMORY_LIMIT"
    CRASHED = "PARSER_CRASHED"
    ERROR = "PARSER_ERROR"
    OUTPUT_TOO_LARGE = "PARSER_OUTPUT_TOO_LARGE"


@dataclass(frozen=True)
class IsolationLimits:
    seconds: float
    memory_bytes: int = DEFAULT_MEMORY_LIMIT_BYTES
    output_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES


@dataclass(frozen=True)
class IsolatedResult:
    value: Any = None  # the target's JSON-decoded return value
    failure: IsolationFailure | None = None


class _RUsageInfoV2(ctypes.Structure):
    """<sys/resource.h> rusage_info_v2, used for ri_phys_footprint on macOS."""

    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        *[
            (name, ctypes.c_uint64)
            for name in (
                "ri_user_time ri_system_time ri_pkg_idle_wkups ri_interrupt_wkups "
                "ri_pageins ri_wired_size ri_resident_size ri_phys_footprint "
                "ri_proc_start_abstime ri_proc_exit_abstime ri_child_user_time "
                "ri_child_system_time ri_child_pkg_idle_wkups ri_child_interrupt_wkups "
                "ri_child_pageins ri_child_elapsed_abstime ri_diskio_bytesread "
                "ri_diskio_byteswritten"
            ).split()
        ],
    ]


def _libproc() -> Any:
    if sys.platform != "darwin":
        return None
    try:
        library = ctypes.CDLL(ctypes.util.find_library("proc") or "libproc.dylib")
        library.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        return library
    except (OSError, AttributeError):
        return None


_LIBPROC = _libproc()
#: How the memory ceiling is enforced on this platform: "rlimit" (kernel, plus the
#: watchdog), "watchdog" (parent samples and kills) or "none" (not enforced; the time
#: limit still applies).
MEMORY_ENFORCEMENT = (
    "rlimit" if sys.platform.startswith("linux") else "watchdog" if _LIBPROC else "none"
)


def _footprint(pid: int) -> int | None:
    """The child's current memory footprint in bytes, if this platform can tell."""
    if _LIBPROC is not None:
        info = _RUsageInfoV2()
        if _LIBPROC.proc_pid_rusage(pid, 2, ctypes.byref(info)) == 0:
            return int(info.ri_phys_footprint)
        return None
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/statm") as handle:
                return int(handle.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            return None
    return None


def _read_exact(stream: IO[bytes], size: int) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise EOFError
    return data


def _read_frame(stream: IO[bytes]) -> bytes:
    (size,) = _LENGTH.unpack(_read_exact(stream, _LENGTH.size))
    return _read_exact(stream, size)


def _child_main() -> None:
    """Child entry point: read one request from stdin, write one reply to stdout."""
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    header = json.loads(_read_frame(stdin))
    content = _read_frame(stdin)
    sys.path[:] = header["path"]
    if sys.platform.startswith("linux"):
        import resource

        try:
            limit = header["memory_bytes"]
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except (ValueError, OSError):
            pass  # the parent's footprint watchdog still applies
    try:
        module_name, _, name = header["target"].partition(":")
        target = getattr(importlib.import_module(module_name), name)
        result = target(content, *header["args"])
        payload = b"OK" + json.dumps(result, separators=(",", ":")).encode()
    except MemoryError:
        payload = b"MEM"
    except BaseException:  # noqa: BLE001 - any parser failure becomes one typed code
        payload = b"ERR"
    stdout.write(payload)
    stdout.flush()


def run_isolated(
    target: Callable[..., Any], content: bytes, *args: Any, limits: IsolationLimits
) -> IsolatedResult:
    """Run module-level `target(content, *args)` in a child process and return its
    JSON-able result. `args` must be JSON values.

    Blocking: async callers run this in a worker thread (`asyncio.to_thread`), which
    only waits on the child - the parsing itself never runs in the server process."""
    header = json.dumps(
        {
            "target": f"{target.__module__}:{target.__qualname__}",
            "args": list(args),
            "memory_bytes": limits.memory_bytes,
            "path": [p for p in sys.path if isinstance(p, str)],
        }
    ).encode()
    request = _LENGTH.pack(len(header)) + header + _LENGTH.pack(len(content)) + content
    deadline = time.monotonic() + limits.seconds
    process = subprocess.Popen(  # fixed argument list, no shell
        [sys.executable, "-P", "-c", _CHILD_CODE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    stdin, stdout = process.stdin, process.stdout
    assert stdin is not None and stdout is not None
    reply = bytearray()
    overflow = threading.Event()

    def write() -> None:
        try:
            stdin.write(request)
        except (OSError, ValueError):
            pass  # the child is already gone; its exit is reported below
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    def read() -> None:
        while chunk := os.read(stdout.fileno(), 1 << 20):
            if len(reply) + len(chunk) > limits.output_bytes:
                overflow.set()
                process.kill()
                return
            reply.extend(chunk)

    workers = [threading.Thread(target=write, daemon=True), threading.Thread(target=read)]
    for worker in workers:
        worker.start()
    failure: IsolationFailure | None = None
    try:
        while process.poll() is None:
            if time.monotonic() >= deadline:
                failure = IsolationFailure.TIMEOUT
                break
            used = _footprint(process.pid)
            if used is not None and used > limits.memory_bytes:
                failure = IsolationFailure.MEMORY_LIMIT
                break
            time.sleep(_SAMPLE_SECONDS)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()  # reap: no zombie survives the call
        for worker in workers:
            worker.join(_REAP_SECONDS)
        stdout.close()
    if failure is not None:
        return IsolatedResult(failure=failure)
    if overflow.is_set():
        return IsolatedResult(failure=IsolationFailure.OUTPUT_TOO_LARGE)
    if reply == b"MEM":
        return IsolatedResult(failure=IsolationFailure.MEMORY_LIMIT)
    if reply == b"ERR":
        return IsolatedResult(failure=IsolationFailure.ERROR)
    if process.returncode != 0 or not reply.startswith(b"OK"):
        return IsolatedResult(failure=IsolationFailure.CRASHED)
    return IsolatedResult(value=json.loads(bytes(reply[2:])))
