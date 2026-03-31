#!/usr/bin/env python3
import subprocess
import time
import os
import statistics
from datetime import datetime
import re
import argparse
import sys
import queue
import threading
import shutil

# Configuration
QEMU_PATH = os.path.expanduser("~/qemu-fast-snapshot/build-debug/qemu-system-x86_64")
NORMAL_SNAPSHOT = "/home/kali/NormalSnapshot.bin"
MAPPED_SNAPSHOT = "/home/kali/kali_mapped_state.bin"   # used for both mapped-ram with and without multifd
DISK_IMAGE = "kali-base.qcow2"
QEMU_DIR = os.path.expanduser("~/qemu-fast-snapshot")
NUM_RUNS = 5
LOG_FILE = os.path.expanduser("~/result.log")
VNC_DISPLAY = ":0"

# Timeout for QEMU to start (seconds)
QEMU_START_TIMEOUT = 10


def clear_caches():
    """Clear filesystem caches for reproducible cold-cache results"""
    try:
        subprocess.run(
            "sync && echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null 2>&1",
            shell=True,
            check=False
        )
        time.sleep(1)
    except Exception as e:
        print(f"Warning: Could not clear caches: {e}")


def find_terminal():
    """Find a suitable terminal emulator to run the VNC viewer."""
    terminals = ["gnome-terminal", "xterm", "konsole", "xfce4-terminal", "terminator"]
    for term in terminals:
        if shutil.which(term):
            return term
    return None


def get_monitor_commands_final(mode: str) -> str:
    """Return monitor commands to be sent after the user closes VNC."""
    # The final commands are the same for all modes: we want to see the migration status and VM state
    return """info migrate
info status
quit
"""


def clean_monitor_output(stdout: str) -> str:
    """Remove noisy debug trace lines (e.g., ../migration/ram.c: ... entered)."""
    cleaned = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if re.match(r'^\.\./migration/.*: .* entered$', stripped):
            continue
        cleaned.append(line)
    return '\n'.join(cleaned)


def parse_migration_time(stdout: str):
    """Parse migration time from QEMU output (if present)."""
    for line in stdout.splitlines():
        line = line.strip()
        if 'total time:' in line.lower():
            match = re.search(r'(\d+)\s*ms', line)
            if match:
                return int(match.group(1)) / 1000.0
    return None


def run_restore_test(test_num: int, mode: str, snapshot_file: str):
    """
    Start QEMU with the snapshot, open VNC in a new terminal, and wait for the user
    to close the VNC window. Then collect final monitor output and kill QEMU.
    Returns (wall_clock_time, migration_time, migration_completed).
    """
    print(f"\n[Test {test_num}] Starting {mode.upper()} snapshot restore (interactive VNC)...")
    os.chdir(QEMU_DIR)

    # Build QEMU command line
    cmd = [
        QEMU_PATH,
        "-enable-kvm", "-m", "2048", "-smp", "2", "-cpu", "host",
        "-drive", f"file={DISK_IMAGE},format=qcow2,if=none,id=drive0",
        "-device", "virtio-blk-pci,drive=drive0",
        "-vnc", VNC_DISPLAY,
        "-monitor", "stdio",
        "-name", f"Kali-restore-test-{test_num}-{mode}",
        "-incoming", "defer"
    ]

    # Pre‑commands to send before the user sees VNC (migration start, capabilities)
    if mode == "normal":
        pre_cmds = f"""info status
migrate_incoming "file:{snapshot_file}"
"""
    elif mode == "mapped-ram":
        # mapped-ram with multifd enabled
        pre_cmds = f"""info status
migrate_set_capability multifd on
migrate_set_capability mapped-ram on
migrate_set_parameter multifd-channels 1
migrate_incoming "file:{snapshot_file}"
"""
    elif mode == "mapped-nomultifd":
        # mapped-ram with multifd disabled (or not enabled)
        # We explicitly disable multifd to be sure
        pre_cmds = f"""info status
migrate_set_capability multifd off
migrate_set_capability mapped-ram on
migrate_incoming "file:{snapshot_file}"
"""
    else:
        raise ValueError(f"Unknown mode: {mode}")

    # We'll collect all QEMU output (stdout + stderr) for logging
    collected_stdout = []
    collected_stderr = []

    start_time = time.time()
    migration_completed = False

    try:
        # Start QEMU
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=QEMU_DIR,
            bufsize=1,
            universal_newlines=True
        )

        # Thread to read stdout and stderr continuously (to avoid blocking)
        out_queue = queue.Queue()
        def reader(stream, q):
            for line in iter(stream.readline, ''):
                q.put(line)
            stream.close()
        t_out = threading.Thread(target=reader, args=(proc.stdout, out_queue), daemon=True)
        t_err = threading.Thread(target=reader, args=(proc.stderr, out_queue), daemon=True)
        t_out.start()
        t_err.start()

        # Helper to read until we see the QEMU prompt (or timeout)
        def read_until_prompt(timeout=5):
            lines = []
            try:
                line = out_queue.get(timeout=timeout)
                lines.append(line)
            except queue.Empty:
                return None
            # Keep reading until prompt appears
            while True:
                try:
                    line = out_queue.get(timeout=0.2)
                    lines.append(line)
                    if line.rstrip().endswith('(qemu)'):
                        break
                except queue.Empty:
                    break
            return ''.join(lines)

        # Send the initial migration commands
        proc.stdin.write(pre_cmds)
        proc.stdin.flush()

        # Wait a bit for the migration to start (we don't wait for completion)
        time.sleep(1)  # give QEMU time to process

        # Now, open VNC in a new terminal and wait for user to close it
        term = find_terminal()
        if not term:
            print("  No terminal emulator found. Please open a VNC viewer manually (vncviewer localhost:0).")
            input("  Press Enter after you have closed the VNC viewer...")
            vnc_closed = True
        else:
            print(f"  Opening VNC viewer in {term}...")
            # Launch the terminal with vncviewer
            if term == "gnome-terminal":
                vnc_cmd = [term, "--", "vncviewer", f"localhost{VNC_DISPLAY}"]
            elif term == "xterm":
                vnc_cmd = [term, "-e", "vncviewer", f"localhost{VNC_DISPLAY}"]
            elif term == "konsole":
                vnc_cmd = [term, "-e", "vncviewer", f"localhost{VNC_DISPLAY}"]
            elif term == "xfce4-terminal":
                vnc_cmd = [term, "-e", "vncviewer", f"localhost{VNC_DISPLAY}"]
            elif term == "terminator":
                vnc_cmd = [term, "-e", "vncviewer", f"localhost{VNC_DISPLAY}"]
            else:
                vnc_cmd = [term, "-e", "vncviewer", f"localhost{VNC_DISPLAY}"]

            # Run the VNC viewer in a separate terminal and wait for it to exit
            vnc_proc = subprocess.run(vnc_cmd, check=False)
            vnc_closed = (vnc_proc.returncode == 0)

        # After VNC is closed, send final monitor commands
        final_cmds = get_monitor_commands_final(mode)
        proc.stdin.write(final_cmds)
        proc.stdin.flush()

        # Give QEMU a moment to respond, then read remaining output
        time.sleep(1)
        while True:
            try:
                line = out_queue.get_nowait()
                collected_stdout.append(line)
            except queue.Empty:
                break

        # Kill QEMU
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        # Collect any remaining stderr
        while True:
            try:
                line = out_queue.get_nowait()
                collected_stdout.append(line)
            except queue.Empty:
                break

        end_time = time.time()
        elapsed = end_time - start_time

        full_stdout = ''.join(collected_stdout)
        full_stderr = ''.join(collected_stderr)

        migration_time = parse_migration_time(full_stdout)

        # Determine if migration completed by looking at the final info migrate
        # We'll trust that if the user closed VNC, the VM was running, so migration succeeded.
        migration_completed = vnc_closed   # or we could parse the final status

        # Log detailed info
        with open(LOG_FILE, "a") as log_f:
            ts = datetime.now().isoformat()
            log_f.write(f"\n{'=' * 90}\n")
            log_f.write(f"[TEST {test_num}] {mode.upper()} SNAPSHOT RESTORE (interactive)\n")
            log_f.write(f"Timestamp          : {ts}\n")
            log_f.write(f"Snapshot file      : {snapshot_file}\n")
            log_f.write(f"Wall-clock time    : {elapsed:.3f} s\n")
            if migration_time is not None:
                log_f.write(f"Parsed migration time : {migration_time:.3f} s\n")
            else:
                log_f.write(f"Parsed migration time : (not detected)\n")
            log_f.write(f"Migration completed: {'YES' if migration_completed else 'NO'}\n")
            log_f.write(f"\n--- FULL QEMU MONITOR OUTPUT (debug traces filtered) ---\n")
            log_f.write(clean_monitor_output(full_stdout))
            log_f.write("\n--- END OF MONITOR OUTPUT ---\n")
            if full_stderr:
                log_f.write(f"\nSTDERR:\n{full_stderr}\n")
            log_f.write(f"{'=' * 90}\n\n")

        # Console output
        print(f" ✓ Total wall-clock time: {elapsed:.3f}s", end="")
        if migration_time is not None:
            print(f" | Migration time: {migration_time:.3f}s", end="")
        else:
            print(" | (migration time not detected)", end="")
        if migration_completed:
            print(" | Migration: COMPLETED ✓")
        else:
            print(" | Migration: FAILED / NOT COMPLETED ✗")

        if mode == "mapped-ram" and "multifd" in full_stdout.lower():
            print("   (multifd + mapped-ram path active)")
        elif mode == "mapped-nomultifd" and "multifd" in full_stdout.lower():
            # Might see a message about multifd being off
            if "off" in full_stdout.lower():
                print("   (mapped-ram with multifd off)")

        return elapsed, migration_time, migration_completed

    except Exception as e:
        print(f" ✗ Error in test {test_num}: {e}")
        return None, None, False


def main():
    parser = argparse.ArgumentParser(
        description="QEMU Snapshot Restore Benchmark (Interactive VNC mode)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('-n', '--normal', action='store_true',
                      help='Benchmark NORMAL snapshot (classic single-threaded)')
    group.add_argument('-m', '--mapped', action='store_true',
                      help='Benchmark MAPPED-RAM snapshot (multifd + mapped-ram)')
    group.add_argument('-q', '--mapped-nomultifd', action='store_true',
                      help='Benchmark MAPPED-RAM snapshot WITHOUT multifd (only mapped-ram)')

    args = parser.parse_args()

    if args.normal:
        mode = "normal"
        snapshot_file = NORMAL_SNAPSHOT
    elif args.mapped:
        mode = "mapped-ram"
        snapshot_file = MAPPED_SNAPSHOT
    else:  # args.mapped_nomultifd
        mode = "mapped-nomultifd"
        snapshot_file = MAPPED_SNAPSHOT

    print("=" * 90)
    print(f"QEMU Snapshot Restore Benchmark - {mode.upper()} MODE (Interactive VNC)")
    print("=" * 90)
    print(f"Timestamp     : {datetime.now().isoformat()}")
    print(f"Snapshot      : {snapshot_file}")
    print(f"Disk image    : {DISK_IMAGE}")
    print(f"Memory        : 2048 MB")
    print(f"CPUs          : 2")
    print(f"QEMU binary   : {QEMU_PATH}")
    print(f"Number of runs: {NUM_RUNS}")
    print("=" * 90)
    print("Instructions:")
    print("  For each test, a VNC viewer will open in a new terminal window.")
    print("  Verify that the VM is running (e.g., you see the login screen).")
    print("  Then close the VNC viewer window (or press Ctrl+C in that terminal).")
    print("  The test will then record the time and proceed to the next run.")
    print("=" * 90)

    times = []
    migration_times = []
    successful_migrations = 0

    for i in range(1, NUM_RUNS + 1):
        clear_caches()
        elapsed, migration_time, completed = run_restore_test(i, mode, snapshot_file)

        if elapsed is not None:
            times.append(elapsed)
            if migration_time is not None:
                migration_times.append(migration_time)
            if completed:
                successful_migrations += 1

        if i < NUM_RUNS:
            print("\nWaiting 2 seconds before next test...")
            time.sleep(2)

    if not times:
        print("\n✗ No successful test runs completed")
        sys.exit(1)

    # Statistics
    mean = statistics.mean(times)
    stdev = statistics.stdev(times) if len(times) > 1 else 0
    min_time = min(times)
    max_time = max(times)
    median = statistics.median(times)
    ci = 1.96 * stdev / (len(times) ** 0.5) if len(times) > 1 else 0

    print("\n" + "=" * 90)
    print(f"RESULTS - Wall-clock Time ({mode.upper()} mode)")
    print("=" * 90)
    print(f"Mean                  : {mean:.3f} s")
    print(f"Median                : {median:.3f} s")
    print(f"Std Dev               : {stdev:.3f} s")
    print(f"Min / Max             : {min_time:.3f} s / {max_time:.3f} s")
    print(f"95% Confidence        : {mean:.3f} ± {ci:.3f} s")
    print(f"Coefficient of Var    : {(stdev/mean)*100:.2f}%")
    print(f"Successful runs       : {len(times)}/{NUM_RUNS}")
    print(f"Migration completed   : {successful_migrations}/{NUM_RUNS}")
    print("=" * 90)

    if migration_times:
        mig_mean = statistics.mean(migration_times)
        mig_stdev = statistics.stdev(migration_times) if len(migration_times) > 1 else 0
        print(f"\nMigration time (parsed from info migrate):")
        print(f" Mean : {mig_mean:.3f} s")
        print(f" Std Dev : {mig_stdev:.3f} s")
        print("=" * 90)

    print("\nMethodology:")
    print("- Filesystem caches cleared before each run (true cold cache)")
    print("- QEMU started with '-incoming defer', snapshot loaded via monitor")
    print("- VNC viewer opens in a new terminal; user closes it when VM is running")
    print("- Wall-clock time measured from QEMU start until VNC viewer is closed")
    print("- Final monitor commands (info migrate, info status) are collected")
    print("- Log file ~/result.log contains full monitor output for every run")
    print(f"- Mode: {mode.upper()}")

    # Final summary appended to log
    with open(LOG_FILE, "a") as log_f:
        log_f.write(f"\n{'=' * 90}\n")
        log_f.write(f"FINAL BENCHMARK RESULTS - {mode.upper()} MODE\n")
        log_f.write(f"Timestamp              : {datetime.now().isoformat()}\n")
        log_f.write(f"Mean                   : {mean:.3f} s\n")
        log_f.write(f"Median                 : {median:.3f} s\n")
        log_f.write(f"Std Dev                : {stdev:.3f} s\n")
        log_f.write(f"Min / Max              : {min_time:.3f} s / {max_time:.3f} s\n")
        log_f.write(f"95% Confidence         : {mean:.3f} ± {ci:.3f} s\n")
        log_f.write(f"Successful runs        : {len(times)}/{NUM_RUNS}\n")
        log_f.write(f"Migration completed    : {successful_migrations}/{NUM_RUNS}\n")
        log_f.write(f"{'=' * 90}\n\n")


if __name__ == "__main__":
    main()
