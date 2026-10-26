# Printer-side LAN stream failure: retained client threads

**Research case:** 19 September 2026. **Component:** the stock `ai_camera`
program running on the Elegoo Centauri Carbon 2 printer, not the firmware
inside the USB camera module.

## Summary and scope

The printer's LAN video server creates a worker thread for each stream client,
but retains the finished thread and its resources after that client disconnects.
Resources are reclaimed only when the entire streamer stops. Repeatedly opening
camera views, or software reconnecting in the background, therefore accumulates
thread resources even with only a few simultaneous viewers. In the investigated
case, each retained stack reserved 8 MiB of virtual address space. Eventually a
new worker could not be created; the resulting unhandled C++ exception aborted
`ai_camera`, leaving nothing listening on TCP port 8080. This can look like a
black camera preview even though the camera module itself has not failed.

This is separate from the camera-module JFFS2 flash-cleanup failure addressed by
[this repository's prevention and recovery tools](../README.md). That failure
accumulates old configuration records across camera boots until the module can
no longer start successfully. Repairing the camera's flash does not fix the
printer's worker-thread lifetime bug; replacing the printer's streaming program
does not repair the camera's flash. Both defects can exist on the same printer.
Camera PCB revision alone cannot diagnose a printer-software failure.

The owner observed the stream disappearing after hours of operation in LAN
mode. Time to failure depends on connection churn and available resources, not
a fixed timer or number of prints. This note establishes the defect in the
specific build below; it does not establish that every firmware release has it
or that cloud-mode operation cannot encounter related code.

## Affected build and evidence scope

| Property | Observed value |
|---|---|
| Printer firmware banner | `01.02.12.01:tina.3dp.20260707.085328` |
| Printer kernel / architecture | Linux `5.4.61`, `armv7l`; 32-bit ARM service |
| Executable | `/opt/bin/ai_camera`, 4,160,772 bytes |
| Embedded commit | `5a04a82ee13ac6d39af3bc256d1271bb090c3d34` |
| Embedded commit date | `2026-06-18 06:45:00 +0000` |
| Embedded build time | `2026-06-18 14:49:54` |
| Executable SHA-256 | `15672a7c85b90a81d2b4aa3596e5204fab5a54df29bd734714952b44c3bfeb43` |

The owner supplied live logs, the exact installed executable, a core prefix,
two selected core slices, and matching runtime libraries. The on-disk executable
and `/proc/<pid>/exe` had the same hash. An accompanying root-partition image
contained an older executable; the thread-lifetime and crash conclusions here
use the exact June build above, not an assumption that the root image matched.

**Hardware observations** came from the owner's printer. **Offline verification**
decoded ELF metadata, instructions, saved return addresses, and the exception
object without executing the ARM binaries. The raw firmware, binaries, core
memory, and private logs are not redistributed here. Hashes and independently
reconstructed behavior are recorded below for auditability.

## What the program does wrong

The server tracks active sockets separately from a vector of `std::thread`
objects. Admission checks limit simultaneous stream clients to four, but that
does not bound the number of completed workers retained in the thread vector.
The following is independently reconstructed, normalized pseudocode, not
vendor-authored source:

```cpp
on_client_accepted(fd) {
    active_clients.push_back(fd);
    log_active_client_count();
    client_threads.emplace_back(handle_client, this, fd);
}

on_client_handler_finished(fd) {
    close(fd);
    remove_from_active_clients(fd);
    // No join, detach, or removal from client_threads here.
}

on_entire_streamer_stopped() {
    for (auto& worker : client_threads)
        if (worker.joinable())
            worker.join();
    client_threads.clear();
}
```

Returning from a joinable thread does not release all its associated resources;
it must eventually be joined or detached [1]. During continuous operation this
implementation therefore retains completed workers and their stack reservations.
It can exhaust the limited virtual address space of the 32-bit process without
using an equivalent amount of physical RAM. Reducing simultaneous viewers does
not reclaim resources already retained from earlier connections.

A failed `pthread_create()` returns an error number. The supplied C++ runtime
turns a nonzero return into `std::system_error`, and the server does not handle
failure of the client-thread constructor. An exception escaping the server
callback reaches the runtime's thread-entry catch-all, which terminates the
whole process. The inspected startup script launches `ai_camera &` without
camera-specific respawn supervision; the owner found the process absent and
port 8080 refusing connections, rather than automatically recovering.

## Crash findings and limits of the diagnosis

The final log admitted a new client and logged four active clients, but never
logged that new handler starting. An older handler finished about 40 ms later,
then logging ended. The core timestamp matched that failure. The preserved
core describes process 1910 and aborting server thread 2057, with `SIGABRT` (6)
and `SI_TKILL` (-6), sent by the process itself.

The ARM saved-return reconstruction, checked against the supplied libraries,
is shown from the stopped location outward:

```text
0xb3cdb21c  raise
0xb3cdc4dc  abort
0xb3ed6920  __gnu_cxx::__verbose_terminate_handler
0xb3ed4abc  __cxxabiv1::__terminate helper
0xb3ed4b30  std::terminate
0xb3ef376c  execute_native_thread_routine, catch-all path
0xb3f6cde8  pthread start_thread
0xb3d68f30  clone
```

The application frames had already unwound. The earlier client-thread creation
path is reconstructed from the executable, constructor-unwind remnants, and
exception object; it is not an extra set of live frames in that backtrace.

| Directly decoded observation | Result |
|---|---|
| Live thread register records | 9 |
| Guard-page plus writable-stack mapping pairs | 370 |
| Size of each pair | 4 KiB guard + `0x7ff000` writable bytes = 8 MiB |
| Total address space in those pairs | 2,960 MiB = 2.890625 GiB |
| Pairs with no live thread's stack pointer | 363 |
| Fatal exception | `std::system_error` |
| Stored error category and value | `std::generic_category()`, 11 (`EAGAIN`) |

A stack-shaped mapping without a live stack pointer is not independently proof
of one particular dead HTTP worker; unused or cached stacks can also exist.
The hundreds of mappings, nine live threads, and verified retention code
jointly support the resource-exhaustion diagnosis. Core logical size, allocated
disk blocks, virtual address space, and resident RAM are distinct quantities.
The owner reported a roughly 2.9 GiB sparse core occupying only 7.1 MiB on disk.

The exception type, category, and integer are decoded from the caught exception
object, not inferred from an old stack string or a later `errno`. `EAGAIN` can
mean insufficient resources or an imposed process/thread limit [1]. The supplied
pthread library can translate an internal `ENOMEM` into `EAGAIN`, so this value
is compatible with failing to allocate another stack. No trace of the original
failing `mmap` or `clone` was recovered. **Retained worker resources exhausting
usable address space is the high-confidence root cause; the exact failing
internal operation is not individually established.**

## Why a manual restart can still produce no feed

Starting the service and enabling LAN streaming are separate operations. The
exact executable initializes its local-stream flag to disabled. It can bind
and listen on port 8080, then sleep for 500 ms repeatedly before reaching
`accept()`. The owner's manual instance showed precisely this pattern: TCP
connections queued, HTTP returned no headers, and the server thread repeatedly
called `nanosleep` with 500,000,000 ns. This is a paused streamer, not proof of
a camera failure or a full active-client list.

The internal control message to resume that state is:

```json
{"id":123,"method":"local_video_monitor","params":{"status":"resume"}}
```

`"status":"pause"` disables it. This is protocol documentation, **not an HTTP
request or a recommendation to inject commands into a running printer**. The
control endpoint is `/tmp/aicamera_uds`; the printer and GUI already occupied
its two control connections in this case. A third connection may just queue.
Reconnecting control sockets alone did not restore LAN-enable state in the
observed restart. Recovery must account for both process startup and that state.

## Diagnosis and practical options

An unavailable feed or a refused connection alone is not proof of either known
bug. With existing **root access to the printer**, these bounded checks inspect
the current state without restarting services or changing camera configuration:

```sh
ps w | grep '[a]i_camera'
netstat -lntp 2>/dev/null | grep ':8080'
ls -l /dev/video* 2>/dev/null
ls -lh /opt/usr/logs/ai_camera* 2>/dev/null
cat /proc/sys/kernel/core_pattern
```

A missing process and listener support a printer-service failure. Present video
device nodes show enumeration, not proof of working frames. Preserve existing
logs and core evidence before changing state. In this case the core pattern was
`/opt/usr/logs/%e.core.log` with `core_uses_pid=0`, so another crash can reuse the
same pathname. Raw cores can contain private data; do not publish them. Ordinary
copy/archive operations may expand a sparse core to its full logical size.

Do not stop the whole printer service or reboot during a print. The inspected
`/etc/init.d/printer` stop operation also stops the GUI, print-control process,
and MQTT service, not just the camera. A reboot used for a replacement-webcam
test restarts the streaming program too; that test alone cannot distinguish a
camera-module fault from the service having crashed before the reboot.

### Optional replacement for rooted CC2 printers

[viridivn/ai_camera](https://github.com/viridivn/ai_camera) is a third-party
replacement for the printer's stock streaming program, not for camera-module
firmware. It requires root access to replace the printer executable; it is an
option for owners already rooted or independently considering that step, not
a requirement of this repository's camera-flash repair.

As of 27 September 2026, the owner who supplied this crash reported about a week
of successful use of that replacement. This is one owner's experience, not a
comparative stress test or a compatibility guarantee; the tested replacement
commit was not supplied. Upstream's README [2] says it is tested only in LAN-only
mode, removes all AI functionality, needs more stress testing, and is not tested
on all firmware versions. It also notes build-system and higher-resolution USB
load uncertainties. Read upstream's current instructions and limitations,
preserve the stock binary and a way to restore it, and make changes only with
the printer idle. `cc2camera` does not install or manage this replacement.

A proper repair of the stock implementation would reclaim finished workers
during normal operation or use a bounded reusable worker model, and handle
thread-creation failure by rolling back/closing the admitted client without
aborting the service. Blindly detaching handlers is insufficient if they retain
`this` and can outlive the streamer. Smaller stacks, fewer reconnects, or
periodic restarts can postpone or mask the failure but do not correct unbounded
retention. No repaired stock binary is provided or hardware-validated here.

## Audit anchors for this case

Executable addresses below are ELF virtual addresses in the exact hashed June
build, not file offsets or patch instructions. Core virtual addresses and
library load biases belong only to this original crash.

| Executable location | Address |
|---|---|
| `VideoStreamer::run_server()` | `0x0015a97c` |
| Append admitted socket | call at `0x0015b134` |
| Log active-client count | call at `0x0015b1dc` |
| Append client `std::thread` | call at `0x0015b210` |
| Client-thread constructor / call to `_M_start_thread` | `0x0015faf8` / `0x0015fb84` |
| Constructor exception-cleanup landing pad | `0x0015fb9c` |

The 16 KiB stack slice comes from core file offset `0xb089f000`, mapping to
virtual range `0xb11fc000–0xb1200000`. Crashing SP is `0xb11fec48`. Stack/TLS
slot `0xb11ff918` points to exception header `0xb01104f8`; its adjusted object
pointer is `0xb0110570`. The 4 KiB exception page comes from core offset
`0xaf892000`, mapping to `0xb0110000–0xb0111000`.

At object +8 (`0xb0110578`) the stored error is 11; at +12 it holds category
pointer `0xb3f64094`, matching the supplied `generic_category()` implementation.
The RTTI pointer, destructor, and object vtable also identify `std::system_error`.
The object's message pointer is outside the supplied page: the error text
"Resource temporarily unavailable" was checked in libc's error table, not read
from the missing heap-resident `what()` string. The runtime library load biases
are libc `0xb3cb2000`, libstdc++ `0xb3e61000`, and libpthread `0xb3f67000`.

### Input fingerprints and offline rechecking

These are identifiers for privately supplied artifacts, not download links.
Core slices are incomplete and must not be treated as a full core with missing
bytes silently replaced by zeroes.

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `ai_camera` | 4,160,772 | `15672a7c85b90a81d2b4aa3596e5204fab5a54df29bd734714952b44c3bfeb43` |
| First 4 MiB of original core | 4,194,304 | `12f82023489cb1412d34d28ffddfb2c5c4972ac3656c49f2755e553131fa3add` |
| Server stack slice | 16,384 | `6cd1926779264eb45306beced00d381e74c1d8f636dfc1b151ce82483d57b516` |
| Exception page | 4,096 | `251f17aea40ff79388fd4da63550f2b14f4673eee541d3a0d0d11d42dc78b1a5` |
| `libc-2.23.so` | 1,095,404 | `af176e5c3aabe81f293d574727d47060a5d49c452f87afce724c2968ae432a2a` |
| `libstdc++.so.6.0.22` | 996,956 | `9516f349bb543a0d8d6359d738bd80c0dc0fd7a996b81425277a362e161162f9` |
| `libpthread-2.23.so` | 84,484 | `3374386b829d26645f0e25c171bd3a1d5c56ae6d59083fd2bed194ed5a926610` |

An analyst with the private originals can first check `sha256sum`, then inspect
ELF headers, program headers, notes, and symbols with `readelf -h -l -n -s` on a
PC. The core prefix contains the complete program-header table and thread notes;
use each `PT_LOAD` mapping to translate the two slice offsets to virtual
addresses above. ARM disassembly should be checked against the exact executable
and libraries, not different public-source versions. These are offline reads,
not instructions to run the binaries or deliberately crash a printer.

## External references

The device-specific findings above come from the owner's supplied artifacts.
External references document API semantics or third-party project limitations;
they are not independent proof of this printer's crash.

1. [Linux pthread_create manual: errors and joinable-thread lifetime](https://man7.org/linux/man-pages/man3/pthread_create.3.html).
2. [viridivn/ai_camera README inspected at commit e38d2f7](https://github.com/viridivn/ai_camera/blob/e38d2f7522d17f358f474b95d439bdf8e95a3928/README.md).
