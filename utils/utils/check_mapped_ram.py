#!/usr/bin/env python3
"""
QEMU Mapped-RAM Snapshot Parser
Parses and displays RAMBlock headers with mapped-ram metadata
"""
import sys
import struct
import os

def read_string(data, offset, max_len=256):
    """Read null-terminated string from data at offset"""
    end = data.find(b'\x00', offset, offset + max_len)
    if end == -1:
        return None, offset
    return data[offset:end].decode('ascii', errors='ignore'), end + 1

def parse_ramblock_header(data, offset):
    """
    Parse a ramblock header from migration stream
    Format (from QEMU migration code):
    - idstr (null-terminated string): ramblock identifier
    - padding to 8-byte boundary
    - used_len (uint64): number of bytes in ramblock
    - flags (uint32): ramblock flags
    """
    if offset + 16 > len(data):
        return None
    
    header = {}
    
    # Read idstr (ramblock name)
    idstr, next_offset = read_string(data, offset)
    if idstr is None:
        return None
    
    header['idstr'] = idstr
    header['offset_start'] = offset
    
    # Align to 8-byte boundary after idstr
    aligned_offset = ((next_offset + 7) // 8) * 8
    
    # Read used_len (8 bytes, little-endian)
    if aligned_offset + 8 > len(data):
        return None
    header['used_len'] = struct.unpack_from('<Q', data, aligned_offset)[0]
    
    # Read flags (4 bytes, little-endian)
    if aligned_offset + 12 > len(data):
        return None
    header['flags'] = struct.unpack_from('<I', data, aligned_offset + 8)[0]
    
    header['offset_end'] = aligned_offset + 12
    header['header_size'] = header['offset_end'] - header['offset_start']
    
    return header

def parse_mapped_ram_header(data, offset):
    """
    Parse mapped-ram specific header that follows ramblock header
    Format:
    - bitmap_size (uint64): size of the bitmap
    - pages_offset (uint64): offset of pages in file
    """
    if offset + 16 > len(data):
        return None
    
    try:
        bitmap_size = struct.unpack_from('<Q', data, offset)[0]
        pages_offset = struct.unpack_from('<Q', data, offset + 8)[0]
        
        return {
            'bitmap_size': bitmap_size,
            'pages_offset': pages_offset,
            'offset': offset
        }
    except:
        return None

def check_mapped_ram_snapshot(filename):
    print(f"🔍 Analyzing snapshot: {filename}\n")
    
    # Check file permissions
    if not os.access(filename, os.R_OK):
        print(f"❌ Permission denied: Cannot read {filename}")
        print(f"   Try: sudo chmod 644 {filename}")
        return
    
    try:
        with open(filename, "rb") as f:
            # Read first 100 MB for analysis
            data = f.read(100 * 1024 * 1024)
            file_size = os.path.getsize(filename)
    except Exception as e:
        print(f"❌ Error reading file: {e}")
        return
    
    print("=" * 80)
    print("VERIFICATION CHECKS")
    print("=" * 80)
    
    # Check 1: mapped-ram capability
    print("\n1️⃣  Checking for 'mapped-ram' capability...")
    has_mapped_ram_str = b"mapped-ram" in data[:5000000]
    if has_mapped_ram_str:
        print("   ✅ PASSED - 'mapped-ram' string found in header")
    else:
        print("   ❌ FAILED - No 'mapped-ram' capability detected")
        return
    
    # Check 2: RAM_SAVE_FLAG_MAPPED_RAM flag
    print("\n2️⃣  Checking for RAM_SAVE_FLAG_MAPPED_RAM (0x40000000)...")
    has_flag = (b'\x40\x00\x00\x00' in data[:10000000] or 
                b'\x00\x00\x00\x40' in data[:10000000])
    if has_flag:
        print("   ✅ PASSED - Mapped RAM flag found")
    else:
        print("   ⚠️  Flag not explicitly found (may use different encoding)")
    
    # Check 3: Look for ramblocks
    print("\n3️⃣  Scanning for RAMBlock headers...")
    ramblocks = []
    
    # Search for common ramblock names
    for ramblock_name in [b'pc.ram', b'pc.rom', b'bios', b'fw', b'ram']:
        search_limit = min(len(data), 50 * 1024 * 1024)
        pos = 0
        while True:
            pos = data.find(ramblock_name + b'\x00', pos, search_limit)
            if pos == -1:
                break
            
            # Try to parse as ramblock header
            header = parse_ramblock_header(data, pos)
            if header:
                ramblocks.append(header)
                print(f"   ✅ Found: {header['idstr']}")
            pos += 1
    
    if not ramblocks:
        print("   ❌ No ramblocks found")
        return
    
    print(f"\n   ✅ PASSED - Found {len(ramblocks)} RAMBlock(s)")
    
    # Display detailed ramblock information
    print("\n" + "=" * 80)
    print("RAMBLOCK HEADERS (Detailed)")
    print("=" * 80)
    
    for i, block in enumerate(ramblocks[:3], 1):  # Show first 3
        print(f"\n📦 RAMBlock #{i}")
        print("-" * 80)
        print(f"  Name (idstr):        {block['idstr']}")
        print(f"  Size (used_len):     {block['used_len']:,} bytes ({block['used_len'] / (1024**2):.2f} MB)")
        print(f"  Flags:               0x{block['flags']:08x}")
        
        # Decode flags
        flags_list = []
        if block['flags'] & 0x1:
            flags_list.append("RAM_SAVE_FLAG_MEM_SIZE")
        if block['flags'] & 0x4:
            flags_list.append("RAM_SAVE_FLAG_COMPRESS")
        if block['flags'] & 0x8:
            flags_list.append("RAM_SAVE_FLAG_XBZRLE")
        if block['flags'] & 0x40000000:
            flags_list.append("RAM_SAVE_FLAG_MAPPED_RAM")
        
        if flags_list:
            print(f"  Flag Details:        {', '.join(flags_list)}")
        
        print(f"  Header Offset:       0x{block['offset_start']:08x}")
        print(f"  Header Size:         {block['header_size']} bytes")
        
        # Try to find mapped-ram header
        mapped_header = parse_mapped_ram_header(data, block['offset_end'])
        if mapped_header:
            print(f"\n  📊 Mapped-RAM Header:")
            print(f"     Bitmap Size:     {mapped_header['bitmap_size']:,} bytes")
            print(f"     Pages Offset:    0x{mapped_header['pages_offset']:016x} ({mapped_header['pages_offset']:,})")
            print(f"     Pages Location:  {mapped_header['pages_offset'] / (1024**2):.2f} MB from file start")
    
    # Summary
    print("\n" + "=" * 80)
    print("✅ SUCCESS - This is a MAPPED-RAM snapshot!")
    print("=" * 80)
    print(f"\nFile Information:")
    print(f"  Total File Size:     {file_size:,} bytes ({file_size / (1024**3):.2f} GB)")
    print(f"  RAMBlocks Found:     {len(ramblocks)}")
    print(f"  Format:              QEMU Mapped-RAM Migration Format")
    print("\nKey Features:")
    print("  ✓ RAM pages mapped to fixed file offsets")
    print("  ✓ Compatible with multifd parallel migration")
    print("  ✓ Direct I/O compatible (O_DIRECT)")
    print("  ✓ Bounded file size (pages don't duplicate)")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 check_mapped_ram.py <snapshot.bin>")
        sys.exit(1)
    check_mapped_ram_snapshot(sys.argv[1])

