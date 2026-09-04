#!/usr/bin/env python3
"""Count deflate blocks and gzip members in a .gz stream.

Parses the deflate bitstream itself, every Huffman symbol, because block
boundaries are not marked; a table-driven decoder keeps that tolerable in
pure Python. Multi-member streams, bgzip and MiGz output, are walked
member by member, and each member's ISIZE trailer is checked against the
bytes the symbols would have produced.

Usage:
    python3 scripts/deflate_blocks.py file.gz [...]

Prints members, blocks by type, and average block sizes per file.
bench.py imports scan_path() for its block-count pass. Needs only the
Python standard library.
"""
import sys

# Extra bits for length symbols 257..285 and distance symbols 0..29
LEN_EXTRA = [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2,
             3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0]
DIST_EXTRA = [0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6,
              7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13]
# Bases for the produced-size accounting the ISIZE check needs
LEN_BASE = [3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31,
            35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258]
CLC_ORDER = [16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15]


class BadStream(Exception):
    pass


def _table(lengths):
    """Full-size LSB-first decode table, entry = (codelen << 9) | symbol.
    Each code fills every table slot sharing its low bits, with a C-speed
    slice store, so lookup is one mask and one index."""
    maxbits = max(lengths)
    if maxbits == 0:
        raise BadStream("empty huffman table")
    size = 1 << maxbits
    table = [0] * size
    bl_count = [0] * (maxbits + 1)
    for ln in lengths:
        bl_count[ln] += 1
    bl_count[0] = 0
    next_code = [0] * (maxbits + 1)
    code = 0
    for bits in range(1, maxbits + 1):
        code = (code + bl_count[bits - 1]) << 1
        next_code[bits] = code
    for sym, ln in enumerate(lengths):
        if ln == 0:
            continue
        code = next_code[ln]
        next_code[ln] += 1
        rev = int(f"{code:0{ln}b}"[::-1], 2)
        table[rev::1 << ln] = [(ln << 9) | sym] * (size >> ln)
    return table, size - 1


FIXED_LIT = _table([8] * 144 + [9] * 112 + [7] * 24 + [8] * 8)
FIXED_DIST = _table([5] * 32)


def _dynamic_tables(data, p, bitbuf, bitcnt):
    n = len(data)
    while bitcnt < 14 and p < n:
        bitbuf |= data[p] << bitcnt
        p += 1
        bitcnt += 8
    hlit = (bitbuf & 31) + 257
    hdist = ((bitbuf >> 5) & 31) + 1
    hclen = ((bitbuf >> 10) & 15) + 4
    bitbuf >>= 14
    bitcnt -= 14
    cl_lens = [0] * 19
    for i in range(hclen):
        while bitcnt < 3 and p < n:
            bitbuf |= data[p] << bitcnt
            p += 1
            bitcnt += 8
        cl_lens[CLC_ORDER[i]] = bitbuf & 7
        bitbuf >>= 3
        bitcnt -= 3
    cl_table, cl_mask = _table(cl_lens)

    lens = []
    want = hlit + hdist
    while len(lens) < want:
        while bitcnt < 14 and p < n:
            bitbuf |= data[p] << bitcnt
            p += 1
            bitcnt += 8
        e = cl_table[bitbuf & cl_mask]
        ln = e >> 9
        if ln == 0:
            raise BadStream("bad code length code")
        bitbuf >>= ln
        bitcnt -= ln
        sym = e & 511
        if sym < 16:
            lens.append(sym)
        elif sym == 16:
            if not lens:
                raise BadStream("repeat with no previous length")
            rep = 3 + (bitbuf & 3)
            bitbuf >>= 2
            bitcnt -= 2
            lens.extend([lens[-1]] * rep)
        elif sym == 17:
            rep = 3 + (bitbuf & 7)
            bitbuf >>= 3
            bitcnt -= 3
            lens.extend([0] * rep)
        else:
            rep = 11 + (bitbuf & 127)
            bitbuf >>= 7
            bitcnt -= 7
            lens.extend([0] * rep)
    if len(lens) != want:
        raise BadStream("code length overrun")
    lit = _table(lens[:hlit])
    dist = _table(lens[hlit:]) if any(lens[hlit:]) else (None, 0)
    return lit, dist, p, bitbuf, bitcnt


def _scan_deflate(data, pos, counts):
    """Walk one deflate stream from byte pos, counting blocks into counts.
    Returns (end_pos, produced_bytes) with end_pos past the final block's
    byte-aligned padding."""
    n = len(data)
    p = pos
    bitbuf = 0
    bitcnt = 0
    produced = 0
    len_extra, len_base, dist_extra = LEN_EXTRA, LEN_BASE, DIST_EXTRA
    while True:
        while bitcnt < 3 and p < n:
            bitbuf |= data[p] << bitcnt
            p += 1
            bitcnt += 8
        if bitcnt < 3:
            raise BadStream("truncated block header")
        final = bitbuf & 1
        btype = (bitbuf >> 1) & 3
        bitbuf >>= 3
        bitcnt -= 3
        if btype == 0:
            counts["stored"] += 1
            drop = bitcnt & 7
            bitbuf >>= drop
            bitcnt -= drop
            head = p - (bitcnt >> 3)
            if head + 4 > n:
                raise BadStream("truncated stored block")
            length = data[head] | data[head + 1] << 8
            produced += length
            p = head + 4 + length
            if p > n:
                raise BadStream("stored block past end")
            bitbuf = 0
            bitcnt = 0
        elif btype in (1, 2):
            if btype == 1:
                counts["fixed"] += 1
                (lit_table, lit_mask), (dist_table, dist_mask) = FIXED_LIT, FIXED_DIST
            else:
                counts["dynamic"] += 1
                (lit_table, lit_mask), (dist_table, dist_mask), p, bitbuf, bitcnt = \
                    _dynamic_tables(data, p, bitbuf, bitcnt)
            while True:
                # One literal or match consumes at most 48 bits
                while bitcnt < 48 and p < n:
                    bitbuf |= data[p] << bitcnt
                    p += 1
                    bitcnt += 8
                e = lit_table[bitbuf & lit_mask]
                ln = e >> 9
                if ln == 0 or ln > bitcnt:
                    raise BadStream("bad literal/length code")
                bitbuf >>= ln
                bitcnt -= ln
                sym = e & 511
                if sym < 256:
                    produced += 1
                    continue
                if sym == 256:
                    break
                if sym > 285:
                    raise BadStream("bad length symbol")
                eb = len_extra[sym - 257]
                produced += len_base[sym - 257] + (bitbuf & ((1 << eb) - 1))
                bitbuf >>= eb
                bitcnt -= eb
                if dist_table is None:
                    raise BadStream("match with no distance table")
                d = dist_table[bitbuf & dist_mask]
                dl = d >> 9
                if dl == 0 or (d & 511) > 29:
                    raise BadStream("bad distance code")
                bitbuf >>= dl
                bitcnt -= dl
                deb = dist_extra[d & 511]
                bitbuf >>= deb
                bitcnt -= deb
        else:
            raise BadStream("reserved block type")
        if final:
            return p - (bitcnt >> 3), produced


def scan_bytes(data):
    """Counts for a gzip byte stream, every member walked and verified."""
    counts = {"members": 0, "stored": 0, "fixed": 0, "dynamic": 0,
              "produced_bytes": 0, "deflate_bytes": 0}
    n = len(data)
    pos = 0
    while pos < n:
        if pos + 10 > n or data[pos] != 0x1F or data[pos + 1] != 0x8B:
            raise BadStream(f"bad gzip magic at offset {pos}")
        if data[pos + 2] != 8:
            raise BadStream("unknown compression method")
        flg = data[pos + 3]
        p = pos + 10
        if flg & 4:  # FEXTRA
            xlen = data[p] | data[p + 1] << 8
            p += 2 + xlen
        if flg & 8:  # FNAME
            p = data.index(0, p) + 1
        if flg & 16:  # FCOMMENT
            p = data.index(0, p) + 1
        if flg & 2:  # FHCRC
            p += 2
        start = p
        end, produced = _scan_deflate(data, p, counts)
        if end + 8 > n:
            raise BadStream("truncated member trailer")
        isize = int.from_bytes(data[end + 4:end + 8], "little")
        if isize != produced % 2 ** 32:
            raise BadStream(f"ISIZE {isize} != produced {produced}")
        counts["members"] += 1
        counts["produced_bytes"] += produced
        counts["deflate_bytes"] += end - start
        pos = end + 8
    counts["blocks"] = counts["stored"] + counts["fixed"] + counts["dynamic"]
    return counts


def scan_path(path):
    with open(path, "rb") as f:
        return scan_bytes(f.read())


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__.strip())
    for path in sys.argv[1:]:
        c = scan_path(path)
        blocks = c["blocks"]
        print(f"{path}: {c['members']} members, {blocks} blocks "
              f"({c['stored']} stored, {c['fixed']} fixed, {c['dynamic']} dynamic), "
              f"avg {c['deflate_bytes'] / blocks:,.0f} B compressed, "
              f"{c['produced_bytes'] / blocks:,.0f} B input per block")


if __name__ == "__main__":
    main()
