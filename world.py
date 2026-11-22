"""Static map model: building raster (2 m), terrain, line of sight, nav grid (4 m) and exposure.

Baked offline into nav.bin by tools/bake.py; rebuilt at runtime (without exposure) if the bake is
missing or does not match the map. Standard library only.
"""
from __future__ import annotations

import array
import json
import math
import os
import struct
import sys

C2 = 2.0          # raster cell (m)
C4 = 4.0          # nav cell (m)
MAGIC = b"MRNAV03\0"
NONE_H = -32768   # no solid in this 2 m cell


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class World:
    def __init__(self):
        self.minx = -513.0
        self.miny = -334.0
        self.tnx = 0
        self.tny = 0
        self.terr = None          # array('h') cm, terrain vertex grid at 1 m
        self.nx2 = self.ny2 = 0
        self.top = None           # array('h') dm, top of tallest solid in 2 m cell (NONE_H = none)
        self.base = None          # array('h') dm, base of that solid
        self.nx4 = self.ny4 = 0
        self.walk = None          # bytearray 1 = walkable nav cell
        self.expo = None          # bytearray mean free sight distance from eye (m, capped 120)
        self.gnd4 = None          # array('h') dm ground at nav cell centre
        self.bounds = (-512.0, 555.0, -333.0, 388.0)

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, map_dir: str, baked: str) -> "World":
        w = cls()
        wj = None
        sig = 0
        try:
            p = os.path.join(map_dir, "v2", "world.json")
            sig = os.path.getsize(p)
            with open(p) as f:
                wj = json.load(f)
        except Exception as e:  # map missing (should not happen in a match)
            log("world.json not readable:", e)
        try:
            w._load_terrain(map_dir, wj)
        except Exception as e:
            log("terrain not readable:", e)
        ok = False
        try:
            ok = w._load_baked(baked, sig if wj is not None else None)
        except Exception as e:
            log("bake not loaded:", e)
        if not ok and wj is not None:
            log("building raster at runtime")
            w.build(wj, exposure=False)
        if w.terr is None and w.gnd4 is not None:
            pass
        return w

    def _load_terrain(self, map_dir, wj):
        t = (wj or {}).get("terrain") or {"minX": -513.0, "minY": -334.0, "nx": 1070, "ny": 724}
        self.minx, self.miny = float(t["minX"]), float(t["minY"])
        self.tnx, self.tny = int(t["nx"]), int(t["ny"])
        if wj is not None:
            b = wj["bounds"]
            self.bounds = (b["minX"], b["maxX"], b["minY"], b["maxY"])
        a = array.array("h")
        with open(os.path.join(map_dir, "v2", "terrain.i16"), "rb") as f:
            a.frombytes(f.read())
        if sys.byteorder != "little":
            a.byteswap()
        if len(a) == self.tnx * self.tny:
            self.terr = a

    def _load_baked(self, path, sig):
        with open(path, "rb") as f:
            data = f.read()
        if data[:8] != MAGIC:
            return False
        bsig, nx2, ny2, nx4, ny4 = struct.unpack_from("<qiiii", data, 8)
        if sig is not None and bsig != sig:
            log("bake signature mismatch", bsig, sig)
            return False
        o = 8 + 24
        n2, n4 = nx2 * ny2, nx4 * ny4
        self.nx2, self.ny2, self.nx4, self.ny4 = nx2, ny2, nx4, ny4
        self.top = array.array("h"); self.top.frombytes(data[o:o + 2 * n2]); o += 2 * n2
        self.base = array.array("h"); self.base.frombytes(data[o:o + 2 * n2]); o += 2 * n2
        self.gnd4 = array.array("h"); self.gnd4.frombytes(data[o:o + 2 * n4]); o += 2 * n4
        self.walk = bytearray(data[o:o + n4]); o += n4
        self.expo = bytearray(data[o:o + n4]); o += n4
        return True

    def save_baked(self, path, sig):
        with open(path, "wb") as f:
            f.write(MAGIC)
            f.write(struct.pack("<qiiii", sig, self.nx2, self.ny2, self.nx4, self.ny4))
            f.write(self.top.tobytes()); f.write(self.base.tobytes()); f.write(self.gnd4.tobytes())
            f.write(bytes(self.walk)); f.write(bytes(self.expo))

    # ------------------------------------------------------------------ terrain
    def ground(self, x, y):
        t = self.terr
        if t is None:
            if self.gnd4 is not None:
                return self.gnd4[self.c4(x, y)] * 0.1
            return 0.0
        fx = x - self.minx
        fy = y - self.miny
        nx = self.tnx
        if fx < 0: fx = 0.0
        if fy < 0: fy = 0.0
        if fx > nx - 1.001: fx = nx - 1.001
        if fy > self.tny - 1.001: fy = self.tny - 1.001
        i = int(fx); j = int(fy)
        ax = fx - i; ay = fy - j
        k = j * nx + i
        h00 = t[k]; h10 = t[k + 1]; h01 = t[k + nx]; h11 = t[k + nx + 1]
        return 0.01 * ((h00 * (1 - ax) + h10 * ax) * (1 - ay) + (h01 * (1 - ax) + h11 * ax) * ay)

    # ------------------------------------------------------------------ indexing
    def c4(self, x, y):
        i = int((x - self.minx) / C4); j = int((y - self.miny) / C4)
        if i < 0: i = 0
        elif i >= self.nx4: i = self.nx4 - 1
        if j < 0: j = 0
        elif j >= self.ny4: j = self.ny4 - 1
        return j * self.nx4 + i

    def c4xy(self, k):
        j, i = divmod(k, self.nx4)
        return (self.minx + (i + 0.5) * C4, self.miny + (j + 0.5) * C4)

    def walkable_at(self, x, y):
        return self.walk[self.c4(x, y)] == 1

    def inside_bounds(self, x, y, m=6.0):
        b = self.bounds
        return b[0] + m < x < b[1] - m and b[2] + m < y < b[3] - m

    # ------------------------------------------------------------------ sight / shots
    def solid(self, x, y, z):
        """True if (x,y,z) is inside a building/terrain (coarse 2 m raster)."""
        i = int((x - self.minx) / C2); j = int((y - self.miny) / C2)
        if 0 <= i < self.nx2 and 0 <= j < self.ny2:
            k = j * self.nx2 + i
            tp = self.top[k]
            if tp != NONE_H and self.base[k] * 0.1 <= z <= tp * 0.1:
                return True
        return z < self.ground(x, y) - 0.3

    def los(self, a, b, step=2.0, skip=3.0):
        """Segment a->b (3D) clear of buildings and terrain. Skips `skip` m at both ends."""
        dx = b[0] - a[0]; dy = b[1] - a[1]; dz = b[2] - a[2]
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        if d < 2 * skip + 0.1:
            return True
        n = int((d - 2 * skip) / step) + 1
        inv = 1.0 / d
        ux, uy, uz = dx * inv, dy * inv, dz * inv
        top = self.top; base = self.base; nx2 = self.nx2; ny2 = self.ny2
        mx = self.minx; my = self.miny
        s = skip
        for _ in range(n + 1):
            if s > d - skip:
                s = d - skip
            x = a[0] + ux * s; y = a[1] + uy * s; z = a[2] + uz * s
            i = int((x - mx) * 0.5); j = int((y - my) * 0.5)
            if 0 <= i < nx2 and 0 <= j < ny2:
                k = j * nx2 + i
                tp = top[k]
                if tp != NONE_H and base[k] * 0.1 <= z <= tp * 0.1:
                    return False
            if z < self.ground(x, y) - 0.3:
                return False
            if s >= d - skip:
                break
            s += step
        return True

    def wall_ahead(self, pos, ux, uy, dist=6.0):
        """Something taller than a step within `dist` m along (ux,uy) at knee height."""
        z = pos[2] + 1.6
        for s in (3.0, dist * 0.5 + 1.5, dist):
            x = pos[0] + ux * s; y = pos[1] + uy * s
            if self.solid(x, y, z) or self.ground(x, y) > z:
                return True
        return False

    def line_walkable(self, a, b):
        """Straight horizontal walk a->b stays on walkable nav cells."""
        dx = b[0] - a[0]; dy = b[1] - a[1]
        d = math.hypot(dx, dy)
        n = int(d / 2.0) + 1
        walk = self.walk
        for s in range(1, n + 1):
            f = s / n
            if walk[self.c4(a[0] + dx * f, a[1] + dy * f)] != 1:
                return False
        return True

    # ------------------------------------------------------------------ building
    def build(self, wj, exposure=True):
        mx, my = self.minx, self.miny
        b = wj["bounds"]
        self.bounds = (b["minX"], b["maxX"], b["minY"], b["maxY"])
        nx2 = int(math.ceil(self.tnx / C2)); ny2 = int(math.ceil(self.tny / C2))
        self.nx2, self.ny2 = nx2, ny2
        top = array.array("h", [NONE_H]) * (nx2 * ny2)
        base = array.array("h", [0]) * (nx2 * ny2)
        for p in wj["prisms"]:
            pt = int(round(p["top"] * 10)); pb = int(round(p["base"] * 10))
            rings = [p["ring"]] + list(p.get("holes") or [])
            edges = []
            ys = p["ring"][1::2]
            for r in rings:
                m = len(r) // 2
                for e in range(m):
                    x0, y0 = r[2 * e], r[2 * e + 1]
                    x1, y1 = r[2 * ((e + 1) % m)], r[2 * ((e + 1) % m) + 1]
                    if y0 != y1:
                        edges.append((x0, y0, x1, y1))
            j0 = max(0, int((min(ys) - my) / C2)); j1 = min(ny2 - 1, int((max(ys) - my) / C2))
            for j in range(j0, j1 + 1):
                yc = my + (j + 0.5) * C2
                xs = []
                for (x0, y0, x1, y1) in edges:
                    if (y0 <= yc < y1) or (y1 <= yc < y0):
                        xs.append(x0 + (yc - y0) * (x1 - x0) / (y1 - y0))
                xs.sort()
                for q in range(0, len(xs) - 1, 2):
                    i0 = int(math.ceil((xs[q] - mx) / C2 - 0.5)); i1 = int(math.floor((xs[q + 1] - mx) / C2 - 0.5))
                    if i1 < i0:  # thin wall: mark the cell it sits in
                        i0 = i1 = int(((xs[q] + xs[q + 1]) * 0.5 - mx) / C2)
                    for i in range(max(0, i0), min(nx2 - 1, i1) + 1):
                        k = j * nx2 + i
                        if top[k] == NONE_H or pt > top[k]:
                            top[k] = pt; base[k] = pb
        self.top, self.base = top, base
        # ground at 2 m cells and obstacle mask
        g2 = [0.0] * (nx2 * ny2)
        for j in range(ny2):
            for i in range(nx2):
                g2[j * nx2 + i] = self.ground(mx + (i + 0.5) * C2, my + (j + 0.5) * C2)
        obst = bytearray(nx2 * ny2)
        for j in range(ny2):
            for i in range(nx2):
                k = j * nx2 + i
                g = g2[k]
                tp = top[k]
                if tp != NONE_H:
                    t = tp * 0.1; bb = base[k] * 0.1
                    if t > g + 1.2 and bb < g + 9.5:
                        obst[k] = 1
                        continue
                # steep terrain
                if i + 1 < nx2 and abs(g2[k + 1] - g) > 2.0 * 1.05:
                    obst[k] = 1
                elif j + 1 < ny2 and abs(g2[k + nx2] - g) > 2.0 * 1.05:
                    obst[k] = 1
        # nav grid: centre clear of obstacle cell centres by >= 3.6 m
        nx4 = int(math.ceil(nx2 / 2)); ny4 = int(math.ceil(ny2 / 2))
        self.nx4, self.ny4 = nx4, ny4
        walk = bytearray(nx4 * ny4)
        gnd4 = array.array("h", [0]) * (nx4 * ny4)
        bx = self.bounds
        offs = []
        for dj in range(-3, 4):
            for di in range(-3, 4):
                offs.append((di, dj))
        for j in range(ny4):
            for i in range(nx4):
                cx = mx + (i + 0.5) * C4; cy = my + (j + 0.5) * C4
                gnd4[j * nx4 + i] = int(round(self.ground(cx, cy) * 10))
                if not (bx[0] + 8 < cx < bx[1] - 8 and bx[2] + 8 < cy < bx[3] - 8):
                    continue
                ok = True
                for di, dj in offs:
                    ii = 2 * i + di; jj = 2 * j + dj
                    if 0 <= ii < nx2 and 0 <= jj < ny2 and obst[jj * nx2 + ii]:
                        ox = mx + (ii + 0.5) * C2; oy = my + (jj + 0.5) * C2
                        if (ox - cx) ** 2 + (oy - cy) ** 2 < 3.6 ** 2:
                            ok = False
                            break
                if ok:
                    walk[j * nx4 + i] = 1
        # keep only the largest connected walkable region (drop courtyards you can't walk into)
        self.walk = self._largest_component(walk, nx4, ny4)
        self.gnd4 = gnd4
        self.expo = bytearray([60]) * (nx4 * ny4)
        if exposure:
            self.compute_exposure()

    @staticmethod
    def _largest_component(walk, nx, ny):
        comp = [0] * (nx * ny)
        best, best_id, cid = 0, 0, 0
        for s in range(nx * ny):
            if walk[s] != 1 or comp[s]:
                continue
            cid += 1
            comp[s] = cid
            st = [s]; size = 0
            while st:
                k = st.pop(); size += 1
                j, i = divmod(k, nx)
                for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ii, jj = i + di, j + dj
                    if 0 <= ii < nx and 0 <= jj < ny:
                        q = jj * nx + ii
                        if walk[q] == 1 and not comp[q]:
                            comp[q] = cid; st.append(q)
            if size > best:
                best, best_id = size, cid
        out = bytearray(nx * ny)
        for k in range(nx * ny):
            if comp[k] == best_id:
                out[k] = 1
        return out

    def compute_exposure(self, dirs=16, maxd=120.0, step=3.0):
        """Mean free sight distance from eye height over `dirs` directions (lower = better cover)."""
        nx4 = self.nx4
        exp = bytearray(nx4 * self.ny4)
        vec = [(math.cos(2 * math.pi * q / dirs), math.sin(2 * math.pi * q / dirs)) for q in range(dirs)]
        top = self.top; base = self.base; nx2 = self.nx2; ny2 = self.ny2; mx = self.minx; my = self.miny
        for k in range(nx4 * self.ny4):
            if self.walk[k] != 1:
                continue
            cx, cy = self.c4xy(k)
            z = self.gnd4[k] * 0.1 + 8.0
            tot = 0.0
            for (ux, uy) in vec:
                s = step
                while s < maxd:
                    x = cx + ux * s; y = cy + uy * s
                    i = int((x - mx) * 0.5); j = int((y - my) * 0.5)
                    if not (0 <= i < nx2 and 0 <= j < ny2):
                        break
                    q = j * nx2 + i
                    tp = top[q]
                    if tp != NONE_H and base[q] * 0.1 <= z <= tp * 0.1:
                        break
                    if self.ground(x, y) > z:
                        break
                    s += step
                tot += min(s, maxd)
            exp[k] = int(min(255, tot / dirs))
        self.expo = exp
