import sys

lines = sys.stdin.readlines()
result = []
i = 0

while i < len(lines):
    match_len = 0
    for size in range(100, 0, -1):
        if i + 2 * size <= len(lines):
            if lines[i : i + size] == lines[i + size : i + 2 * size]:
                match_len = size
                break

    if match_len > 0:
        block = lines[i: i+match_len]
        result.extend(block)
        i += match_len
        while i + match_len <= len(lines) and lines[i : i + match_len] == block:
            i += match_len
    else:
        result.append(lines[i])
        i += 1

sys.stdout.writelines(result)
