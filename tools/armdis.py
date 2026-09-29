#!/usr/bin/env python3
"""Disassemble a RISC OS module image around a "*Where" offset.

usage: armdis.py MODULE_FILE OFFSET [WORDS_BEFORE [WORDS_AFTER]]

OFFSET is the module-relative offset that *Where prints, in hex ("2B28",
"&2B28" and "0x2B28" all work). MODULE_FILE is the built module image
(e.g. rm.USBDriver) -- it must be the exact build that crashed.

Needs capstone:  pip install capstone

Function names come from the APCS name markers (the name string, then a
word 0xFFxxxxxx holding its padded length, immediately before a function's
entry point), when the compiler emitted them.
"""
import re
import struct
import sys

from capstone import CS_ARCH_ARM, CS_MODE_ARM, Cs


def parse_offset(text):
    text = text.strip().lstrip("&")
    if text.lower().startswith("0x"):
        text = text[2:]
    return int(text, 16)


def word(data, off):
    return struct.unpack_from("<I", data, off)[0]


def function_at(data, off, limit=0x10000):
    """Nearest APCS name marker at or before off -> (name, entry offset)."""
    pos = off & ~3
    stop = max(0, pos - limit)
    while pos >= stop:
        w = word(data, pos)
        if (w & 0xFF000000) == 0xFF000000:
            length = w & 0x00FFFFFF
            if 0 < length <= 256 and pos - length >= 0:
                name = data[pos - length:pos].split(b"\0")[0]
                if name and all(32 <= c < 127 for c in name):
                    return name.decode("ascii"), pos + 4
        pos -= 4
    return None


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    with open(argv[1], "rb") as f:
        data = f.read()
    target = parse_offset(argv[2]) & ~3
    before = int(argv[3]) if len(argv) > 3 else 16
    after = int(argv[4]) if len(argv) > 4 else 8
    if target >= len(data):
        print("offset %X is beyond the end of the file (%X bytes)" % (target, len(data)))
        return 1

    fn = function_at(data, target)
    if fn:
        print("in %s (entry +%X, fault is +%X into it)" % (fn[0], fn[1], target - fn[1]))
    else:
        print("no APCS function-name marker found before +%X" % target)
    print()

    md = Cs(CS_ARCH_ARM, CS_MODE_ARM)
    start = max(0, target - 4 * before)
    end = min(len(data) - 3, target + 4 * (after + 1))
    for addr in range(start & ~3, end, 4):
        w = word(data, addr)
        text = ".word 0x%08X" % w
        for insn in md.disasm(struct.pack("<I", w), addr):
            text = ("%s %s" % (insn.mnemonic, insn.op_str)).strip()
        note = ""
        m = re.match(r"ldr\w* \w+, \[pc, #(-?)(0x[0-9a-f]+|\d+)\]$", text)
        if m:
            disp = int(m.group(2), 0) * (-1 if m.group(1) else 1)
            lit = addr + 8 + disp
            if 0 <= lit <= len(data) - 4:
                note = "   ; literal +%X = 0x%08X" % (lit, word(data, lit))
        mark = "  <== fault" if addr == target else ""
        print("%06X: %08X  %s%s%s" % (addr, w, text, note, mark))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
