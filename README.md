# QEMU Fast Snapshot Setup

## Table of Contents
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
  - [How normal snapshots load (not mapped-ram)](#how-normal-snapshots-load-not-mapped-ram)
  - [How to tell if a snapshot is mapped-ram or not](#how-to-tell-if-a-snapshot-is-mapped-ram-or-not)
  - [The order snapshots load things](#the-order-snapshots-load-things)
  - [Do you really need to load everything?](#do-you-really-need-to-load-everything)
  - [What's multifd?](#whats-multifd)
  - [Why I didn't use QMP commands](#why-i-didnt-use-qmp-commands)
  - [Why postcopy and multifd can't work together yet](#why-postcopy-and-multifd-cant-work-together-yet)
  - [How I cleaned up the log files](#how-i-cleaned-up-the-log-files)
  - [My project roadmap](#my-project-roadmap)
  - [How I know multifd doesn't work atomically](#how-i-know-multifd-doesnt-work-atomically)
  - [Why device state matters and will get big](#why-device-state-matters-and-will-get-big)
  - [How QEMU guarantees multifd only reads RAM from mapped-ram](#how-qemu-guarantees-multifd-only-reads-ram-from-mapped-ram)
  - [How to translate a memory address into file offset for pread()](#how-to-translate-a-memory-address-into-file-offset-for-pread)
  - [What happens if the snapshot gets corrupted during load?](#what-happens-if-the-snapshot-gets-corrupted-during-load)
- [Videos](#videos)

---

## Initial Setup

### Update System Packages
```bash
sudo apt upgrade -y
sudo apt update
```

### Install Dependencies
Just run this one command and it'll install everything you need:

```bash
sudo apt install -y \
    build-essential git ccache meson ninja-build python3-venv python3-pip \
    pkg-config libglib2.0-dev libfdt-dev libpixman-1-dev zlib1g-dev \
    libaio-dev libcapstone-dev libssh2-1-dev libvdeplug-dev
```

## Building QEMU

### Clone and Setup
Get the QEMU source and set up a development branch:

```bash
git clone https://gitlab.com/qemu-project/qemu.git qemu-src
cd qemu-src
git submodule update --init --recursive
git worktree add ../qemu-fast-branch fast-snapshot-load
cd ../qemu-fast-branch
mkdir build-debug
```

### Configure Build
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

### Compile It
```bash
make -j2
```

## Running QEMU

### Basic Run
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

### With Debug Output
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

### Load Snapshot (Deferred Mode)
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

### Load Snapshot (Direct)
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

### Kill It
```bash
pkill -f qemu-system-x86_64
```

## Debugging

### Why Printf?
I had three options for debugging: GDB, QEMU's internal tracing, or printf statements. Since I was running out of time and needed to understand the code flow quickly, I just went with printf everywhere. It was faster and easier to follow what's happening.

### Adding Print Statements Automatically
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

### Reset Everything
To restore the files to their original state:

```bash
git checkout -- migration/ram.c migration/migration.c migration/postcopy-live.c
```


## Creating Snapshots

Before you start, you should know there are two types of snapshots:
- **Normal snapshot** - The standard default one
- **Mapped-RAM snapshot** - You have to explicitly enable this with a command

### Normal Snapshot

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

### Mapped-RAM Snapshot

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

## Loading Snapshots

### Normal Snapshot

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

### Mapped-RAM Snapshot (without multifd)

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

### Mapped-RAM Snapshot (with multifd)

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


## Benchmarking Results

### Test Setup
I ran these tests on my setup:
- 4GB RAM
- 128GB SSD
- Running Kali Linux as the VM

### How I Did It
The benchmarking was done manually with VNC - I'd visually watch the screen load and close it myself. So yeah, the numbers aren't super precise since VNC takes time to load and closing is on human speed. But I ran each test 5 times and averaged them out, so the results should be pretty consistent.

You can find the benchmark script in the repo and there's also a YouTube video where I recorded the whole process.

### Test 1: Normal Snapshot
```
Mean                  : 18.459 s
Median                : 18.018 s
Std Dev               : 0.959 s
Min / Max             : 17.797 s / 20.098 s
95% Confidence        : 18.459 ± 0.841 s
Coefficient of Var    : 5.20%
Successful runs       : 5/5
```

### Test 2: Mapped-RAM (without multifd)
```
Mean                  : 9.226 s
Median                : 9.284 s
Std Dev               : 0.156 s
Min / Max             : 9.037 s / 9.376 s
95% Confidence        : 9.226 ± 0.137 s
Coefficient of Var    : 1.69%
Successful runs       : 5/5
```

### Test 3: Mapped-RAM (with multifd)
```
Mean                  : 13.526 s
Median                : 13.484 s
Std Dev               : 0.227 s
Min / Max             : 13.269 s / 13.837 s
95% Confidence        : 13.526 ± 0.199 s
Coefficient of Var    : 1.68%
Successful runs       : 5/5
```

### What I Noticed
So you'll see that Test 2 is actually faster than Test 3, even though multifd should theoretically be better. But this is probably because of a bug in my code or some inconsistency in how postcopy works with multifd. The thing is, when VNC loads, it lags sometimes and the screen doesn't show up instantly. Since postcopy is so fast, the VM starts before VNC even displays anything, so it takes extra time for me to see the login screen and close the window. 

You can check out the YouTube video I uploaded to see this happening in real time. That'll make it clearer why the timings are different from what you'd expect.

**Watch the benchmark in action:** [Benchmarking of Snapshot Performance](https://youtu.be/HKbD-1bTRb8)

## Live Migration Experiments

I decided to explore two different migration approaches to understand how the snapshot technology actually works. Both have videos showing what's happening in real time.

### Before You Start
First, enable the kernel feature that postcopy needs to hijack memory:
```bash
sudo sysctl -w vm.unprivileged_userfaultfd=1
```

### Setting Up Two VMs

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

### The Heartbeat Script
Once the source VM boots, run this inside to generate memory changes you can watch:
```bash
while true; do 
    head -c 100M /dev/urandom > /dev/shm/dirty_data
    echo "Dirtied 100MB RAM at $(date +%T)"
    sleep 0.1
done
```

### Approach 1: Precopy Migration

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

### Approach 2: Postcopy Migration

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

### What This All Means

What you're looking at is the exact engine behind your snapshot technology. Your project is basically **local postcopy** - instead of pulling pages over TCP from another VM, your code pulls them from a file on the SSD. Same concept, different transport.

I used netcat (`nc`) here even though it wasn't strictly necessary, just wanted to try it out and see how it worked.

### Recording Videos

Since OBS was lagging on my system, I used ffmpeg to record instead:
```bash
ffmpeg -video_size 1920x1080 -framerate 30 -f x11grab -i :0.0 -c:v libx264 \
  -preset ultrafast -crf 30 /tmp/migration-demo.mp4
```


## Minimalist Live Migration Lab Setup

### Why Alpine Linux?

My machine has 4GB of RAM total. If I ran two Kali Linux VMs and gave each 2GB, my host would have nothing left and crash. So instead, I used Alpine Linux - it's super lightweight and uses less than 1GB total for both VMs. Perfect for doing these migration experiments without killing the system.

### The General Idea

The whole setup works like this:
- **Source VM** - The one that's running and doing stuff
- **Destination VM** - Waits around with the `-incoming` flag until the source sends its state
- **TCP Connection** - They talk to each other over localhost on port 4444
- **Monitors** - Two telnet connections let you control each VM without messing up the logs
- **VNC** - Watch what's happening on each screen

### Getting Started

First, download Alpine (it's tiny - only about 50MB):
```bash
wget https://dl-cdn.alpinelinux.org/alpine/v3.19/releases/x86_64/alpine-virt-3.19.1-x86_64.iso
```

### Precopy Gotcha

When I ran precopy migration, it was taking forever. If you run into the same thing, you can just tell QEMU to not care about the VM freezing for a bit. The default is 300ms, but I bumped it up to 10 seconds so I could actually record what was happening:

```
(qemu) migrate_set_parameter downtime-limit 10000
```

This just makes precopy fast enough to see what's going on without waiting forever.

---

## FAQs from my experiments

### How normal snapshots load (not mapped-ram)

The file is stored sequentially - like you've got ram1, ram2, ram3, etc. You have to load them one after the other, which takes time. That's the whole problem mapped-ram solved - you can access the RAM blocks in parallel instead of waiting for each one. Check [the QEMU migration docs](https://gitlab.com/qemu-project/qemu/-/blob/master/docs/devel/migration/mapped-ram.rst) for more details on the structure.

### How to tell if a snapshot is mapped-ram or not

**Three ways:**

1. Try loading it with mapped-ram off - if it throws an error, it's a mapped-ram file
2. I created a Python script that'll check for you and tell you what type it is
3. Mapped-ram files have three things internally:
   - "mapped-ram" written in binary (can't see it with vim)
   - A flag called `RAM_SAVE_FLAG_MAPPED_RAM`
   - Ramblock headers

### The order snapshots load things

Normally it goes: RAM data first, then device state. That's what normal snapshots and precopy do. But with postcopy live migration, it's flipped - device states load first, then the data comes on-demand.

For my snapshot implementation right now, the RAM data comes first in the file.

### Do you really need to load everything?

Yeah, you do. The question was like - if I have two terminals open and a calculator, but the user is only using the terminals, why load the calculator data?

The answer is you gotta load it all anyway. There's a background thread that tracks what's been loaded. If you don't, you become totally dependent on the source VM and can't disconnect. For example, if someone doesn't use the calculator for 2 months and then suddenly opens it, and you already disconnected from the source - boom, that data is gone and you get an error. So we load everything upfront.

### What's multifd?

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

### Why I didn't use QMP commands

Honestly, I learned about QMP later and felt more comfortable just using the QEMU terminal. But if you do use it, remember to first set the capability with:
```
{"execute":"qmp_capabilities"}
{"execute": "migrate", "arguments": {"uri": "file:/home/yash/kali_normal_snapshot.bin"}}
```

### Why postcopy and multifd can't work together yet

Postcopy requires atomic updates on memory pages using userfaultfd, but multifd is designed to blast data directly into memory buffers without that atomic guarantee. So if you use them together right now, you'd corrupt guest memory.

That means we need to modify multifd to work atomically before they can actually work together. Check the [QEMU todo list](https://wiki.qemu.org/ToDo/LiveMigration#Multifd+Postcopy) for more details.

### How I cleaned up the log files

I wrote a Python script to remove duplicate printf statements from the logs. Vim's `uniq` command helped too. Once I got most of the duplicates out, visual inspection of what remained helped me eliminate even more noise.

(Python script available at: [link to clean.py])

### My project roadmap

Here's the order I'm planning to work on this:

1. **First** - Implement basic mapped-ram + userfaultfd (forget about multifd for now)
2. **Second** - Modify multifd to work atomically with userfaultfd
3. **Third** - Combine all three: mapped-ram + userfaultfd + multifd
4. **Fourth** - Extend multifd to handle device state too (not just RAM)

### How I know multifd doesn't work atomically

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

### Why device state matters and will get big

Standard emulated devices (like normal virtual hard drives) have tiny state sizes that fit easily on the main migration channel. But VFIO passthrough devices (like vGPUs and SmartNICs) accumulate massive state during stop-and-copy. As [highlighted at KVM Forum 2024](https://lists.gnu.org/archive/html/qemu-devel/2025-01/msg05916.html), sending all that device state through a single channel creates a huge bottleneck during switchover.

By extending our architecture to support multifd device state transfer (new in QEMU 10.0), we can parallelize both RAM and massive device state loading directly from the snapshot file.

### How QEMU guarantees multifd only reads RAM from mapped-ram

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



### How to translate a memory address into file offset for pread()

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

### What happens if the snapshot gets corrupted during load?

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

---

## Videos

Here are all the videos from my experiments:

1. **[Benchmarking of Snapshot Performance](https://youtu.be/HKbD-1bTRb8)** - Performance comparison of all three snapshot types
2. **[Creating the Normal Snapshot](https://youtu.be/YwQBGTtXCYE)** - Step-by-step walkthrough of creating a normal snapshot
3. **[Creating the Mapped-RAM Format Snapshot](https://youtu.be/sXFQr_QbcEg)** - How to create a mapped-RAM snapshot
4. **[Precopy Live Migration Failed (Network Too Slow)](https://youtu.be/XZrDKEk64Qc)** - What happens when the network is too slow for precopy
5. **[Precopy Live Migration with Downtime Limit](https://youtu.be/y0oNx5YOHOE)** - Precopy migration with the downtime-limit parameter set
6. **[Postcopy Live Migration](https://youtu.be/qoGIOb3z9Ic)** - Live migration using postcopy method
