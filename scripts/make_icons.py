#!/usr/bin/env python3
"""Draws the ArtCraft Tracker mark and writes the app icons to icons/ (standard library only).

The mark is an open triangle, filled up to a level line: a gauge that fills as an app nears parity.
It is drawn for this project and is not an ArtCraft logo. Geometry on a 64-unit grid, the same as the
<symbol id="logo"> in index.html.

Run after changing the mark: python3 scripts/make_icons.py
"""
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "icons"

TRIANGLE = [(32.0, 7.0), (59.0, 55.0), (5.0, 55.0)]  # centre line of the outline
STROKE = 5.0
FILL = [(23.7, 33.0), (40.3, 33.0), (49.6, 49.5), (14.4, 49.5)]  # the "level"


def offset_triangle(tri, d):
    """Triangle offset by d (positive = outward). Offset lines stay parallel, so the result is the
    original scaled about the incentre by (r + d) / r."""
    (ax, ay), (bx, by), (cx, cy) = tri
    a = ((bx - cx) ** 2 + (by - cy) ** 2) ** .5
    b = ((cx - ax) ** 2 + (cy - ay) ** 2) ** .5
    c = ((ax - bx) ** 2 + (ay - by) ** 2) ** .5
    ix, iy = (a * ax + b * bx + c * cx) / (a + b + c), (a * ay + b * by + c * cy) / (a + b + c)
    area = abs((bx - ax) * (cy - ay) - (cx - ax) * (by - ay)) / 2
    r = area / ((a + b + c) / 2)
    k = (r + d) / r
    return [(ix + (x - ix) * k, iy + (y - iy) * k) for x, y in tri]


def inside(poly, x, y):
    hit = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


OUTER = offset_triangle(TRIANGLE, STROKE / 2)
INNER = offset_triangle(TRIANGLE, -STROKE / 2)


def mark(x, y):
    return (inside(OUTER, x, y) and not inside(INNER, x, y)) or inside(FILL, x, y)


def render(size, scale, radius=0.0, ss=4):
    """RGBA rows: the white mark on black, the 64-unit grid spanning `scale` of the canvas.
    radius > 0 rounds the corners of the black tile (transparent outside)."""
    unit = size * scale / 64
    off = (size - 64 * unit) / 2
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            cover = tile = 0
            for sy in range(ss):
                for sx in range(ss):
                    x, y = px + (sx + .5) / ss, py + (sy + .5) / ss
                    in_tile = True
                    if radius:
                        cx = min(max(x, radius), size - radius)
                        cy = min(max(y, radius), size - radius)
                        in_tile = (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2
                    if in_tile:
                        tile += 1
                        if mark((x - off) / unit, (y - off) / unit + 1.5):  # +1.5: optical centring
                            cover += 1
            v = round(255 * cover / tile) if tile else 0
            row += bytes((v, v, v, round(255 * tile / (ss * ss))))
        rows.append(bytes(row))
    return rows


def write_png(path, rows):
    size = len(rows)
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    path.write_bytes(png)


def svg(bg_radius, scale):
    off = 32 * (1 - scale)
    pts = lambda poly: " ".join(f"{x:.2f},{y:.2f}" for x, y in poly)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            f'<rect width="64" height="64" rx="{bg_radius}" fill="#000"/>'
            f'<g transform="translate({off:.2f} {off - 1.5 * scale:.2f}) scale({scale:.4f})" fill="#fff">'
            f'<path fill-rule="evenodd" d="M{pts(OUTER)}Z M{pts(INNER)}Z"/>'
            f'<polygon points="{pts(FILL)}"/></g></svg>\n')


def main():
    OUT.mkdir(exist_ok=True)
    write_png(OUT / "icon-512.png", render(512, .70))
    write_png(OUT / "icon-192.png", render(192, .70))
    write_png(OUT / "icon-maskable-512.png", render(512, .52))  # stays inside the 80 % safe zone
    write_png(OUT / "apple-touch-icon.png", render(180, .64))
    write_png(OUT / "favicon-32.png", render(32, .86, radius=5))
    (OUT / "favicon.svg").write_text(svg(12, .86), encoding="utf-8")
    (OUT / "icon.svg").write_text(svg(0, .70), encoding="utf-8")
    print("icons written to", OUT)


if __name__ == "__main__":
    main()
