"""Survival-first mechfight bot: zone discipline, cover, careful loot, fight from cover, retreat to regen."""
from __future__ import annotations

import math
import os
import random
import time
from collections import deque

import mechfight as mf
from world import World, log

HERE = os.path.dirname(os.path.abspath(__file__))
TAU = 2 * math.pi
SLICE = 0.010          # total decision budget before path search stops (s)
PREF = {"lightning": 3.0, "laser": 2.5, "mortar": 1.0, "gun": 0.0}
UPGRADE_ORDER = ["plating", "armor", "nanites", "gun_damage", "gun_rate", "lightning_efficiency",
                 "laser_damage", "laser_charge", "lightning_range", "servos", "gyros", "gun_mag",
                 "mortar_reload", "sensors", "gun_velocity", "capacitor", "reactor", "jets",
                 "lightning_chain", "laser_pierce", "mortar_splash", "mortar_velocity"]


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def d2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Field:
    """Breadth-first distance field to a goal cell over walkable nav cells, built in time slices."""

    def __init__(self, w: World, goal_xy):
        self.w = w
        self.goal = goal_xy
        n = w.nx4 * w.ny4
        self.dist = [-1] * n
        g = self._nearest_walkable(w.c4(*goal_xy))
        self.gk = g
        self.dist[g] = 0
        self.q = deque([g])
        self.done = False

    def _nearest_walkable(self, k):
        w = self.w
        if w.walk[k]:
            return k
        j0, i0 = divmod(k, w.nx4)
        for r in range(1, 40):
            best = None
            for dj in range(-r, r + 1):
                for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                    i, j = i0 + di, j0 + dj
                    if 0 <= i < w.nx4 and 0 <= j < w.ny4 and w.walk[j * w.nx4 + i]:
                        dd = di * di + dj * dj
                        if best is None or dd < best[0]:
                            best = (dd, j * w.nx4 + i)
            if best:
                return best[1]
        return k

    def step(self, deadline):
        if self.done:
            return True
        w = self.w; nx = w.nx4; ny = w.ny4; walk = w.walk; dist = self.dist; q = self.q
        pc = time.perf_counter
        cnt = 0
        while q:
            k = q.popleft()
            dk = dist[k] + 1
            j, i = divmod(k, nx)
            e = i + 1 < nx; wv = i > 0; nn = j + 1 < ny; s = j > 0
            if e and walk[k + 1] and dist[k + 1] < 0:
                dist[k + 1] = dk; q.append(k + 1)
            if wv and walk[k - 1] and dist[k - 1] < 0:
                dist[k - 1] = dk; q.append(k - 1)
            if nn and walk[k + nx] and dist[k + nx] < 0:
                dist[k + nx] = dk; q.append(k + nx)
            if s and walk[k - nx] and dist[k - nx] < 0:
                dist[k - nx] = dk; q.append(k - nx)
            if e and nn and walk[k + nx + 1] and walk[k + 1] and walk[k + nx] and dist[k + nx + 1] < 0:
                dist[k + nx + 1] = dk; q.append(k + nx + 1)
            if wv and nn and walk[k + nx - 1] and walk[k - 1] and walk[k + nx] and dist[k + nx - 1] < 0:
                dist[k + nx - 1] = dk; q.append(k + nx - 1)
            if e and s and walk[k - nx + 1] and walk[k + 1] and walk[k - nx] and dist[k - nx + 1] < 0:
                dist[k - nx + 1] = dk; q.append(k - nx + 1)
            if wv and s and walk[k - nx - 1] and walk[k - 1] and walk[k - nx] and dist[k - nx - 1] < 0:
                dist[k - nx - 1] = dk; q.append(k - nx - 1)
            cnt += 1
            if cnt & 255 == 0 and pc() > deadline:
                return False
        self.done = True
        return True

    def waypoint(self, pos):
        """Point to walk toward from pos, or None if pos is not connected yet."""
        w = self.w; nx = w.nx4; dist = self.dist
        k = w.c4(pos[0], pos[1])
        if dist[k] < 0:
            j0, i0 = divmod(k, nx)
            best = None
            for dj in range(-3, 4):
                for di in range(-3, 4):
                    i, j = i0 + di, j0 + dj
                    if 0 <= i < nx and 0 <= j < w.ny4:
                        q = j * nx + i
                        if dist[q] >= 0:
                            sc = dist[q] + 1.5 * math.hypot(di, dj)
                            if best is None or sc < best[0]:
                                best = (sc, q)
            if best is None:
                return None
            return w.c4xy(best[1])
        if dist[k] == 0:
            return self.goal
        cur = k
        chain = []
        for _ in range(8):
            j, i = divmod(cur, nx)
            bd, bq = dist[cur], None
            for dj in (-1, 0, 1):
                for di in (-1, 0, 1):
                    if di == 0 and dj == 0:
                        continue
                    ii, jj = i + di, j + dj
                    if 0 <= ii < nx and 0 <= jj < w.ny4:
                        q = jj * nx + ii
                        if 0 <= dist[q] < bd:
                            bd, bq = dist[q], q
            if bq is None:
                break
            cur = bq
            chain.append(cur)
            if dist[cur] == 0:
                break
        if not chain:
            return self.goal
        for q in reversed(chain):
            c = w.c4xy(q)
            if w.line_walkable(pos, c):
                if dist[q] == 0:
                    return self.goal if w.line_walkable(pos, self.goal) else c
                return c
        return w.c4xy(chain[0])


class Bot:
    name = "survivor-py"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.me_id = hello["slot"]
        self.rng = random.Random(hello.get("seed", 1))
        md = hello.get("map", {}).get("dir", "/arena")
        t0 = time.perf_counter()
        self.w = World.load(md, os.path.join(HERE, "nav.bin"))
        log("world loaded in %.2fs" % (time.perf_counter() - t0))
        self.field = None
        self.pending = None          # Field being built
        self.home = None
        self.home_zone = None
        self.home_t = -99.0
        self.enemies = {}            # id -> dict
        self.npcs = {}
        self.last_dmg_t = -99.0
        self.last_dmg_by = {}        # id -> t
        self.last_dmg_bearing = None
        self.last_hit_t = -99.0
        self.sounds = []             # (t, bearing, kind, dist)
        self.retreat = None
        self.retreat_t = -99.0
        self.hide_pt = None
        self.hide_t = -99.0
        self.recover_until = -99.0
        self.strafe_dir = 1
        self.strafe_until = 0.0
        self.jink_boost = False
        self.hist = deque(maxlen=16)
        self.stuck_until = -1.0
        self.stuck_turn = 0.0
        self.scan_yaw = 0.0
        self.mode = "start"
        self.loot_target = None
        self.loot_until = 0.0
        self.bad_loot = {}
        self.pending_take = None
        self.known_pickups = {}      # id -> pickup dict (weapons never despawn)
        self.arm_goal = None
        self.climb = False
        self.t = 0.0

    # ------------------------------------------------------------------ helpers
    def zone_now(self, z, t):
        if t <= z["shrink_start"]:
            return z["center"], z["radius"]
        if t >= z["shrink_end"]:
            return z["next_center"], z["next_radius"]
        f = (t - z["shrink_start"]) / max(1e-3, z["shrink_end"] - z["shrink_start"])
        c = [z["center"][0] + (z["next_center"][0] - z["center"][0]) * f,
             z["center"][1] + (z["next_center"][1] - z["center"][1]) * f]
        return c, z["radius"] + (z["next_radius"] - z["radius"]) * f

    def safe_radius(self, z):
        nr = z["next_radius"]
        return max(0.0, nr - max(6.0, min(60.0, nr * 0.3)))

    def in_safe(self, p, z, slack=0.0):
        return d2(p, z["next_center"]) <= self.safe_radius(z) + slack

    def set_goal(self, xy, deadline):
        if self.pending is not None and d2(self.pending.goal, xy) < 6:
            return
        if self.field is not None and d2(self.field.goal, xy) < 6:
            self.pending = None
            return
        self.pending = Field(self.w, xy)
        self.pending.step(deadline)

    def advance_field(self, deadline):
        if self.pending is not None:
            if self.pending.step(deadline):
                self.field, self.pending = self.pending, None

    def nav_dir(self, me, goal):
        """Unit move direction toward goal using the field if it matches, else straight."""
        pos = me["pos"]
        wp = None
        f = self.field
        if f is not None and d2(f.goal, goal) < 8:
            wp = f.waypoint(pos)
            if wp is None and f.done:
                self.climb = True          # we are somewhere the walk grid can't reach (roof, bowl)
            elif f.done and not self.w.walkable_at(goal[0], goal[1]) and d2(pos, self.w.c4xy(f.gk)) < 12:
                self.climb = True          # goal itself is off the walk grid: go straight, jet over walls
                wp = None
        if wp is None:
            wp = goal
        dx, dy = wp[0] - pos[0], wp[1] - pos[1]
        n = math.hypot(dx, dy)
        if n < 1e-6:
            return (0.0, 0.0)
        return (dx / n, dy / n)

    def straight_dir(self, pos, goal):
        dx, dy = goal[0] - pos[0], goal[1] - pos[1]
        n = math.hypot(dx, dy)
        return (0.0, 0.0) if n < 1e-6 else (dx / n, dy / n)

    def eye(self, m, scale=1.0):
        return (m["pos"][0], m["pos"][1], m["pos"][2] + 8.0 * scale)

    def center(self, m):
        s = 1 + 0.05 * (m.get("cubes") or 0)
        return (m["pos"][0], m["pos"][1], m["pos"][2] + 4.5 * s)

    # ------------------------------------------------------------------ home selection
    def choose_home(self, me, z, t):
        w = self.w
        nc = z["next_center"]
        sr = self.safe_radius(z)
        pos = me["pos"]
        if sr < 4:
            return (nc[0], nc[1])
        r = sr
        stride = max(1, int(math.ceil(2 * r / 4.0 / 40)))
        i0 = max(0, int((nc[0] - r - w.minx) / 4)); i1 = min(w.nx4 - 1, int((nc[0] + r - w.minx) / 4))
        j0 = max(0, int((nc[1] - r - w.miny) / 4)); j1 = min(w.ny4 - 1, int((nc[1] + r - w.miny) / 4))
        threats = [e["pos"] for e in self.enemies.values() if t - e["t"] < 20]
        best = None
        r2 = r * r
        for j in range(j0, j1 + 1, stride):
            for i in range(i0, i1 + 1, stride):
                k = j * w.nx4 + i
                if not w.walk[k]:
                    continue
                x = w.minx + (i + 0.5) * 4; y = w.miny + (j + 0.5) * 4
                dc2 = (x - nc[0]) ** 2 + (y - nc[1]) ** 2
                if dc2 > r2:
                    continue
                dm = math.hypot(x - pos[0], y - pos[1])
                sc = w.expo[k] + 0.10 * dm + 0.04 * math.sqrt(dc2)
                for tp in threats:
                    dd = math.hypot(x - tp[0], y - tp[1])
                    if dd < 150:
                        sc += (150 - dd) * 0.4
                if best is None or sc < best[0]:
                    best = (sc, x, y)
        if best is None:
            return (nc[0], nc[1])
        return (best[1], best[2])

    # ------------------------------------------------------------------ main
    def act(self, obs):
        t0 = time.perf_counter()
        deadline = t0 + SLICE
        t = obs["t"]
        self.t = t
        me = obs["self"]
        pos = me["pos"]
        z = obs["zone"]
        act = {"tick": obs["tick"]}
        label = []

        if obs.get("offer"):
            act["pick"] = self.pick_upgrade(obs["offer"])

        self.ingest(obs, me, t)

        # --- home / zone goal
        zkey = (round(z["next_center"][0], 1), round(z["next_center"][1], 1), z["next_radius"])
        home_bad = self.home is None or zkey != self.home_zone or not self.in_safe(self.home, z, 2)
        if not home_bad and t - self.home_t > 25:
            for e in self.enemies.values():
                if t - e["t"] < 6 and d2(e["pos"], self.home) < 60:
                    home_bad = True
                    break
        if home_bad:
            self.home = self.choose_home(me, z, t)
            self.home_zone = zkey
            self.home_t = t
        self.set_goal(self.home, deadline)

        zc, zr = self.zone_now(z, t)
        dz_now = d2(pos, zc)
        out_now = dz_now > zr - 4
        # time we need to be safe: before the next shrink edge reaches us
        need = 0.0
        if t < z["shrink_end"] and z["next_radius"] < z["radius"]:
            # predicted circle in ~6 s
            c6, r6 = self.zone_now(z, t + 6)
            need = d2(pos, c6) - (r6 - 6)
        urgent = out_now or need > 0 or not me["in_zone"]

        self.climb = False
        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        threats = self.fresh_threats(me, t)
        move = (0.0, 0.0)
        boost = False
        aim = None
        fire = [False, False]

        target = self.pick_target(me, threats, t)
        dodge = self.dodge(obs, me)

        if urgent:
            self.mode = "zone"
            move = self.nav_dir(me, self.home)
            boost = me["energy"] > 8
        elif self.arm_target(me, threats, z, t) is not None:
            self.mode = "arm"
            ap = self.arm_goal
            if self.w.line_walkable(pos, ap) or d2(pos, ap) < 8:
                move = self.straight_dir(pos, ap)
            else:
                self.set_goal(ap, deadline)
                move = self.nav_dir(me, ap)
            boost = me["energy"] > 25 and d2(pos, ap) > 10
        elif target is not None and self.should_retreat(me, threats, target, t):
            self.mode = "retreat"
            rp = self.retreat_point(me, threats, z, t)
            move = self.straight_dir(pos, rp) if rp else self.nav_dir(me, self.home)
            boost = me["energy"] > 15
        elif target is not None:
            self.mode = "fight"
            move = self.fight_move(me, target, z, t)
            boost = self.jink_boost and me["energy"] > 20
            grab = self.grab_nearby(obs, me, z, t)
            if grab is not None:
                move = grab
        elif hp_frac < 0.7 and t - self.last_dmg_t < 25 and self._rec(me, z, t) is not None:
            self.mode = "recover"
            move = self._rec_move
        else:
            lm = self.loot(obs, me, z, t, hp_frac, act)
            if lm is not None:
                self.mode = "loot"
                move = lm
            else:
                self.mode = "hold"
                if d2(pos, self.home) > 5:
                    move = self.nav_dir(me, self.home)
                    boost = d2(pos, self.home) > 60 and me["energy"] > 60 and t < 30
        if dodge is not None and self.mode != "zone":
            move = dodge
            boost = me["energy"] > 10

        climb_jump = False
        if self.climb and (move[0] or move[1]):
            wall = self.w.wall_ahead(pos, move[0], move[1])
            if not me["grounded"]:
                boost = me["energy"] > 2
            elif wall:
                if me["energy"] > 60 or (urgent and me["energy"] > 25):
                    climb_jump = True; boost = True
                else:
                    move = (0.0, 0.0)      # wait for energy before the climb

        # --- aiming and shooting
        if target is not None:
            aim, fire = self.shoot(me, target, t, threats)
        elif self.loot_target is not None and self.mode == "loot":
            aim, fire = self.shoot_loot(me, t)
        if aim is None:
            aim = self.scan_aim(me, move, t)

        # --- weapon pickups nearby
        self.try_take(obs, me, act)

        # --- stuck handling
        move, jump, sboost = self.unstick(me, move, t)
        if jump or climb_jump:
            act["jump"] = True
        boost = boost or sboost

        act["move"] = [round(move[0], 3), round(move[1], 3)]
        act["boost"] = bool(boost)
        act["aim"] = [round(aim[0], 4), round(max(-0.69, min(1.29, aim[1])), 4)]
        if fire[0] or fire[1]:
            act["fire"] = fire
        g = me["weapons"][0]
        if g and g["kind"] == "gun" and g.get("ammo", 1) < g["stats"].get("mag", 12) * 0.5 and target is None and g.get("reload", 0) == 0:
            act["reload"] = True
        self.advance_field(deadline)
        mf.draw.label("%s hp%.0f e%.0f c%d %s" % (self.mode, me["hp"], me["energy"], me["cubes"],
                                                   ("T%d" % target["id"]) if target else ""))
        mf.draw.point([self.home[0], self.home[1], pos[2] + 2], "#40c0ff", 6)
        return act

    # ------------------------------------------------------------------ perception
    def ingest(self, obs, me, t):
        for m in obs["mechs"]:
            if m["kind"] == "mech":
                if m["id"] == me["id"]:
                    continue
                e = self.enemies.get(m["id"])
                if e is None:
                    e = self.enemies[m["id"]] = {"id": m["id"], "first": t}
                e.update(pos=m["pos"], vel=m["vel"], aim=m.get("aim", [0, 0]), hp=m.get("hp", 1400),
                         max_hp=m.get("max_hp", 1400), weapons=m.get("weapons", []), cubes=m.get("cubes", 0),
                         charging=m.get("charging", False), t=t, kind="mech")
            else:
                n = self.npcs.setdefault(m["id"], {"id": m["id"]})
                n.update(pos=m["pos"], vel=m["vel"], hp=m.get("hp", 350), t=t, kind="npc")
        seen = {m["id"] for m in obs["mechs"]}
        for k in [k for k, e in self.enemies.items() if k not in seen and t - e["t"] > 40]:
            del self.enemies[k]
        for k in [k for k, n in self.npcs.items() if k not in seen]:
            del self.npcs[k]
        for ev in obs["events"]:
            ty = ev.get("type")
            if ty == "damaged" and ev.get("by") is not None:
                self.last_dmg_t = t
                self.last_dmg_by[ev["by"]] = t
                self.last_dmg_bearing = (t, ev.get("bearing"))
            elif ty == "damaged":
                pass
            elif ty == "hit":
                self.last_hit_t = t
            elif ty == "took":
                self.pending_take = None
                self.known_pickups.pop(ev.get("id"), None)
                self.arm_goal = None
            elif ty == "take_failed":
                self.bad_loot[ev.get("id")] = t
                self.pending_take = None
            elif ty == "kill":
                self.enemies.pop(ev.get("target"), None)
        for p in obs["pickups"]:
            self.known_pickups[p["id"]] = dict(p, t=t)
        vis = {p["id"] for p in obs["pickups"]}
        for k in [k for k, p in self.known_pickups.items()
                  if k not in vis and (d2(p["pos"], me["pos"]) < 25 or k in self.bad_loot)]:
            del self.known_pickups[k]
        for s in obs["sounds"]:
            if s["kind"] in ("gun", "laser", "lightning", "mortar", "step", "boost", "laser_charge", "jet", "reload"):
                self.sounds.append((t - s.get("age", 0), s["bearing"], s["kind"], s["dist"]))
        self.sounds = [s for s in self.sounds if t - s[0] < 4][-20:]
        self.hist.append((t, pos_copy(me["pos"])))

    def fresh_threats(self, me, t):
        out = []
        for e in self.enemies.values():
            age = t - e["t"]
            if age > 3.0:
                continue
            d = d2(e["pos"], me["pos"])
            e["dist"] = d
            out.append(e)
        return out

    def pick_target(self, me, threats, t):
        best = None
        for e in threats:
            d = e["dist"]
            if d > 260:
                continue
            attacked = t - self.last_dmg_by.get(e["id"], -99) < 6
            hpf = e["hp"] / max(1.0, e["max_hp"])
            # engage when it matters: attacking us, close, hunting us (aiming at us / closing), weak, or late
            bx, by_ = me["pos"][0] - e["pos"][0], me["pos"][1] - e["pos"][1]
            bn = math.hypot(bx, by_) or 1.0
            aiming = abs(wrap(e.get("aim", [9, 0])[0] - math.atan2(by_, bx))) < 0.35
            closing = (e["vel"][0] * bx + e["vel"][1] * by_) / bn > 3.0
            if not (attacked or d < 100 or (d < 175 and (aiming or closing)) or (hpf < 0.4 and d < 175) or self.t > 330):
                continue
            sc = d + 160 * hpf - (120 if attacked else 0) - (40 if t - e["t"] < 0.2 else 0)
            if best is None or sc < best[0]:
                best = (sc, e)
        return best[1] if best else None

    # ------------------------------------------------------------------ fighting
    def should_retreat(self, me, threats, target, t):
        hp = me["hp"]; hpf = hp / max(1.0, me["max_hp"])
        close = [e for e in threats if e["dist"] < 120]
        shooting = [e for e in close if t - self.last_dmg_by.get(e["id"], -99) < 4]
        if self.mode == "retreat" and hpf < 0.5 and len(close) >= 2:
            return True
        if len(shooting) >= 2 and hpf < 0.6 and target["hp"] > hp * 0.5:
            return True
        if len(close) >= 3 and hpf < 0.85:
            return True
        # a lone duel: fight it out unless it is clearly lost
        if len(close) <= 1 and hpf < 0.2 and target["hp"] > hp * 2.5:
            return True
        return False

    def retreat_point(self, me, threats, z, t):
        if self.retreat is not None and t - self.retreat_t < 1.0:
            return self.retreat
        w = self.w
        pos = me["pos"]
        eyes = [(e["pos"][0], e["pos"][1], e["pos"][2] + 8) for e in threats if e["dist"] < 200][:3]
        best = None
        for q in range(16):
            a = q * TAU / 16
            for r in (14.0, 26.0, 40.0):
                x = pos[0] + math.cos(a) * r; y = pos[1] + math.sin(a) * r
                if not w.walkable_at(x, y) or not w.line_walkable(pos, (x, y)):
                    continue
                inz = self.in_safe((x, y), z, 10) or d2((x, y), z["next_center"]) < d2(pos, z["next_center"])
                if not inz:
                    continue
                gz = w.ground(x, y)
                sc = 0.0
                for ey in eyes:
                    if w.los(ey, (x, y, gz + 5.0)):
                        sc += 60
                    sc -= 0.25 * math.hypot(x - ey[0], y - ey[1])
                sc += 0.15 * r + 0.2 * w.expo[w.c4(x, y)]
                if best is None or sc < best[0]:
                    best = (sc, (x, y))
        self.retreat = best[1] if best else None
        self.retreat_t = t
        return self.retreat

    def _rec(self, me, z, t):
        self._rec_move = self.recover_move(me, z, t)
        return self._rec_move

    def recover_move(self, me, z, t):
        """Hide from where enemies were last seen while regenerating."""
        eyes = sorted(((e["pos"][0], e["pos"][1], e["pos"][2] + 8.0) for e in self.enemies.values()
                       if t - e["t"] < 20 and d2(e["pos"], me["pos"]) < 220), key=lambda e: d2(e, me["pos"]))[:2]
        if not eyes:
            return None
        pos = me["pos"]
        if not any(self.w.los(ey, (pos[0], pos[1], pos[2] + 6.0)) for ey in eyes):
            return (0.0, 0.0)          # already hidden: stand still and regen
        hp_ = self.find_hide(me, eyes, z, t)
        if hp_ is None:
            return None
        return self.straight_dir(pos, hp_)

    def find_hide(self, me, eyes, z, t):
        """Nearest reachable spot (<= 14 m) that none of `eyes` can see. Cached briefly."""
        if self.hide_pt is not None and t - self.hide_t < 0.6:
            return self.hide_pt
        w = self.w
        pos = me["pos"]
        best = None
        ey = min(eyes, key=lambda e: d2(e, pos))
        stop = time.perf_counter() + 0.004
        for r in (6.0, 12.0):
            for q in range(10):
                a = q * TAU / 10 + 0.3 * (r > 7)
                x = pos[0] + math.cos(a) * r; y = pos[1] + math.sin(a) * r
                if not w.walkable_at(x, y) or not self.in_safe((x, y), z, 30):
                    continue
                if w.los(ey, (x, y, w.ground(x, y) + 6.0)) or not w.line_walkable(pos, (x, y)):
                    continue
                best = (x, y)
                break
            if best or time.perf_counter() > stop:
                break
        self.hide_pt = best
        self.hide_t = t
        return best

    def fight_move(self, me, tg, z, t):
        pos = me["pos"]
        w = self.w
        d = tg["dist"]
        gun = me["weapons"][0]
        eyes = [(e["pos"][0], e["pos"][1], e["pos"][2] + 8.0) for e in self.fresh_threats(me, t) if e["dist"] < 220][:3]
        reloading = gun is not None and gun["kind"] == "gun" and (gun.get("reload", 0) > 0.25 or gun.get("ammo", 12) == 0)
        has_lightning = any(x and x["kind"] == "lightning" for x in me["weapons"])
        if eyes and reloading and not (has_lightning and d < 40):
            hp_ = self.find_hide(me, eyes, z, t)
            if hp_ is not None:
                self.mode = "duck"
                return self.straight_dir(pos, hp_) if d2(pos, hp_) > 1.5 else (0.0, 0.0)
        to = self.straight_dir(pos, tg["pos"])
        kinds = [x["kind"] for x in me["weapons"] if x]
        their = tg.get("weapons") or []
        want = None
        if "lightning" in kinds and d > 30 and me["hp"] >= tg["hp"] * 0.5 and d < 140:
            want = 1      # close in
        elif "lightning" in their and "lightning" not in kinds and d < 55:
            want = -1     # stay out of arc range
        elif d < 18:
            want = -1
        if t > self.strafe_until:
            if self.rng.random() < 0.8:
                self.strafe_dir = -self.strafe_dir
            self.strafe_until = t + 0.3 + self.rng.random() * 0.45
            self.jink_boost = me["energy"] > 35 and self.rng.random() < 0.6
        sx, sy = -to[1] * self.strafe_dir, to[0] * self.strafe_dir
        mx, my = sx, sy
        if want == 1:
            mx, my = sx * 0.5 + to[0], sy * 0.5 + to[1]
        elif want == -1:
            mx, my = sx * 0.5 - to[0], sy * 0.5 - to[1]
        n = math.hypot(mx, my) or 1.0
        mx, my = mx / n, my / n
        ahead = (pos[0] + mx * 6, pos[1] + my * 6)
        if not w.walkable_at(*ahead) or not self.in_safe(ahead, z, 25):
            self.strafe_dir = -self.strafe_dir
            mx, my = -mx if want is None else mx, -my if want is None else my
            ahead = (pos[0] + mx * 6, pos[1] + my * 6)
            if not w.walkable_at(*ahead):
                return self.nav_dir(me, self.home)
        return (mx, my)

    def shoot(self, me, tg, t, threats):
        pos = me["pos"]
        scale = 1 + 0.05 * (me.get("cubes") or 0)
        mz = pos[2] + me["stats"].get("muzzle_height", 7 * scale)
        tc = self.center(tg)
        vel = tg.get("vel", [0, 0, 0])
        fresh = t - tg["t"] < 0.15
        w = self.w
        muzzle = (pos[0], pos[1], mz)
        clear = fresh and w.los(muzzle, tc)
        d = d2(pos, tc)
        fire = [False, False]
        aim = None
        cur = me["aim"]
        weapons = me["weapons"]
        healing = self.mode == "retreat" and d > 140
        for slot in (1, 0):
            wp = weapons[slot] if slot < len(weapons) else None
            if not wp:
                continue
            k = wp["kind"]; st = wp["stats"]
            if k == "gun":
                sp = st.get("speed", 252)
                tt = d / sp
                p = tc
                for _ in range(2):
                    p = (tc[0] + vel[0] * tt, tc[1] + vel[1] * tt, tc[2] + vel[2] * tt * 0.5)
                    tt = math.dist(muzzle, p) / sp
                ya = math.atan2(p[1] - pos[1], p[0] - pos[0])
                pa = math.atan2(p[2] - mz, d2(pos, p))
                if aim is None:
                    aim = (ya, pa)
                rad = 2.5 * (1 + 0.05 * (tg.get("cubes") or 0))
                tol = math.atan2(rad * 1.1, max(1.0, d)) + 0.006
                ok = abs(wrap(cur[0] - ya)) < tol and abs(cur[1] - pa) < tol * 2.0
                if clear and ok and d < st.get("range", 230) and wp.get("reload", 0) == 0 and not healing:
                    fire[slot] = True
            elif k == "lightning":
                ya = math.atan2(tc[1] - pos[1], tc[0] - pos[0])
                pa = math.atan2(tc[2] - mz, d)
                if aim is None or d < st.get("range", 45) * 1.2:
                    aim = (ya, pa)
                cone = math.radians(st.get("cone_deg", 30)) * 0.4
                if clear and d < st.get("range", 45) * 0.95 and abs(wrap(cur[0] - ya)) < cone and me["energy"] > 6 + st.get("energy", 3):
                    fire[slot] = True
            elif k == "laser":
                p = (tc[0] + vel[0] * 0.1, tc[1] + vel[1] * 0.1, tc[2])
                ya = math.atan2(p[1] - pos[1], p[0] - pos[0])
                pa = math.atan2(p[2] - mz, d2(pos, p))
                if aim is None or d > 50:
                    aim = (ya, pa)
                full = st.get("charge", 0.75)
                ch = wp.get("charge", 0) or 0
                tol = max(0.01, math.atan2(1.6, max(1.0, d)))
                ok = abs(wrap(cur[0] - ya)) < tol and abs(cur[1] - pa) < tol * 1.5
                if ch > 0:
                    # charging: keep holding until full and on target, then release
                    if ch >= full * 0.98 and ok and clear:
                        fire[slot] = False
                    elif ch > full + st.get("hold_s", 2) * 0.7:
                        fire[slot] = False
                    else:
                        fire[slot] = True
                elif wp.get("cooldown", 0) <= 0.05 and clear and d < st.get("range", 280) and not healing:
                    fire[slot] = True
            elif k == "mortar":
                sp = st.get("speed", 72); g = st.get("gravity", 22)
                tz = tg["pos"][2] + 1.0
                dh = tz - (pos[2] + me["stats"].get("height", 9) * 1.05)
                if 20 < d < 225 and wp.get("cooldown", 0) <= 0.05 and not healing:
                    sol = ballistic(d, dh, sp, g, high=not clear)
                    if sol is not None:
                        ft = d / (sp * math.cos(sol))
                        lx = tg["pos"][0] + vel[0] * ft; ly = tg["pos"][1] + vel[1] * ft
                        dl = d2(pos, (lx, ly))
                        sol2 = ballistic(dl, dh, sp, g, high=not clear)
                        if sol2 is not None:
                            ya = math.atan2(ly - pos[1], lx - pos[0])
                            if aim is None or (weapons[0] is None or not clear):
                                aim = (ya, sol2)
                            if abs(wrap(cur[0] - ya)) < 0.03 and abs(cur[1] - sol2) < 0.03 and (clear or t - tg["t"] < 1.5):
                                fire[slot] = True
        if aim is None:
            aim = (math.atan2(tc[1] - pos[1], tc[0] - pos[0]), 0.0)
        return aim, fire

    def dodge(self, obs, me):
        pos = me["pos"]
        best = None
        for p in obs["projectiles"]:
            if p["kind"] != "mortar":
                continue
            g = self.cfg["weapons"]["mortar"].get("gravity", 22)
            vz = p["vel"][2]; z0 = p["pos"][2] - me["pos"][2]
            disc = vz * vz + 2 * g * z0
            if disc < 0:
                continue
            tt = (vz + math.sqrt(disc)) / g
            lx = p["pos"][0] + p["vel"][0] * tt; ly = p["pos"][1] + p["vel"][1] * tt
            dd = d2(pos, (lx, ly))
            if dd < 10 and tt < 3:
                awayx, awayy = pos[0] - lx, pos[1] - ly
                n = math.hypot(awayx, awayy)
                if n < 0.5:
                    awayx, awayy, n = -p["vel"][1], p["vel"][0], math.hypot(p["vel"][0], p["vel"][1]) or 1
                cand = (awayx / n, awayy / n)
                if self.w.walkable_at(pos[0] + cand[0] * 5, pos[1] + cand[1] * 5):
                    best = cand
        if best:
            return best
        for e in self.enemies.values():
            if e.get("charging") and self.t - e["t"] < 0.15:
                ya = math.atan2(pos[1] - e["pos"][1], pos[0] - e["pos"][0])
                if abs(wrap(e["aim"][0] - ya)) < 0.08:
                    perp = (-math.sin(ya) * self.strafe_dir, math.cos(ya) * self.strafe_dir)
                    if self.w.walkable_at(pos[0] + perp[0] * 5, pos[1] + perp[1] * 5):
                        return perp
                    return (-perp[0], -perp[1])
        return None

    # ------------------------------------------------------------------ loot
    def loot(self, obs, me, z, t, hp_frac, act):
        pos = me["pos"]
        w = self.w
        near_threat = any(t - e["t"] < 3 and d2(e["pos"], pos) < 150 for e in self.enemies.values())
        calm = not near_threat and t - self.last_dmg_t > 3
        self.loot_target = None
        if not calm:
            return None
        best = None

        def consider(score, kind, item, approach):
            nonlocal best
            if best is None or score < best[0]:
                best = (score, kind, item, approach)

        gz = w.ground(pos[0], pos[1])
        for c in obs["cubes"]:
            d = d2(pos, c["pos"])
            if d < 90 and c["pos"][2] < w.ground(c["pos"][0], c["pos"][1]) + 3 and self.in_safe(c["pos"], z, 30) \
                    and t - self.bad_loot.get(c["id"], -99) > 10:
                consider(d * 0.6, "cube", c, c["pos"])
        held = [x["kind"] for x in me["weapons"] if x]
        slot1 = me["weapons"][1] if len(me["weapons"]) > 1 else None
        for p in obs["pickups"]:
            if p["kind"] in held or t - self.bad_loot.get(p["id"], -99) < 8:
                continue
            gain = PREF.get(p["kind"], 0) - (PREF.get(slot1["kind"], 0) if slot1 else -1)
            if gain <= 0.4:
                continue
            d = d2(pos, p["pos"])
            if d < 110 and p["pos"][2] < w.ground(p["pos"][0], p["pos"][1]) + 3 and self.in_safe(p["pos"], z, 30):
                consider(d * 0.7 - 20 * gain, "pickup", p, p["pos"])
        if hp_frac > 0.5 and t < 380:
            for c in obs["crates"]:
                d = d2(pos, c["pos"])
                if d < 80 and c["pos"][2] < w.ground(c["pos"][0], c["pos"][1]) + 3 and self.in_safe(c["pos"], z, 20):
                    consider(d + 20, "crate", c, c["pos"])
        if hp_frac > 0.7 and t < 300:
            for n in self.npcs.values():
                d = d2(pos, n["pos"])
                if d < 85 and self.in_safe(n["pos"], z, 20):
                    consider(d + 40, "npc", n, n["pos"])
        for dr in obs["drops"]:
            d = d2(pos, dr["pos"])
            others_near = any(d2(e["pos"], dr["pos"]) < 120 and t - e["t"] < 15 for e in self.enemies.values())
            if d < 170 and not others_near and dr["lands_in"] < d / 11 + 4 and hp_frac > 0.4 and \
                    self.in_safe(dr["pos"], z, 15) and (dr["holder"] in (None, me["id"])):
                consider(d * 0.4 - 30, "drop", dr, dr["pos"])
        if best is None:
            return None
        _, kind, item, ap = best
        self.loot_target = (kind, item)
        d = d2(pos, ap)
        if kind in ("crate", "npc"):
            standoff = 20.0 if kind == "crate" else 30.0
            if any(x and x["kind"] == "lightning" for x in me["weapons"]):
                standoff = 28.0
            if d < standoff and w.los((pos[0], pos[1], pos[2] + 7), (ap[0], ap[1], ap[2] + 1.5)):
                # in position: strafe gently for npcs, stand for crates
                if kind == "npc":
                    to = self.straight_dir(pos, ap)
                    if t > self.strafe_until:
                        self.strafe_dir = -self.strafe_dir
                        self.strafe_until = t + 0.8 + self.rng.random()
                    pr = (-to[1] * self.strafe_dir, to[0] * self.strafe_dir)
                    if w.walkable_at(pos[0] + pr[0] * 5, pos[1] + pr[1] * 5):
                        return pr
                return (0.0, 0.0)
        if kind == "drop" and d < dr_radius(item) * 0.5:
            return (0.0, 0.0)
        if w.line_walkable(pos, ap) or d < 6:
            return self.straight_dir(pos, ap)
        # not straight-reachable: skip it for a while
        self.bad_loot[item.get("id")] = t
        self.loot_target = None
        return None

    def arm_target(self, me, threats, z, t):
        slot1 = me["weapons"][1] if len(me["weapons"]) > 1 else None
        cur = PREF.get(slot1["kind"], 0) if slot1 else -1
        if cur >= PREF["lightning"]:
            self.arm_goal = None
            return None
        pos = me["pos"]
        held = [x["kind"] for x in me["weapons"] if x]
        tclose = min([e["dist"] for e in threats] + [999])
        best = None
        for p in self.known_pickups.values():
            if p["kind"] in held or t - self.bad_loot.get(p["id"], -99) < 8:
                continue
            gain = PREF.get(p["kind"], 0) - cur
            if gain <= 0.4:
                continue
            d = d2(pos, p["pos"])
            lim = 170 if slot1 is None else 70
            if d > lim or abs(p["pos"][2] - self.w.ground(p["pos"][0], p["pos"][1])) > 3 or not self.in_safe(p["pos"], z, 30):
                continue
            # with an enemy close, only go if the weapon is nearer than the fight
            if tclose < 60 and d > max(25.0, tclose * 0.8):
                continue
            sc = d - 25 * gain
            if best is None or sc < best[0]:
                best = (sc, p)
        if best is None:
            self.arm_goal = None
            return None
        self.arm_goal = (best[1]["pos"][0], best[1]["pos"][1])
        return best[1]

    def grab_nearby(self, obs, me, z, t):
        """Mid-fight: a cube or a better weapon within a few strides is worth stepping to."""
        pos = me["pos"]
        held = [x["kind"] for x in me["weapons"] if x]
        slot1 = me["weapons"][1] if len(me["weapons"]) > 1 else None
        best = None
        for c in obs["cubes"]:
            d = d2(pos, c["pos"])
            if d < 22 and abs(c["pos"][2] - pos[2]) < 3.5 and (best is None or d < best[0]):
                best = (d, c["pos"])
        for p in obs["pickups"]:
            if p["kind"] in held:
                continue
            gain = PREF.get(p["kind"], 0) - (PREF.get(slot1["kind"], 0) if slot1 else -1)
            d = d2(pos, p["pos"])
            if gain > 0.4 and d < 28 and abs(p["pos"][2] - pos[2]) < 3.5 and (best is None or d - 8 < best[0]):
                best = (d - 8, p["pos"])
        if best is None or not self.w.line_walkable(pos, best[1]):
            return None
        return self.straight_dir(pos, best[1])

    def shoot_loot(self, me, t):
        kind, item = self.loot_target
        if kind not in ("crate", "npc"):
            return None, [False, False]
        pos = me["pos"]
        mz = pos[2] + me["stats"].get("muzzle_height", 7)
        h = 1.2 if kind == "crate" else 1.75
        tc = (item["pos"][0], item["pos"][1], item["pos"][2] + h)
        d = d2(pos, tc)
        if kind == "npc":
            v = item.get("vel", [0, 0, 0]); tt = d / 252
            tc = (tc[0] + v[0] * tt, tc[1] + v[1] * tt, tc[2])
        ya = math.atan2(tc[1] - pos[1], tc[0] - pos[0]); pa = math.atan2(tc[2] - mz, d)
        fire = [False, False]
        cur = me["aim"]
        if not self.w.los((pos[0], pos[1], mz), tc):
            return (ya, pa), fire
        tol = max(0.012, math.atan2(1.0, max(1.0, d)))
        for slot, wp in enumerate(me["weapons"]):
            if not wp:
                continue
            k = wp["kind"]
            if k == "gun" and d < 120 and abs(wrap(cur[0] - ya)) < tol and abs(cur[1] - pa) < tol * 1.5 and wp.get("reload", 0) == 0:
                fire[slot] = True
            elif k == "lightning" and d < wp["stats"].get("range", 45) * 0.9 and abs(wrap(cur[0] - ya)) < 0.2 and me["energy"] > 30:
                fire[slot] = True
        return (ya, pa), fire

    def try_take(self, obs, me, act):
        pos = me["pos"]
        held = [x["kind"] if x else None for x in me["weapons"]]
        for p in obs["pickups"]:
            if abs(p["pos"][0] - pos[0]) > 3.8 or abs(p["pos"][1] - pos[1]) > 3.8:
                continue
            if math.hypot(p["pos"][0] - pos[0], p["pos"][1] - pos[1]) > 3.8 or abs(p["pos"][2] - pos[2]) > 3.8:
                continue
            if p["kind"] in held:
                continue
            if held[1] is None:
                act["take"] = p["id"]
                return
            cur = me["weapons"][1]
            if PREF.get(p["kind"], 0) > PREF.get(held[1], 0) + 0.4 and not (cur.get("charge") or 0) > 0:
                act["take"] = p["id"]; act["drop"] = 1
                return

    # ------------------------------------------------------------------ misc
    def scan_aim(self, me, move, t):
        # face the most recent damage / sound, else sweep
        if self.last_dmg_bearing and t - self.last_dmg_bearing[0] < 3 and self.last_dmg_bearing[1] is not None:
            return (self.last_dmg_bearing[1], 0.0)
        for s in reversed(self.sounds):
            if t - s[0] < 2.0 and s[2] in ("gun", "laser", "lightning", "mortar", "laser_charge") or (s[3] == "near" and t - s[0] < 1.5):
                return (s[1], 0.0)
        # remembered enemies (stale) — look where they were
        best = None
        for e in self.enemies.values():
            age = t - e["t"]
            if age < 15:
                d = d2(e["pos"], me["pos"])
                if best is None or d < best[0]:
                    best = (d, e)
        if best and best[0] < 220:
            e = best[1]
            return (math.atan2(e["pos"][1] - me["pos"][1], e["pos"][0] - me["pos"][0]), 0.0)
        if self.mode in ("zone", "loot") and (move[0] or move[1]):
            return (math.atan2(move[1], move[0]), 0.0)
        self.scan_yaw = wrap(self.scan_yaw + 0.13)
        return (self.scan_yaw, 0.0)

    def unstick(self, me, move, t):
        jump = False; boost = False
        if t < self.stuck_until:
            c, s = math.cos(self.stuck_turn), math.sin(self.stuck_turn)
            mv = (move[0] * c - move[1] * s, move[0] * s + move[1] * c)
            return mv, me["grounded"], me["energy"] > 20
        wants = math.hypot(*move) > 0.5
        if wants and len(self.hist) >= 12:
            t_old, p_old = self.hist[0]
            if t - t_old > 1.0 and d2(p_old, me["pos"]) < 1.5:
                self.stuck_until = t + 0.8
                self.stuck_turn = self.rng.choice((-1.1, 1.1))
                self.hist.clear()
                return move, me["grounded"], True
        return move, jump, boost

    def pick_upgrade(self, offer):
        ids = [c["id"] for c in offer["choices"]]
        for u in UPGRADE_ORDER:
            if u in ids:
                return ids.index(u)
        return 0


def dr_radius(item):
    return item.get("radius", 8)


def pos_copy(p):
    return (p[0], p[1], p[2])


def ballistic(d, h, v, g, high=False):
    """Launch pitch to hit horizontal distance d, height difference h. None if out of range."""
    v2 = v * v
    disc = v2 * v2 - g * (g * d * d + 2 * h * v2)
    if disc < 0 or d < 1e-3:
        return None
    r = math.sqrt(disc)
    a = math.atan2(v2 + r if high else v2 - r, g * d)
    return a


if __name__ == "__main__":
    mf.run(Bot)
