# Fast Snapshot Load via Local Postcopy (mapped-ram + userfaultfd)

**GSoC 2026 Project Proposal for QEMU**

## Table of Contents

- [1. Abstract / Executive Summary](#1-abstract--executive-summary)
- [2. The Bottleneck: I/O Bound State Deserialization](#2-the-bottleneck-i-o-bound-state-deserialization)
- [3. Proposed Architecture: Local Demand-Paging](#3-proposed-architecture-local-demand-paging)
  - [3.1 Translating Page Faults to File Offsets (ram.c)](#31-translating-page-faults-to-file-offsets-ramc)
  - [3.2 Concurrency & The multifd Challenge](#32-concurrency--the-multifd-challenge)
- [4. Implementation Roadmap (12-Week Timeline)](#4-implementation-roadmap-12-week-timeline)
- [5. Personal & Contact Information](#5-personal--contact-information)
- [6. Complete Research & Lab Notebook](#6-complete-research--lab-notebook)
  - [Initial Setup](#initial-setup)
  - [Building QEMU](#building-qemu)
  - [Running QEMU](#running-qemu)
  - [Debugging](#debugging)
  - [Creating Snapshots](#creating-snapshots)
  - [Loading Snapshots](#loading-snapshots)
  - [Benchmarking Results](#benchmarking-results)
  - [Live Migration Experiments](#live-migration-experiments)
  - [Minimalist Live Migration Lab Setup](#minimalist-live-migration-lab-setup)
  - [FAQs from my experiments](#faqs-from-my-experiments)
  - [Videos](#videos)

---

## 1. Abstract / Executive Summary

Restoring large VMs from snapshots currently incurs severe downtime because QEMU blocks the vCPU while sequentially loading RAM from disk. This project proposes implementing a "Local Postcopy" mechanism that bypasses this block by combining the new mapped-ram direct-I/O feature with the userfaultfd page-fault mechanism. This architecture will allow the VM to resume execution almost instantly, fetching memory pages on-demand from the local snapshot file, while utilizing refactored multifd threads to perform atomic background fetching.

The feasibility and performance characteristics of this approach have been validated through comprehensive lab experiments documented in [Section 6](#6-complete-research--lab-notebook), including detailed benchmarking across three snapshot methods, live migration analysis using both precopy and postcopy techniques, and architectural constraint analysis.

## 2. The Bottleneck: I/O Bound State Deserialization

Currently, QEMU's snapshot restoration process treats local file loads as a standard Precopy migration stream. The `qemu_loadvm_state()` routine strictly blocks the vCPU from entering `RUN_STATE_RUNNING` until the entire RAM payload is loaded.

While the introduction of mapped-ram allows for parallel disk I/O, the underlying architecture remains synchronous from the guest's perspective. To quantify this, I benchmarked a 2GB Kali Linux VM snapshot load on constrained hardware (4GB RAM host, 128GB NVMe SSD):

- Legacy Snapshot (Sequential): **18.459 s** (Mean)
- Mapped-RAM (Single-threaded): **9.226 s** (Mean)
- Mapped-RAM + multifd: **13.526 s** (Mean)*

*(Note: The multifd overhead in this specific low-resource constraint resulted in a slight regression due to display thread contention, as verified via VNC traces. See [Section 6: Benchmarking Results](#benchmarking-results) for complete test methodology, screenshots, and detailed statistical analysis).*

Even at ~9 seconds for a 2GB VM, this downtime scales unacceptably for enterprise VMs (e.g., 64GB+). The vCPU remains artificially paused while disk I/O populates memory pages the guest does not immediately require. Through the experiments detailed in [Section 6: Loading Snapshots](#loading-snapshots) and [Section 6: Benchmarking Results](#benchmarking-results), I have verified that the mapped-ram format enables parallel I/O, but the synchronous blocking architecture remains the fundamental bottleneck.

## 3. Proposed Architecture: Local Demand-Paging

This proposal introduces a "Local Postcopy" architecture. By marrying mapped-ram direct file offsets with the userfaultfd mechanism (currently used exclusively for Network Postcopy), we bypass the blocking `ram_load()` iteration entirely. The technical foundation for this approach is established through the postcopy live migration experiments detailed in [Section 6: Approach 2: Postcopy Migration](#approach-2-postcopy-migration), which demonstrate the userfaultfd page-fault handling in a network context. Local postcopy simply replaces the TCP source with disk I/O.

### 3.1 Translating Page Faults to File Offsets (ram.c)

Currently, `postcopy_ram_fault_thread` requests missing pages from a remote socket. I propose modifying this handler to perform direct `pread()` calls against the local snapshot file descriptor.

- **Map HVA to RAMBlock**: Utilize `poll_fault_page()` to convert the faulting address to its corresponding RAMBlock and relative offset (see [Section 6 FAQ: Address Translation](#how-to-translate-a-memory-address-into-file-offset-for-pread) for detailed technical breakdown).  
  ```c
  page_address = (void *)(uintptr_t) uffd_msg.arg.pagefault.address;
  block = qemu_ram_block_from_host(page_address, false, offset);
  ```

- **Calculate Absolute Offset**: Using the header parsed via `parse_ramblock_mapped_ram`, compute the exact disk location:  
  `file_offset = block->pages_offset + relative_offset;`  
  (Verified through experiments; refer to [Section 6: How to translate a memory address...](#how-to-translate-a-memory-address-into-file-offset-for-pread) for implementation details)

- **Inject and Resume**: Perform a blocking read at `file_offset` and inject it atomically using `qemu_ufd_copy_ioctl()`.

### 3.2 Concurrency & The multifd Challenge

Relying purely on demand-paging will cause guest stutter due to disk I/O latency. We must utilize multifd threads to perform background fetching while the VM runs. However, as defined in `multifd.c`:

```c
/* multifd thread should not be active... Two threads writing the same memory area could easily corrupt guest state. */
assert(!migration_in_postcopy());
```

Because multifd blasts data directly into memory buffers, it lacks the atomic guarantees required when userfaultfd is active. Furthermore, because `migrate_mapped_ram()` forces `multifd_use_packets()` to evaluate to false, worker threads bypass header parsing (as documented in [Section 6: How QEMU guarantees multifd only reads RAM](#how-qemu-guarantees-multifd-only-reads-ram-from-mapped-ram)).

**The Solution**: I will refactor the multifd receive threads to utilize the userfaultfd atomic ioctls when the mapped-ram fast-load flag is active, allowing parallel background fetching without violating the `!migration_in_postcopy()` assertion or corrupting guest memory. For the technical constraints, see [Section 6: Why postcopy and multifd can't work together yet](#why-postcopy-and-multifd-cant-work-together-yet).

To illustrate the current multifd capabilities, [Section 6: Test 3: Mapped-RAM (with multifd)](#test-3-mapped-ram-with-multifd) shows the performance characteristics when multifd is used with mapped-ram files on local snapshots, yielding valuable insights into the overhead and optimization opportunities.

## 4. Implementation Roadmap (12-Week Timeline)

To ensure smooth upstreaming and code review, I have structured the implementation into four discrete, sequential patch series mirroring the exact architectural dependencies.

**Community Bonding (Pre-coding)**
- Submit my custom mapped-ram Python verification script (see [Section 6 FAQ: How to tell if a snapshot is mapped-ram](#how-to-tell-if-a-snapshot-is-mapped-ram-or-not)) to the QEMU mailing list as a utility patch.
- Finalize discussions with mentors regarding the specific RAMBlock struct modifications needed for local file descriptor tracking.

**Phase 1: Basic Local Postcopy (mapped-ram + userfaultfd) [Weeks 1 - 3]**  
Goal: Implement a single-threaded instant boot, ignoring multifd entirely.  
- Intercept the migration state machine in `savevm.c` to skip the blocking RAM load loop when the fast-load flag is present.  
- Wire `postcopy-ram.c` to the local file descriptor. Implement the `pread()` offset calculation (`block->pages_offset + relative_offset`) to allow the VM to lazy-load successfully from the SSD via page faults. (Technical reference: [Section 6: Loading Snapshots](#loading-snapshots) shows the complete experimental workflow; [FAQ on address translation](#how-to-translate-a-memory-address-into-file-offset-for-pread) provides detailed offset calculations)

**Phase 2: Atomic multifd Refactoring [Weeks 4 - 6]**  
Goal: Resolve the strict incompatibility between multifd and Postcopy.  
- Refactor `multifd_ram_state_recv` to safely inject pages using atomic `qemu_ufd_copy_ioctl` operations when required, proving that multifd can operate safely under userfaultfd constraints. (See [Section 6 FAQ: multifd atomicity](#how-i-know-multifd-doesnt-work-atomically) for the code-level constraint analysis and [What's multifd?](#whats-multifd) for capability overview)

**Phase 3: The Integration (mapped-ram + userfaultfd + multifd) [Weeks 7 - 9]**  
Goal: Eliminate guest I/O stutter through background fetching.  
- Combine Phase 1 and Phase 2. Implement the background fetching loop, tying the newly atomic multifd worker threads to the remaining unread blocks in the mapped-ram file.  
- The VM runs fluidly while multifd pre-fetches the rest of the file into RAM concurrently. (Experimental validation: [Section 6: Benchmarking Results](#test-3-mapped-ram-with-multifd) demonstrates the performance characteristics; [Section 6: Live Migration Experiments](#live-migration-experiments) illustrates postcopy principles)

**Phase 4: Device State via multifd & Upstreaming [Weeks 10 - 12]**  
Goal: Handle massive device states and finalize the patch series.  
- Week 10: Extend the architecture to support parallel loading of massive device states (e.g., VFIO passthrough devices) directly from the snapshot, resolving the switchover bottleneck highlighted at KVM Forum 2024. (See [Section 6 FAQ: Why device state matters](#why-device-state-matters-and-will-get-big))
- Week 11: Implement strict error propagation. Ensure that `pread()` failures caught by `qemu_file_get_error(f)` bubble up to `process_incoming_migration_co` and trigger `exit(EXIT_FAILURE)` to prevent silent guest disk corruption. (Error handling analysis in [Section 6 FAQ: Snapshot corruption](#what-happens-if-the-snapshot-gets-corrupted-during-load))
- Week 12: Finalize documentation, clean up code, and address mailing list review feedback for the final merge.

## 5. Personal & Contact Information

- **Name**: Yash Kumar Kasaudhan  
- **University**: GLA University, Mathura (B.Tech Computer Science, Class of 2026)  
- **Location**: Gonda, Uttar Pradesh, India  
- **Email**: vididvidid@gmail.com
- **GitHub**: https://github.com/vididvidid
- **Linkedin**: https://www.linkedin.com/in/yash-kumar-kasaudhan/
- **Timezone**: IST (UTC+5:30)  
- **Commitment**: I will dedicate 35-40 hours per week to this project.  
- **Experience**: Completed a Google Summer of Code project with The Linux Foundation (OpenPrinting). Contributed code to the Microsoft Terminal repository, Meta's Buck2 build system, and PDFio.

---

## 6. Complete Research & Lab Notebook

This section contains the **complete lab experiments, benchmarking results, and technical analysis** from my snapshot research, with all external links, images, and detailed walkthroughs. It provides detailed evidence and practical implementation guides for all concepts discussed in Sections 1-5.

### Initial Setup

#### Update System Packages
```bash
sudo apt upgrade -y
sudo apt update
```

#### Install Dependencies
Just run this one command and it'll install everything you need:

```bash
sudo apt install -y \
    build-essential git ccache meson ninja-build python3-venv python3-pip \
    pkg-config libglib2.0-dev libfdt-dev libpixman-1-dev zlib1g-dev \
    libaio-dev libcapstone-dev libssh2-1-dev libvdeplug-dev
```

### Building QEMU

#### Clone and Setup
Get the QEMU source and set up a development branch:

```bash
git clone https://gitlab.com/qemu-project/qemu.git qemu-src
cd qemu-src
git submodule update --init --recursive
git worktree add ../qemu-fast-branch fast-snapshot-load
cd ../qemu-fast-branch
mkdir build-debug
```

#### Configure Build
```bash
../configure \
  --target-list=x86_64-softmmu \
  --enable-kvm \
  --enable-debug \
  --disable-docs \
  --disable-spice \
  --disable-curl \
  --enable-vnc \
  --disable-gtk \
  --prefix=$HOME/qemu-install
```

#### Compile It
```bash
make -j2
```

### Running QEMU

This section covers the practical execution of QEMU with the configurations used throughout our snapshot research. These command patterns are fundamental to all subsequent experiments documented in [Section 6: Creating Snapshots](#creating-snapshots) and [Section 6: Loading Snapshots](#loading-snapshots).

#### Basic Run
Just start the VM with KVM and you're good to go:

```bash
./qemu-fast-snapshot/build-debug/qemu-system-x86_64 \
  -enable-kvm \
  -m 2048 \
  -smp 2 \
  -cpu host \
  -drive file=kali-base.qcow2,format=qcow2,if=none,id=drive0 \
  -device virtio-blk-pci,drive=drive0 \
  -vnc :0 \
  -monitor stdio \
  -name "kali-Create-Snapshot"
```

#### With Debug Output
To see what's happening in the migration code, I added printf statements everywhere. You can redirect the output to a log file:

```bash
./qemu-fast-snapshot/build-debug/qemu-system-x86_64 \
  -enable-kvm \
  -m 2048 \
  -smp 2 \
  -cpu host \
  -drive file=kali-base.qcow2,format=qcow2,if=none,id=drive0 \
  -device virtio-blk-pci,drive=drive0 \
  -vnc :0 \
  -monitor stdio \
  -name "kali-create-snapshot" \
  2> ~/temp.log
```

#### Load Snapshot (Deferred Mode)
If you want to interact with the QEMU monitor while loading, use `-incoming defer`:

```bash
./qemu-fast-snapshot/build-debug/qemu-system-x86_64 \
  -enable-kvm \
  -m 2048 \
  -smp 2 \
  -cpu host \
  -drive file=kali-base.qcow2,format=qcow2,if=none,id=drive0 \
  -device virtio-blk-pci,drive=drive0 \
  -vnc :0 \
  -monitor stdio \
  -name "kali-create-snapshot" \
  -incoming defer \
  2> ~/temp.log
```

#### Load Snapshot (Direct)
If you want to load a snapshot file directly without the monitor, do this:

```bash
./qemu-fast-snapshot/build-debug/qemu-system-x86_64 \
  -enable-kvm \
  -m 2048 \
  -smp 2 \
  -cpu host \
  -drive file=kali-base.qcow2,format=qcow2,if=none,id=drive0 \
  -device virtio-blk-pci,drive=drive0 \
  -vnc :0 \
  -monitor stdio \
  -name "kali-create-snapshot" \
  -incoming "file:/home/kali/***.bin" \
  2> ~/temp.log
```

**Important:** Use absolute paths like `/home/kali/snapshot.bin`, not `~/snapshot.bin`. QEMU sometimes doesn't expand the tilde properly.

#### Kill It
```bash
pkill -f qemu-system-x86_64
```

### Debugging

#### Why Printf?
I had three options for debugging: GDB, QEMU's internal tracing, or printf statements. Since I was running out of time and needed to understand the code flow quickly, I just went with printf everywhere. It was faster and easier to follow what's happening.

#### Adding Print Statements Automatically
I used a bash script to add fprintf statements to all the migration files:

```bash
cd ~/qemu-fast-snapshot

for f in migration/*.c; do
  # Skip files we already modified
  [[ "$f" == migration/ram.c || \
     "$f" == migration/migration.c || \
     "$f" == migration/postcopy-live.c ]] && continue

  echo "→ Adding prints to $f"

  awk '
    /^\s*(if|while|for|else|switch|do)\s*\(/ { in_control=1; print; next }
    /)\s*$/ && !in_control { decl=1; print; next }
    decl && /^\s*{\s*$/ {
      print "{"
      print "        fprintf(stderr, \"%s: %s entered\\n\", __FILE__, __func__);"
      decl=0
      next
    }
    { print }
  ' "$f" > "/tmp/$(basename "$f").tmp" && mv "/tmp/$(basename "$f").tmp" "$f"
done
```

#### Reset Everything
To restore the files to their original state:

```bash
git checkout -- migration/ram.c migration/migration.c migration/postcopy-live.c
```

### Creating Snapshots

This section demonstrates both snapshot types discussed in the proposal: Normal Snapshots (traditional approach) and Mapped-RAM Snapshots (the optimization foundation described in [Section 3: Proposed Architecture](#3-proposed-architecture-local-demand-paging)). Understanding the mechanics of each type is essential to comprehending the bottleneck analysis in [Section 2: The Bottleneck](#2-the-bottleneck-i-o-bound-state-deserialization).

Before you start, you should know there are two types of snapshots:
- **Normal snapshot** - The standard default one (see [Section 6: Normal Snapshot](#normal-snapshot) for complete implementation guide)
- **Mapped-RAM snapshot** - You have to explicitly enable this with a command (see [Section 6: Mapped-RAM Snapshot](#mapped-ram-snapshot) for the architecture and [FAQ: How to tell if a snapshot is mapped-ram](#how-to-tell-if-a-snapshot-is-mapped-ram-or-not) for detection techniques)

#### Normal Snapshot

**Step 1:** Start QEMU
```bash
./qemu-fast-snapshot/build-debug/qemu-system-x86_64 \
  -enable-kvm \
  -m 2048 \
  -smp 2 \
  -cpu host \
  -drive file=kali-base.qcow2,format=qcow2,if=none,id=drive0 \
  -device virtio-blk-pci,drive=drive0 \
  -vnc :0 \
  -monitor stdio \
  -name "kali-create-snapshot" \
  2> ~/temp.log
```

**Step 2:** Open VNC in another terminal
```bash
vncviewer localhost:5900
```

**Step 3:** When you're ready to create the snapshot, type this in the QEMU monitor (first terminal):
```
migrate "file:/home/kali/yoursnapshotname.bin"
```

That's it, your snapshot is created.

**See it in action:** [Creating the Normal Snapshot](https://youtu.be/YwQBGTtXCYE)

#### Mapped-RAM Snapshot

**Step 1:** Start QEMU with deferred incoming
```bash
./qemu-fast-snapshot/build-debug/qemu-system-x86_64 \
  -enable-kvm \
  -m 2048 \
  -smp 2 \
  -cpu host \
  -drive file=kali-base.qcow2,format=qcow2,if=none,id=drive0 \
  -device virtio-blk-pci,drive=drive0 \
  -vnc :0 \
  -monitor stdio \
  -name "kali-create-snapshot" \
  2> ~/temp.log
```

**Step 2:** Connect via VNC
```bash
vncviewer localhost:5900
```

**Step 3:** Back in the QEMU monitor, enable mapped-RAM mode:
```
migrate_set_compatibility mapped-ram on
migrate_set_compatibility multifd on
```
(Multifd is optional - it's just for multithreading, you can skip it if you want)

**Step 4:** Create the snapshot:
```
migrate "file:/home/kali/yoursnapshotname.bin"
```

**See it in action:** [Creating the Mapped-RAM Format Snapshot](https://youtu.be/sXFQr_QbcEg)

### Loading Snapshots

This section walks through the practical loading of both snapshot types. These experiments directly validate the concepts in [Section 2: The Bottleneck](#2-the-bottleneck-i-o-bound-state-deserialization) (demonstrating why current snapshot loading is slow) and [Section 3: Proposed Architecture](#3-proposed-architecture-local-demand-paging) (showing how mapped-ram enables architectural improvements).

#### Normal Snapshot

**Step 1:** Start QEMU with deferred incoming:
```bash
./qemu-fast-snapshot/build-debug/qemu-system-x86_64 \
  -enable-kvm \
  -m 2048 \
  -smp 2 \
  -cpu host \
  -drive file=kali-base.qcow2,format=qcow2,if=none,id=drive0 \
  -device virtio-blk-pci,drive=drive0 \
  -vnc :0 \
  -monitor stdio \
  -name "kali-create-snapshot" \
  -incoming defer \
  2> ~/temp.log
```

**Step 2:** Connect via VNC:
```bash
vncviewer localhost:5900
```

**Step 3:** In the QEMU monitor, load the snapshot:
```
migrate_incoming "file:/home/kali/yoursnapshotname.bin"
```

Done, snapshot loaded.

![Normal Snapshot Loading Flow](https://raw.githubusercontent.com/vididvidid/QEMU-TODO/main/Snapshot/NormalSnapshotLoading.svg)

**Log Details:**
- [Normal Snapshot Loading Log](https://github.com/vididvidid/QEMU-TODO/blob/main/Snapshot/NormalSnapTemp.log)

#### Mapped-RAM Snapshot (without multifd)

**Step 1:** Start QEMU:
```bash
./qemu-fast-snapshot/build-debug/qemu-system-x86_64 \
  -enable-kvm \
  -m 2048 \
  -smp 2 \
  -cpu host \
  -drive file=kali-base.qcow2,format=qcow2,if=none,id=drive0 \
  -device virtio-blk-pci,drive=drive0 \
  -vnc :0 \
  -monitor stdio \
  -name "kali-create-snapshot" \
  -incoming defer \
  2> ~/temp.log
```

**Step 2:** Connect via VNC:
```bash
vncviewer localhost:5900
```

**Step 3:** In the QEMU monitor, set the flags:
```
migrate_set_compatibility mapped-ram on
migrate_set_compatibility multifd off
```

**Step 4:** Load the snapshot:
```
migrate_incoming "file:/home/kali/yoursnapshotname.bin"
```

![Mapped-RAM Snapshot Loading (No Multifd) Flow](https://raw.githubusercontent.com/vididvidid/QEMU-TODO/main/Snapshot/NoMultifdSnapshotLoading.svg)

**Log Details:**
- [Mapped-RAM Snapshot Loading Log (No Multifd)](https://github.com/vididvidid/QEMU-TODO/blob/main/Snapshot/NoMultifdSnap.log)

#### Mapped-RAM Snapshot (with multifd)

**Step 1:** Start QEMU:
```bash
./qemu-fast-snapshot/build-debug/qemu-system-x86_64 \
  -enable-kvm \
  -m 2048 \
  -smp 2 \
  -cpu host \
  -drive file=kali-base.qcow2,format=qcow2,if=none,id=drive0 \
  -device virtio-blk-pci,drive=drive0 \
  -vnc :0 \
  -monitor stdio \
  -name "kali-create-snapshot" \
  -incoming defer \
  2> ~/temp.log
```

**Step 2:** Connect via VNC:
```bash
vncviewer localhost:5900
```

**Step 3:** In the QEMU monitor, set the flags:
```
migrate_set_compatibility mapped-ram on
migrate_set_compatibility multifd on
```

**Step 4:** Load the snapshot:
```
migrate_incoming "file:/home/kali/yoursnapshotname.bin"
```

![Mapped-RAM Snapshot Loading (With Multifd) Flow](https://raw.githubusercontent.com/vididvidid/QEMU-TODO/main/Snapshot/WithMultifdSnapshotLoading.svg)

**Log Details:**
- [Mapped-RAM Snapshot Loading Log (With Multifd)](https://github.com/vididvidid/QEMU-TODO/blob/main/Snapshot/WithMultifdSnap.log)

### Benchmarking Results

The following measurements quantify the problem statement from [Section 2: The Bottleneck](#2-the-bottleneck-i-o-bound-state-deserialization) and demonstrate how mapped-ram improves upon the baseline. These results validate the ~50% improvement from traditional snapshots to mapped-ram, and reveal the multifd overhead challenges that the proposal addresses.

#### Test Setup
I ran these tests on my setup:
- 4GB RAM
- 128GB SSD
- Running Kali Linux as the VM

#### How I Did It
The benchmarking was done manually with VNC - I'd visually watch the screen load and close it myself. So yeah, the numbers aren't super precise since VNC takes time to load and closing is on human speed. But I ran each test 5 times and averaged them out, so the results should be pretty consistent.

You can find the benchmark script in the repo and there's also a YouTube video where I recorded the whole process.

**Tools:**
- [benchmark.py](https://github.com/vididvidid/QEMU-TODO/blob/main/utils/utils/benchmark.py) - Script to run and measure snapshot performance

#### Test 1: Normal Snapshot
```
Mean                  : 18.459 s
Median                : 18.018 s
Std Dev               : 0.959 s
Min / Max             : 17.797 s / 20.098 s
95% Confidence        : 18.459 ± 0.841 s
Coefficient of Var    : 5.20%
Successful runs       : 5/5
```

![Normal Snapshot Benchmark](https://github.com/vididvidid/QEMU-TODO/blob/main/Snapshot/BenchmarkNormalSnapshotLoading.png)

#### Test 2: Mapped-RAM (without multifd)
```
Mean                  : 9.226 s
Median                : 9.284 s
Std Dev               : 0.156 s
Min / Max             : 9.037 s / 9.376 s
95% Confidence        : 9.226 ± 0.137 s
Coefficient of Var    : 1.69%
Successful runs       : 5/5
```

![Mapped-RAM Snapshot Without Multifd Benchmark](https://github.com/vididvidid/QEMU-TODO/blob/main/Snapshot/BenchmarkMappedRamSnapshotWithoutMultifdLoading.png)

#### Test 3: Mapped-RAM (with multifd)
```
Mean                  : 13.526 s
Median                : 13.484 s
Std Dev               : 0.227 s
Min / Max             : 13.269 s / 13.837 s
95% Confidence        : 13.526 ± 0.199 s
Coefficient of Var    : 1.68%
Successful runs       : 5/5
```

![Mapped-RAM Snapshot With Multifd Benchmark](https://github.com/vididvidid/QEMU-TODO/blob/main/Snapshot/BenchmarkMappedRamSnapshotWithMultifdLoading.png)

#### What I Noticed
So you'll see that Test 2 is actually faster than Test 3, even though multifd should theoretically be better. But this is probably because of a bug in my code or some inconsistency in how postcopy works with multifd. The thing is, when VNC loads, it lags sometimes and the screen doesn't show up instantly. Since postcopy is so fast, the VM starts before VNC even displays anything, so it takes extra time for me to see the login screen and close the window. 

You can check out the YouTube video I uploaded to see this happening in real time. That'll make it clearer why the timings are different from what you'd expect.

**Watch the benchmark in action:** [Benchmarking of Snapshot Performance](https://youtu.be/HKbD-1bTRb8)

### Live Migration Experiments

The following demonstrate two migration approaches: **Precopy** (traditional state-of-the-art) and **Postcopy** (the userfaultfd-based approach). These experiments are foundational to understanding the "Local Postcopy" architecture proposed in [Section 3: Proposed Architecture](#3-proposed-architecture-local-demand-paging), and they show how VM resumption can occur before all memory is transferred.

I decided to explore two different migration approaches to understand how the snapshot technology actually works. Both have videos showing what's happening in real time.

#### Before You Start
First, enable the kernel feature that postcopy needs to hijack memory:
```bash
sudo sysctl -w vm.unprivileged_userfaultfd=1
```

#### Setting Up Two VMs

You'll need to run QEMU twice - one as the source (sender) and one as the destination (receiver). They'll talk to each other over TCP.

**Terminal 1 - Destination (waits to receive)**
```bash
./qemu-install/bin/qemu-system-x86_64 -m 256 -name "Dest-VM" -vnc :1 \
  -incoming tcp:127.0.0.1:4444 -monitor telnet:127.0.0.1:4446,server,nowait \
  > /tmp/dest.log 2>&1
```

**Terminal 2 - Source (sends everything)**
```bash
./qemu-install/bin/qemu-system-x86_64 -m 256 -name "Source-VM" -vnc :0 \
  -monitor telnet:127.0.0.1:4445,server,nowait > /tmp/source.log 2>&1
```

#### The Heartbeat Script
Once the source VM boots, run this inside to generate memory changes you can watch:
```bash
while true; do 
    head -c 100M /dev/urandom > /dev/shm/dirty_data
    echo "Dirtied 100MB RAM at $(date +%T)"
    sleep 0.1
done
```

#### Approach 1: Precopy Migration

**How it works:** The source VM keeps running while QEMU sends RAM pages to the destination. Once everything is transferred, the source pauses and the destination wakes up.

In the source monitor (`nc 127.0.0.1 4445`), run:
```
migrate_set_parameter max-bandwidth 1M
migrate -d tcp:127.0.0.1:4444
```

You'll see the source keep printing while the destination screen stays black. Once the RAM is almost completely sent, the source freezes and the destination instantly lights up.

**The code that runs:**
- `ram_save_iterate` (in `ram.c`) - loops through memory sending pages
- `migration_completion` - pauses the source when done

**Speed it up:** If precopy is taking forever, you can increase the downtime limit. By default it's 300ms, but you can set it to 10 seconds if you're just trying to record it:
```
(qemu) migrate_set_parameter downtime-limit 10000
```

**Videos:**
- [Precopy Live Migration Failed (Network Too Slow)](https://youtu.be/XZrDKEk64Qc)
- [Precopy Live Migration with Downtime Limit](https://youtu.be/y0oNx5YOHOE)

![Precopy Migration Flow](https://raw.githubusercontent.com/vididvidid/QEMU-TODO/main/Migration/precopyMigration.svg)

**Log Details:**
- [Precopy Migration - Source Log](https://github.com/vididvidid/QEMU-TODO/blob/main/Migration/source_precopy.log)
- [Precopy Migration - Destination Log](https://github.com/vididvidid/QEMU-TODO/blob/main/Migration/destination_precopy.log)

#### Approach 2: Postcopy Migration

**How it works:** This is where the magic happens. The source pauses immediately, CPU state moves to the destination, and the destination wakes up with zero RAM. When the destination tries to read a memory page, it pulls it from the source on-demand.

**Order matters here - do this exactly:**

1. In destination monitor (`nc 127.0.0.1 4446`):
```
migrate_set_capability postcopy-ram on
```

2. In source monitor (`nc 127.0.0.1 4445`):
```
migrate_set_capability postcopy-ram on
migrate_set_parameter max-bandwidth 1M
migrate -d tcp:127.0.0.1:4444
migrate_start_postcopy
```

**Watch it in action:** [Postcopy Live Migration](https://youtu.be/qoGIOb3z9Ic)

![Postcopy Migration Flow](https://raw.githubusercontent.com/vididvidid/QEMU-TODO/main/Migration/postcopyMigration.svg)

**Log Details:**
- [Postcopy Migration - Source Log](https://github.com/vididvidid/QEMU-TODO/blob/main/Migration/source_postcopy.log)
- [Postcopy Migration - Destination Log](https://github.com/vididvidid/QEMU-TODO/blob/main/Migration/destination_postcopy.log)

#### What This All Means

What you're looking at is the exact engine behind your snapshot technology. Your project is basically **local postcopy** - instead of pulling pages over TCP from another VM, your code pulls them from a file on the SSD. Same concept, different transport.

I used netcat (`nc`) here even though it wasn't strictly necessary, just wanted to try it out and see how it worked.

#### Recording Videos

Since OBS was lagging on my system, I used ffmpeg to record instead:
```bash
ffmpeg -video_size 1920x1080 -framerate 30 -f x11grab -i :0.0 -c:v libx264 \
  -preset ultrafast -crf 30 /tmp/migration-demo.mp4
```

### Minimalist Live Migration Lab Setup

#### Why Alpine Linux?

My machine has 4GB of RAM total. If I ran two Kali Linux VMs and gave each 2GB, my host would have nothing left and crash. So instead, I used Alpine Linux - it's super lightweight and uses less than 1GB total for both VMs. Perfect for doing these migration experiments without killing the system.

#### The General Idea

The whole setup works like this:
- **Source VM** - The one that's running and doing stuff
- **Destination VM** - Waits around with the `-incoming` flag until the source sends its state
- **TCP Connection** - They talk to each other over localhost on port 4444
- **Monitors** - Two telnet connections let you control each VM without messing up the logs
- **VNC** - Watch what's happening on each screen

#### Getting Started

First, download Alpine (it's tiny - only about 50MB):
```bash
wget https://dl-cdn.alpinelinux.org/alpine/v3.19/releases/x86_64/alpine-virt-3.19.1-x86_64.iso
```

#### Precopy Gotcha

When I ran precopy migration, it was taking forever. If you run into the same thing, you can just tell QEMU to not care about the VM freezing for a bit. The default is 300ms, but I bumped it up to 10 seconds so I could actually record what was happening:

```
(qemu) migrate_set_parameter downtime-limit 10000
```

This just makes precopy fast enough to see what's going on without waiting forever.

### FAQs from my experiments

This FAQ section provides deep technical answers to implementation questions that arise when working with the snapshot and migration concepts discussed throughout the proposal. These explain the architectural constraints, technical mechanisms, and design decisions referenced in [Sections 1-5](#1-abstract--executive-summary), with particular emphasis on:

- **Address Translation** ([Section 3.1](#31-translating-page-faults-to-file-offsets-ramc)) - How to map page faults to disk offsets
- **multifd Atomicity** ([Section 3.2](#32-concurrency--the-multifd-challenge)) - Why postcopy and multifd conflict and how to resolve it
- **Device State** ([Section 4 Phase 4](#phase-4-device-state-via-multifd--upstreaming)) - Enterprise-scale VFIO state handling
- **Error Handling** ([Section 4 Week 11](#phase-4-device-state-via-multifd--upstreaming)) - Snapshot corruption detection and recovery

#### How normal snapshots load (not mapped-ram)

The file is stored sequentially - like you've got ram1, ram2, ram3, etc. You have to load them one after the other, which takes time. That's the whole problem mapped-ram solved - you can access the RAM blocks in parallel instead of waiting for each one. Check [the QEMU migration docs](https://gitlab.com/qemu-project/qemu/-/blob/master/docs/devel/migration/mapped-ram.rst) for more details on the structure. For the architectural implications, see [Section 2: The Bottleneck](#2-the-bottleneck-i-o-bound-state-deserialization) and [Section 3.1: Translating Page Faults](#31-translating-page-faults-to-file-offsets-ramc).

#### How to tell if a snapshot is mapped-ram or not

**Three ways:**

1. Try loading it with mapped-ram off - if it throws an error, it's a mapped-ram file
2. I created a Python script that'll check for you and tell you what type it is: [check_mapped_ram.py](https://github.com/vididvidid/QEMU-TODO/blob/main/utils/utils/check_mapped_ram.py)
3. Mapped-ram files have three things internally:
   - "mapped-ram" written in binary (can't see it with vim)
   - A flag called `RAM_SAVE_FLAG_MAPPED_RAM`
   - Ramblock headers

#### The order snapshots load things

Normally it goes: RAM data first, then device state. That's what normal snapshots and precopy do. But with postcopy live migration, it's flipped - device states load first, then the data comes on-demand.

For my snapshot implementation right now, the RAM data comes first in the file.

#### Do you really need to load everything?

Yeah, you do. The question was like - if I have two terminals open and a calculator, but the user is only using the terminals, why load the calculator data?

The answer is you gotta load it all anyway. There's a background thread that tracks what's been loaded. If you don't, you become totally dependent on the source VM and can't disconnect. For example, if someone doesn't use the calculator for 2 months and then suddenly opens it, and you already disconnected from the source - boom, that data is gone and you get an error. So we load everything upfront.

#### What's multifd?

Multifd is basically QEMU's multithreading system for moving data. It only handles RAM data transfers, not device states.

**What it does well:**

- **Parallel RAM Transfer** - Instead of sending all 64GB through one network socket, multifd splits it across multiple connections and sends them all at once
- **Parallel Compression** - If you're using zlib or zstd, it compresses memory pages at the same time instead of one at a time
- **Zero-Copy Networking** - Passes memory pointers straight to the kernel's network stack instead of copying things back and forth
- **File Snapshots** - Works with mapped-ram files too, writing directly to disk super fast
- **Encrypted Migrations** - Built-in TLS support for sending data over untrusted networks

**Where it falls short:**

- **Breaks with Postcopy** - It's not compatible. Multifd blasts data as fast as possible, but postcopy needs atomic updates (using userfaultfd). Mix them and you corrupt guest memory
- **Only Handles RAM** - Virtual hardware states (like CPUs or graphics cards) still go through a single thread, so the VMs freeze longer than necessary
- **Main Thread Bottleneck** - The multifd threads can only send what the main thread gives them. If you've got a huge dirty bitmap, the main thread has to scan it sequentially and the multifd threads just wait around idle
- **Double-Copy on Receive** - On the destination side, data goes into a temporary buffer first, then to guest memory. That's wasting CPU cycles

#### Why I didn't use QMP commands

Honestly, I learned about QMP later and felt more comfortable just using the QEMU terminal. But if you do use it, remember to first set the capability with:
```
{"execute":"qmp_capabilities"}
{"execute": "migrate", "arguments": {"uri": "file:/home/yash/kali_normal_snapshot.bin"}}
```

#### Why postcopy and multifd can't work together yet

Postcopy requires atomic updates on memory pages using userfaultfd, but multifd is designed to blast data directly into memory buffers without that atomic guarantee. So if you use them together right now, you'd corrupt guest memory.

That means we need to modify multifd to work atomically before they can actually work together. Check the [QEMU todo list](https://wiki.qemu.org/ToDo/LiveMigration#Multifd+Postcopy) for more details.

#### How I cleaned up the log files

I wrote a Python script to remove duplicate printf statements from the logs. Vim's `uniq` command helped too. Once I got most of the duplicates out, visual inspection of what remained helped me eliminate even more noise.

**Tool:** [clean.py](https://github.com/vididvidid/QEMU-TODO/blob/main/utils/utils/clean.py) - Script to deduplicate printf statements from log files

#### My project roadmap

Here's the order I'm planning to work on this:

1. **First** - Implement basic mapped-ram + userfaultfd (forget about multifd for now)
2. **Second** - Modify multifd to work atomically with userfaultfd
3. **Third** - Combine all three: mapped-ram + userfaultfd + multifd
4. **Fourth** - Extend multifd to handle device state too (not just RAM)

#### How I know multifd doesn't work atomically

The [QEMU documentation](https://wiki.qemu.org/ToDo/LiveMigration#Multifd+Postcopy) says it directly:

> Currently the two features are not compatible, due to the fact that postcopy requires atomically update on the pages, while multifd is so far designed to receive pages directly into guest memory buffers.

And looking at the code in multifd.c (https://gitlab.com/qemu-project/qemu/-/blob/master/migration/multifd.c?ref_type=heads#L1398), there's even a comment and assertion that says:

```c
if (has_data) {
    /*
     * multifd thread should not be active and receive data
     * when migration is in the Postcopy phase. Two threads
     * writing the same memory area could easily corrupt
     * the guest state.
     */
    assert(!migration_in_postcopy());
    if (is_device_state) {
        assert(use_packets);
        ret = multifd_device_state_recv(p, &local_err);
    } else {
        ret = multifd_ram_state_recv(p, &local_err);
    }
    if (ret != 0) {
        break;
    }
}
```

That assertion is basically saying "if we're in postcopy, don't even let multifd try to write."

#### Why device state matters and will get big

Standard emulated devices (like normal virtual hard drives) have tiny state sizes that fit easily on the main migration channel. But VFIO passthrough devices (like vGPUs and SmartNICs) accumulate massive state during stop-and-copy. As [highlighted at KVM Forum 2024](https://lists.gnu.org/archive/html/qemu-devel/2025-01/msg05916.html), sending all that device state through a single channel creates a huge bottleneck during switchover.

By extending our architecture to support multifd device state transfer (new in QEMU 10.0), we can parallelize both RAM and massive device state loading directly from the snapshot file. This is the central objective of [Section 4: Phase 4: Device State via multifd & Upstreaming](#phase-4-device-state-via-multifd--upstreaming), which addresses this critical enterprise use case.

#### How QEMU guarantees multifd only reads RAM from mapped-ram

QEMU uses a strict delegation model and hardcoded assertions to ensure multifd threads only load RAM from a mapped-ram file - nothing else.

**The "No Packets" Rule:**

When mapped-ram is enabled, multifd disables packet headers entirely (https://gitlab.com/qemu-project/qemu/-/blob/master/migration/multifd.c?ref_type=heads#L139):

```c
static bool multifd_use_packets(void)
{
    return !migrate_mapped_ram();
}
```

**The Main Thread is the Director:**

Since the multifd thread doesn't parse headers, it doesn't know what data it's supposed to read. Instead, it waits for the main migration thread to explicitly tell it what to do by reading offsets from the primary migration stream and handing it precise file locations (see https://gitlab.com/qemu-project/qemu/-/blob/master/migration/multifd.c?ref_type=heads#L1372).

**The Hard Guard:**

Before any read operation, there's an assertion that prevents device state processing:

```c
if (is_device_state) {
    assert(use_packets); /* Device state requires packet headers */
    ret = multifd_device_state_recv(p, &local_err);
} else {
    ret = multifd_ram_state_recv(p, &local_err);
}
```

Since `use_packets` is false in mapped-ram mode, if the thread somehow thought it was device state (which it shouldn't be), the assertion would crash immediately. The `is_device_state` flag defaults to false and is never set to true because header parsing is skipped. So it safely falls through to RAM-only reading.

#### How to translate a memory address into file offset for pread()

When userfaultfd catches a page fault and gives you the Host Virtual Address (HVA), you need to figure out exactly where that data is in your snapshot file on disk. It's a two-step process using QEMU's RAMBlock architecture.

**Step 1: HVA to RAMBlock and Offset**

The userfaultfd handler gives you a raw memory address. QEMU has an API in ram.c called `poll_fault_page()` that converts this into a RAMBlock and a relative offset:

```c
/* From poll_fault_page() in ram.c */
https://gitlab.com/qemu-project/qemu/-/blob/master/migration/ram.c?ref_type=heads#L4172
page_address = (void *)(uintptr_t) uffd_msg.arg.pagefault.address;
block = qemu_ram_block_from_host(page_address, false, offset);
```

So now you know which RAM chip the address belongs to and the byte offset within that chip.

**Step 2: Add the File Header Offset**

When mapped-ram is enabled, each RAMBlock has a `pages_offset` value - that's where the actual page data starts in the file (after all the headers). This gets parsed and stored in ram.c:

```c
/* From parse_ramblock_mapped_ram() in ram.c */
https://gitlab.com/qemu-project/qemu/-/blob/master/migration/ram.c?ref_type=heads#L4172
block->pages_offset = header.pages_offset;
```

**The Final Formula**

To get your absolute file offset for `pread()`, you just add them together:

```c
/* From read_ramblock_mapped_ram() in ram.c */
https://gitlab.com/qemu-project/qemu/-/blob/master/migration/ram.c?ref_type=heads#L4128
read = qemu_get_buffer_at(f, host, size, block->pages_offset + offset);
```

So: **file_offset = block->pages_offset + offset**

#### What happens if the snapshot gets corrupted during load?

If your snapshot file gets corrupted, deleted, or becomes unreadable while QEMU is restoring it, the VM just crashes. QEMU doesn't try to reboot or recover - it terminates immediately. Why? Because if the memory pages are missing or corrupted, the guest OS will silently corrupt its own data on the virtual hard drive, which is way worse than a clean crash.

Here's how the code enforces this:

**Step 1: Error Detection**

As QEMU loads pages in `ram_load_postcopy()`, it constantly checks the file handle for errors:

```c
/* From ram_load_postcopy() in ram.c */
https://gitlab.com/qemu-project/qemu/-/blob/master/migration/ram.c?ref_type=heads#L3951
if (!ret && qemu_file_get_error(f)) {
    ret = qemu_file_get_error(f);
}
```

**Step 2: Error Propagates**

If an error is detected, it bubbles up through the migration coroutine:

```c
/* From process_incoming_migration_co() in migration.c */
ret = qemu_loadvm_state(mis->from_src_file, &local_err);
// ...
if (ret < 0) {
    error_prepend(&local_err, "load of migration failed: %s: ", strerror(-ret));
    goto fail;
}
```

**Step 3: VM Terminated**

Finally, at the fail label, QEMU sets the migration state to FAILED and checks the `exit_on_error` flag. If it's set to true (which is the default), the whole process exits immediately:

```c
/* From process_incoming_migration_co() in migration.c */
https://gitlab.com/qemu-project/qemu/-/blob/master/migration/migration.c?ref_type=heads#L791
fail:
    migrate_set_state(&mis->state, MIGRATION_STATUS_ACTIVE,
                      MIGRATION_STATUS_FAILED);
    migrate_error_propagate(s, local_err);
    migration_incoming_state_destroy();

    if (mis->exit_on_error) {
        WITH_QEMU_LOCK_GUARD(&s->error_mutex) {
            error_report_err(s->error);
            s->error = NULL;
        }

        exit(EXIT_FAILURE); /* The VM is immediately terminated */
    }
```

So the VM doesn't try to be clever - it just exits with EXIT_FAILURE.

### Videos

The following videos document the practical execution of all the experimental methodologies described in this lab notebook. These complement the written sections with real-time demonstrations of:

- **Snapshot Performance** - Visual proof of the bottleneck ([Section 2](#2-the-bottleneck-i-o-bound-state-deserialization)) and improvements from mapped-ram
- **Snapshot Creation** - Practical walkthrough of both normal ([Section 6: Creating Snapshots - Normal Snapshot](#normal-snapshot)) and mapped-RAM ([Section 6: Creating Snapshots - Mapped-RAM](#mapped-ram-snapshot)) approaches
- **Live Migration** - Demonstration of precopy and postcopy principles ([Section 3: Proposed Architecture](#3-proposed-architecture-local-demand-paging)) and [Section 6: Live Migration Experiments](#live-migration-experiments))

Here are all the videos from my experiments:

1. **[Benchmarking of Snapshot Performance](https://youtu.be/HKbD-1bTRb8)** - Performance comparison of all three snapshot types
2. **[Creating the Normal Snapshot](https://youtu.be/YwQBGTtXCYE)** - Step-by-step walkthrough of creating a normal snapshot
3. **[Creating the Mapped-RAM Format Snapshot](https://youtu.be/sXFQr_QbcEg)** - How to create a mapped-RAM snapshot
4. **[Precopy Live Migration Failed (Network Too Slow)](https://youtu.be/XZrDKEk64Qc)** - What happens when the network is too slow for precopy
5. **[Precopy Live Migration with Downtime Limit](https://youtu.be/y0oNx5YOHOE)** - Precopy migration with the downtime-limit parameter set
6. **[Postcopy Live Migration](https://youtu.be/qoGIOb3z9Ic)** - Live migration using postcopy method

---
