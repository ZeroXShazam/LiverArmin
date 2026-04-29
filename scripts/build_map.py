#!/usr/bin/env python3
"""
Build chatarmin-office.tmj for WorkAdventure from a structured floorplan
description (floorplan.json), produced by feeding the architect's drawing
through Gemini with scripts/PROMPT.md.

  python3 scripts/build_map.py [floorplan.json]

The JSON is the source of truth for *what* the office looks like. This file
is the source of truth for *how* it gets translated into Tiled JSON: which
tile IDs render each room, furniture kind, and wall family.

Pipeline:
  Gemini → floorplan.json → build_map.py → chatarmin-office.tmj
                                         → render_map.py → preview.png
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---- tile palette (sampled from office.tmj — known-good IDs) ---------------
TILE = 32

# floors
T_GRASS         = 2461
T_FLOOR_WOOD    = 725
T_FLOOR_GANG    = 725   # TODO: distinct gang-carpet tile when identified
T_FLOOR_GREEN   = 725   # TODO: distinct green-carpet tile
T_FLOOR_BATH    = 725   # TODO: distinct bathroom-tiles
T_FLOOR_STONE   = 2461  # TODO: distinct stone-pavement tile (use grass for now)

FLOOR_TILES = {
    "wood":            T_FLOOR_WOOD,
    "gang_carpet":     T_FLOOR_GANG,
    "green_carpet":    T_FLOOR_GREEN,
    "bathroom_tiles":  T_FLOOR_BATH,
    "stone_pavement":  T_FLOOR_STONE,
    "grass":           T_GRASS,
}

# walls (orientation-aware)
T_WALL_TOP       = 479
T_WALL_BOT       = 685
T_WALL_SIDE      = 477
T_WALL_CORNER_TL = 403
T_WALL_CORNER_TR = 404
T_WALL_CORNER_BL = 433
T_WALL_CORNER_BR = 429
T_WALL_GENERIC   = 685

# furniture
T_DESK_TOP    = 1569
T_DESK_BOT    = 1579

# Tiled flip flags (used for the few cases where a sprite has no dedicated
# opposite-orientation variant in the WA tilesets).
TILED_FLIP_V  = 0x40000000
TILED_FLIP_H  = 0x80000000

# Chair tiles in WA_Seats (firstgid 1375). The sheet ships one chair sprite
# per cardinal direction; pick the right one rather than vertically flipping.
T_CHAIR_S     = 1492   # back at NORTH, person faces SOUTH
T_CHAIR_N     = 1493   # back at SOUTH, person faces NORTH
T_CHAIR_E     = 1494   # back at WEST,  person faces EAST
T_CHAIR_W     = 1495   # back at EAST,  person faces WEST
# Legacy aliases used by older renderers that haven't been migrated.
T_CHAIR_LEFT  = T_CHAIR_E
T_CHAIR_RIGHT = T_CHAIR_W

# Monitor tiles in WA_Miscellaneous (firstgid 109).
# 109/110 are the FRONT view (lit screen visible — monitor facing SOUTH);
# 119/120 are the BACK view (dark plastic visible — monitor facing NORTH).
# Use BACK whenever the screen "looks north" (i.e. the person sits north of
# the desk and we, the south camera, see the monitor's back panel).
T_MONITOR_FRONT = 110   # screen faces SOUTH (front visible to south viewer)
T_MONITOR_BACK  = 120   # screen faces NORTH (back  visible to south viewer)
# Dual-monitor halves used by some grouped-desk setups.
T_MONITOR_L   = 134
T_MONITOR_R   = 133

# Laptop tiles in WA_Miscellaneous — same front/back convention as monitors.
T_LAPTOP_FRONT = 112    # open laptop, screen faces SOUTH
T_LAPTOP_BACK  = 114    # closed/back laptop, screen faces NORTH
T_ROUND       = 1525   # 1×1 round table with chairs
T_PLANT       = 90
T_SCREEN      = 187    # whiteboard / screen
T_TOILET      = 245    # WA_Other_Furniture toilet (approx)
T_SINK        = 240    # WA_Other_Furniture sink (approx)
T_COUNTER     = 1567   # generic table; reused as kitchen counter
T_MIRROR_L    = 267    # WA_Other_Furniture wall mirror (left half / standalone)
T_MIRROR_R    = 268    # WA_Other_Furniture wall mirror (right half)

# special-zones
T_START = 2
T_BLOCK = 3

# ---- tilesets (inline-embedded, copied from office.tmj) --------------------
TILESETS = json.loads((ROOT / "office.tmj").read_text())["tilesets"]


# =============================================================================
# Layer helpers
# =============================================================================

def make_state(width, height):
    return {
        "W": width,
        "H": height,
        "floor":      [0] * (width * height),
        "walls":      [0] * (width * height),
        "collisions": [0] * (width * height),
        "furniture1": [0] * (width * height),
        "furniture2": [0] * (width * height),
        "furniture3": [0] * (width * height),
        "above1":     [0] * (width * height),
        "start":      [0] * (width * height),
    }


def idx(s, x, y):
    return y * s["W"] + x


def in_bounds(s, x, y):
    return 0 <= x < s["W"] and 0 <= y < s["H"]


def fill_rect(layer, s, x, y, w, h, tile):
    for yy in range(y, y + h):
        for xx in range(x, x + w):
            if in_bounds(s, xx, yy):
                layer[idx(s, xx, yy)] = tile


def set_tile(layer, s, x, y, tile):
    if in_bounds(s, x, y):
        layer[idx(s, x, y)] = tile


# =============================================================================
# Rooms, walls, doors
# =============================================================================

def draw_room_floor(s, room):
    r = room["rect"]
    floor_tile = FLOOR_TILES.get(room.get("floor", "wood"), T_FLOOR_WOOD)
    fill_rect(s["floor"], s, r["x"], r["y"], r["w"], r["h"], floor_tile)


def draw_outdoor_floor(s, area):
    r = area["rect"]
    floor_tile = FLOOR_TILES.get(area.get("floor", "stone_pavement"), T_GRASS)
    fill_rect(s["floor"], s, r["x"], r["y"], r["w"], r["h"], floor_tile)


def draw_outdoor_walls(s, area):
    """
    Mark every perimeter cell of an outdoor area as a wall. classify_walls()
    will later open up cells whose other side is a room, so a terrasse's
    facade with the building stays an open passage while the other three
    sides become an enclosing fence.
    """
    r = area["rect"]
    x0, y0 = r["x"], r["y"]
    x1, y1 = x0 + r["w"] - 1, y0 + r["h"] - 1
    for x in range(x0, x1 + 1):
        for y in (y0, y1):
            if in_bounds(s, x, y):
                s["walls"][idx(s, x, y)] = T_WALL_GENERIC
                s["collisions"][idx(s, x, y)] = T_BLOCK
    for y in range(y0, y1 + 1):
        for x in (x0, x1):
            if in_bounds(s, x, y):
                s["walls"][idx(s, x, y)] = T_WALL_GENERIC
                s["collisions"][idx(s, x, y)] = T_BLOCK


def draw_room_walls(s, room, door_cells):
    """
    Mark every cell on the room's perimeter as a wall (T_WALL_GENERIC
    placeholder, replaced later by pick_wall_tiles). Skip cells listed in
    `door_cells`.
    """
    r = room["rect"]
    x0, y0 = r["x"], r["y"]
    x1, y1 = x0 + r["w"] - 1, y0 + r["h"] - 1

    for x in range(x0, x1 + 1):
        for y in (y0, y1):
            if (x, y) in door_cells:
                continue
            if in_bounds(s, x, y):
                s["walls"][idx(s, x, y)] = T_WALL_GENERIC
                s["collisions"][idx(s, x, y)] = T_BLOCK
    for y in range(y0, y1 + 1):
        for x in (x0, x1):
            if (x, y) in door_cells:
                continue
            if in_bounds(s, x, y):
                s["walls"][idx(s, x, y)] = T_WALL_GENERIC
                s["collisions"][idx(s, x, y)] = T_BLOCK


def door_wall_cells(room, door):
    """
    Return the wall-cells a door covers on `room`'s perimeter, expanded by
    `door['w']`. The door coordinate from Gemini may sit on either room's
    wall or in the gap between two rooms; we return the closest perimeter
    tile per cell of width.
    """
    r = room["rect"]
    x0, y0 = r["x"], r["y"]
    x1, y1 = x0 + r["w"] - 1, y0 + r["h"] - 1
    dx, dy = door["x"], door["y"]
    width = max(1, int(door.get("w", 1)))

    # snap door cell into the room's perimeter
    cells = set()
    if dy <= y0 + 1:
        # top wall
        for k in range(width):
            cells.add((dx + k, y0))
    elif dy >= y1 - 1:
        # bottom wall
        for k in range(width):
            cells.add((dx + k, y1))
    elif dx <= x0 + 1:
        # left wall
        for k in range(width):
            cells.add((x0, dy + k))
    elif dx >= x1 - 1:
        # right wall
        for k in range(width):
            cells.add((x1, dy + k))
    else:
        # door is entirely inside the room — just open the door tile
        for k in range(width):
            cells.add((dx + k, dy))
    return cells


def open_door_passage(s, room_a, room_b, door):
    """
    Clear walls around a door so the player can actually cross between
    `room_a` and `room_b` even if there's a 1-tile gap of "outside" between
    their perimeters. We open both rooms' shared edge plus the gap.
    """
    cells_a = door_wall_cells(room_a, door)
    if room_b is not None:
        cells_b = door_wall_cells(room_b, door)
    else:
        cells_b = set()

    # Clear walls + collisions on both sides
    for x, y in cells_a | cells_b:
        if in_bounds(s, x, y):
            s["walls"][idx(s, x, y)] = 0
            s["collisions"][idx(s, x, y)] = 0

    # Bridge any 1-2 tile gap between rooms with floor + clear collisions
    # so the player isn't stranded on grass.
    bridge_floor = FLOOR_TILES.get((room_a.get("floor") or "wood"), T_FLOOR_WOOD)
    for x, y in cells_a:
        for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            nx, ny = x + dx, y + dy
            if not in_bounds(s, nx, ny):
                continue
            if s["floor"][idx(s, nx, ny)] in (0, T_GRASS):
                s["floor"][idx(s, nx, ny)] = bridge_floor
            # don't add walls; if it's already a wall on the opposite room's
            # perimeter, we leave it.


def is_wall(s, x, y):
    return in_bounds(s, x, y) and s["walls"][idx(s, x, y)] != 0


def classify_walls(s, plan):
    """
    After geometric walls are placed, look at each wall cell and decide
    whether it sits on:

      (a) the building exterior (one side interior, other side grass) → keep
      (b) an inner partition between two rooms                        → mirror
      (c) the boundary between a room and an outdoor terrasse         → OPEN
          (wall + collision both cleared, floor extended out)

    We tag every cell with the room/area it belongs to (via per-room rect),
    then probe each wall cell at distance 1 *and* 2 in all four cardinal
    directions — that bridges the 1-tile gap left between adjacent rooms'
    perimeter walls.
    """
    W, H = s["W"], s["H"]
    zone = [None] * (W * H)

    for room in plan["rooms"]:
        r = room["rect"]
        for yy in range(r["y"] + 1, r["y"] + r["h"] - 1):
            for xx in range(r["x"] + 1, r["x"] + r["w"] - 1):
                if in_bounds(s, xx, yy):
                    zone[idx(s, xx, yy)] = ("room", room["id"])
    for area in plan.get("outdoor_areas", []):
        r = area["rect"]
        for yy in range(r["y"], r["y"] + r["h"]):
            for xx in range(r["x"], r["x"] + r["w"]):
                if in_bounds(s, xx, yy):
                    zone[idx(s, xx, yy)] = ("terrasse", area["id"])

    floor_for_room = {
        room["id"]: FLOOR_TILES.get(room.get("floor", "wood"), T_FLOOR_WOOD)
        for room in plan["rooms"]
    }

    for y in range(H):
        for x in range(W):
            i = idx(s, x, y)
            if s["walls"][i] == 0:
                continue

            # nearest non-wall zone in each cardinal direction
            zones_found = []
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                for dist in (1, 2):
                    nx, ny = x + dx * dist, y + dy * dist
                    if not in_bounds(s, nx, ny):
                        continue
                    z = zone[idx(s, nx, ny)]
                    if z is not None:
                        zones_found.append(z)
                        break  # closest zone in this direction

            kinds = {z[0] for z in zones_found}
            ids = {z[1] for z in zones_found}

            if "room" in kinds and "terrasse" in kinds:
                # facade to terrasse → open it up
                s["walls"][i] = 0
                s["collisions"][i] = 0
                # extend floor out so the player isn't standing on grass
                room_ids = [z[1] for z in zones_found if z[0] == "room"]
                if room_ids:
                    s["floor"][i] = floor_for_room.get(room_ids[0], T_FLOOR_WOOD)
            elif kinds == {"room"} and len(ids) >= 2:
                # inner partition between two distinct rooms → mirror
                s["walls"][i] = T_MIRROR_L if (x + y) % 2 == 0 else T_MIRROR_R


def pick_wall_tiles(s):
    """Replace generic wall markers with orientation-aware tiles."""
    out = list(s["walls"])
    for y in range(s["H"]):
        for x in range(s["W"]):
            if s["walls"][idx(s, x, y)] == 0:
                continue
            up = is_wall(s, x, y - 1)
            dn = is_wall(s, x, y + 1)
            lf = is_wall(s, x - 1, y)
            rt = is_wall(s, x + 1, y)

            # exterior corners (90° turns)
            if dn and rt and not up and not lf:
                tile = T_WALL_CORNER_TL
            elif dn and lf and not up and not rt:
                tile = T_WALL_CORNER_TR
            elif up and rt and not dn and not lf:
                tile = T_WALL_CORNER_BL
            elif up and lf and not dn and not rt:
                tile = T_WALL_CORNER_BR
            elif lf and rt:
                if not up:
                    tile = T_WALL_TOP
                elif not dn:
                    tile = T_WALL_BOT
                else:
                    tile = T_WALL_SIDE
            elif up and dn:
                tile = T_WALL_SIDE
            else:
                tile = T_WALL_SIDE
            out[idx(s, x, y)] = tile
    s["walls"] = out


# =============================================================================
# Furniture renderers
# =============================================================================

def render_desk_individual(s, item, registry=None):
    """
    1-wide × 2-tall desk + chair below. Returns the list of "person seat"
    coordinates (just the desk top tile) so the caller can name desk_<rep>
    object zones over them.
    """
    x, y = item["x"], item["y"]
    # Person sits in the chair at y+2 facing NORTH towards the desk at y.
    # Monitor screen therefore faces SOUTH (towards the person and viewer).
    set_tile(s["furniture2"], s, x, y, T_DESK_TOP)
    set_tile(s["furniture3"], s, x, y, T_MONITOR_FRONT)
    set_tile(s["furniture2"], s, x, y + 1, T_DESK_BOT)
    set_tile(s["furniture2"], s, x, y + 2, T_CHAIR_N)
    return [(x, y)]


def render_desk_cluster(s, item, registry=None):
    """
    Two rows of `people_per_row` desks placed back-to-back, with chairs on
    BOTH sides of the cluster.

    Footprint: people_per_row tiles wide × 6 tiles tall.

      y+0  chair chair chair ...   ← top row, person sits north of desk
      y+1  desk_top + monitor      ← top row, desk back wall
      y+2  desk_bot                ← top row, desk legs
      y+3  desk_bot                ← bottom row, desk legs
      y+4  desk_top + monitor      ← bottom row, desk back wall
      y+5  chair chair chair ...   ← bottom row, person sits south of desk

    Returns the seat list ordered top-row left→right, then bottom-row L→R,
    so build_objects() can hand the first N seats to the SDR squad.
    """
    x, y = item["x"], item["y"]
    n = max(1, int(item.get("people_per_row", 8)))
    seats_top = []
    seats_bot = []
    for i in range(n):
        cx = x + i
        # Top row: person sits at y (north of desk), faces SOUTH.
        # Monitor on the desk at y+1 faces NORTH (towards the person), so
        # from the south camera we see its BACK panel.
        set_tile(s["furniture2"], s, cx, y,     T_CHAIR_S)
        set_tile(s["furniture2"], s, cx, y + 1, T_DESK_TOP)
        set_tile(s["furniture3"], s, cx, y + 1, T_MONITOR_BACK)
        set_tile(s["furniture2"], s, cx, y + 2, T_DESK_BOT)
        # Bottom row: person sits at y+5 (south of desk), faces NORTH.
        # Monitor on the desk at y+4 faces SOUTH (towards the person and the
        # camera), so we see its FRONT (lit screen).
        set_tile(s["furniture2"], s, cx, y + 3, T_DESK_BOT)
        set_tile(s["furniture2"], s, cx, y + 4, T_DESK_TOP)
        set_tile(s["furniture3"], s, cx, y + 4, T_MONITOR_FRONT)
        set_tile(s["furniture2"], s, cx, y + 5, T_CHAIR_N)
        seats_top.append((cx, y))
        seats_bot.append((cx, y + 5))
    return seats_top + seats_bot


def render_desk_grouped(s, item, registry=None):
    """
    A horizontal/vertical cluster of `people` desks back-to-back.

    horizontal:
       row+0: chair chair chair chair  ← chairs above desks (row above)
       row+1: desk  desk  desk  desk  (top w/ monitor)
       row+2: desk  desk  desk  desk  (bot)
       row+3: chair chair chair chair  ← chairs below

    vertical:
       col+0..2 wide:  chair / desk / desk / chair
                       chair / desk / desk / chair  ...

    Returns a list of (x,y) per individual person spot.
    """
    x, y = item["x"], item["y"]
    n = max(1, int(item.get("people", 4)))
    orient = item.get("orientation", "horizontal")
    seats = []

    if orient == "horizontal":
        # n single-sided desks all facing south. Person sits at y+2 facing
        # NORTH toward the desk at y; monitor screen faces SOUTH.
        for i in range(n):
            cx = x + i
            set_tile(s["furniture2"], s, cx, y,     T_DESK_TOP)
            set_tile(s["furniture3"], s, cx, y,     T_MONITOR_FRONT)
            set_tile(s["furniture2"], s, cx, y + 1, T_DESK_BOT)
            set_tile(s["furniture2"], s, cx, y + 2, T_CHAIR_N)
            seats.append((cx, y))
    else:  # vertical
        # Stack of single desks; chair sits to the WEST of each desk and the
        # person faces EAST toward the desk.
        for i in range(n):
            cy = y + i * 2
            set_tile(s["furniture2"], s, x,     cy,     T_CHAIR_E)
            set_tile(s["furniture2"], s, x + 1, cy,     T_DESK_TOP)
            set_tile(s["furniture3"], s, x + 1, cy,     T_MONITOR_L)
            set_tile(s["furniture2"], s, x + 1, cy + 1, T_DESK_BOT)
            seats.append((x + 1, cy))
    return seats


def render_round_table(s, item, registry=None):
    set_tile(s["furniture2"], s, item["x"], item["y"], T_ROUND)


def render_round_table_outdoor(s, item, registry=None):
    set_tile(s["furniture2"], s, item["x"], item["y"], T_ROUND)


def render_meeting_table(s, item, registry=None):
    """Solid-coloured rectangle of T_DESK_TOP tiles."""
    fill_rect(s["furniture2"], s, item["x"], item["y"], item.get("w", 2), item.get("h", 2), T_DESK_TOP)


def render_kitchen_counter(s, item, registry=None):
    fill_rect(s["furniture2"], s, item["x"], item["y"],
              item.get("w", 1), item.get("h", 1), T_COUNTER)
    fill_rect(s["collisions"], s, item["x"], item["y"],
              item.get("w", 1), item.get("h", 1), T_BLOCK)


def render_lounge_set(s, item, registry=None):
    fill_rect(s["furniture2"], s, item["x"], item["y"],
              item.get("w", 2), item.get("h", 2), T_ROUND)


def render_couch_west(s, item, registry=None):
    """1-tile-wide couch placed against an east wall, seats face WEST.

    Visually this is a stack of west-facing armchair tiles; height is taken
    from item['h'] (defaults to 3).
    """
    x, y = item["x"], item["y"]
    h = max(1, int(item.get("h", 3)))
    for dy in range(h):
        set_tile(s["furniture2"], s, x, y + dy, T_CHAIR_W)


def render_couch_east(s, item, registry=None):
    """Mirror of couch_west: against a west wall, seats face EAST."""
    x, y = item["x"], item["y"]
    h = max(1, int(item.get("h", 3)))
    for dy in range(h):
        set_tile(s["furniture2"], s, x, y + dy, T_CHAIR_E)


def render_couch_south(s, item, registry=None):
    """Against a north wall, seats face SOUTH."""
    x, y = item["x"], item["y"]
    w = max(1, int(item.get("w", 3)))
    for dx in range(w):
        set_tile(s["furniture2"], s, x + dx, y, T_CHAIR_S)


def render_couch_north(s, item, registry=None):
    """Against a south wall, seats face NORTH."""
    x, y = item["x"], item["y"]
    w = max(1, int(item.get("w", 3)))
    for dx in range(w):
        set_tile(s["furniture2"], s, x + dx, y, T_CHAIR_N)


def render_presentation_screen(s, item, registry=None):
    fill_rect(s["above1"], s, item["x"], item["y"], item.get("w", 2), 1, T_SCREEN)


def render_whiteboard(s, item, registry=None):
    set_tile(s["above1"], s, item["x"], item["y"], T_SCREEN)


def render_potted_plant(s, item, registry=None):
    set_tile(s["furniture3"], s, item["x"], item["y"], T_PLANT)


def render_wc_toilet(s, item, registry=None):
    set_tile(s["furniture2"], s, item["x"], item["y"], T_TOILET)


def render_wc_sink(s, item, registry=None):
    set_tile(s["furniture2"], s, item["x"], item["y"], T_SINK)


RENDERERS = {
    "desk_individual":     render_desk_individual,
    "desk_grouped":        render_desk_grouped,
    "desk_cluster":        render_desk_cluster,
    "round_table":         render_round_table,
    "round_table_outdoor": render_round_table_outdoor,
    "meeting_table":       render_meeting_table,
    "kitchen_counter":     render_kitchen_counter,
    "lounge_set":          render_lounge_set,
    "couch_west":          render_couch_west,
    "couch_east":          render_couch_east,
    "couch_south":         render_couch_south,
    "couch_north":         render_couch_north,
    "presentation_screen": render_presentation_screen,
    "whiteboard":          render_whiteboard,
    "potted_plant":        render_potted_plant,
    "wc_toilet":           render_wc_toilet,
    "wc_sink":             render_wc_sink,
    # silently ignored if Gemini emits these
    "office_chair":        lambda s, i, registry=None: set_tile(s["furniture2"], s, i["x"], i["y"], T_CHAIR_LEFT),
    "monitor_pair":        lambda s, i, registry=None: set_tile(s["furniture3"], s, i["x"], i["y"], T_MONITOR_L),
    "bench":               lambda s, i, registry=None: set_tile(s["furniture2"], s, i["x"], i["y"], T_ROUND),
    "dev_corner":          render_desk_grouped,
}


# =============================================================================
# Main pipeline
# =============================================================================

def build(plan):
    W = plan["map"]["width"]
    H = plan["map"]["height"]
    s = make_state(W, H)
    rooms_by_id = {r["id"]: r for r in plan["rooms"]}

    # 1. grass everywhere first
    fill_rect(s["floor"], s, 0, 0, W, H, T_GRASS)

    # 2. rooms: floors then walls (with door cells skipped)
    for room in plan["rooms"]:
        draw_room_floor(s, room)

    # 3. outdoor areas: just floors, no walls
    for area in plan.get("outdoor_areas", []):
        draw_outdoor_floor(s, area)

    # 4. compute per-room door cells
    door_cells_by_room = {r["id"]: set() for r in plan["rooms"]}
    for room in plan["rooms"]:
        for door in room.get("doors", []):
            door_cells_by_room[room["id"]] |= door_wall_cells(room, door)
            # mirror to the other room too
            other = rooms_by_id.get(door.get("to"))
            if other is not None:
                door_cells_by_room[other["id"]] |= door_wall_cells(other, door)

    for room in plan["rooms"]:
        draw_room_walls(s, room, door_cells_by_room[room["id"]])

    # 4b. enclose outdoor areas with walls (3 free sides). The wall on the
    #     side that meets a room is reopened later by classify_walls().
    for area in plan.get("outdoor_areas", []):
        draw_outdoor_walls(s, area)

    # 5. open passages between connected rooms (handles 1-tile gaps)
    for room in plan["rooms"]:
        for door in room.get("doors", []):
            open_door_passage(s, room, rooms_by_id.get(door.get("to")), door)

    # 6. wall tile picker, then classify (mirror inner partitions, open
    #    room↔terrasse facades). Order matters: pick_wall_tiles needs the
    #    raw "is wall" booleans, classify_walls runs afterwards and rewrites.
    pick_wall_tiles(s)
    classify_walls(s, plan)

    # 7. furniture in each room and outdoor area
    seat_registry = {}  # room_id → list[(x, y)] desk seats
    for room in plan["rooms"]:
        seats = []
        for item in room.get("furniture", []):
            r = RENDERERS.get(item["kind"])
            if r is None:
                print(f"  WARN: no renderer for '{item['kind']}' in room {room['id']}")
                continue
            result = r(s, item)
            if isinstance(result, list):
                seats.extend(result)
        seat_registry[room["id"]] = seats

    for area in plan.get("outdoor_areas", []):
        for item in area.get("furniture", []):
            r = RENDERERS.get(item["kind"])
            if r is None:
                print(f"  WARN: no renderer for '{item['kind']}' in area {area['id']}")
                continue
            r(s, item)

    # 8. spawn point — drop in the corridor
    spawn = next((r for r in plan["rooms"] if r["id"].startswith("gang_")),
                 plan["rooms"][0])
    sx, sy = spawn["rect"]["x"] + spawn["rect"]["w"] // 2, spawn["rect"]["y"] + 2
    set_tile(s["start"], s, sx,     sy, T_START)
    set_tile(s["start"], s, sx + 1, sy, T_START)

    return s, seat_registry, (sx, sy)


def build_objects(plan, seat_registry, spawn):
    """
    Build the floorLayer object zones:
      - desk_<localPart> over the first 5 seats of buro_5
      - start zone
      - screen_mrr / screen_leaderboard for live data displays
    """
    objects = []
    next_id = 1

    def rect(name, x, y, w, h, klass="", properties=None):
        nonlocal next_id
        o = {
            "id": next_id, "name": name, "type": klass,
            "x": x * TILE, "y": y * TILE,
            "width": w * TILE, "height": h * TILE,
            "rotation": 0, "visible": True,
        }
        if properties:
            o["properties"] = properties
        next_id += 1
        return o

    # team seats in the big team room
    TEAM = ["shahryar", "armin", "lina", "noah", "mira"]
    seats = seat_registry.get("buro_5", [])
    for lp, (x, y) in zip(TEAM, seats):
        objects.append(rect(f"desk_{lp}", x, y, 1, 3))

    # spawn
    objects.append(rect("start", spawn[0], spawn[1], 2, 1))

    # display zones — only emit if the host room exists
    rooms_by_id = {r["id"]: r for r in plan["rooms"]}
    if "sozialraum" in rooms_by_id:
        r = rooms_by_id["sozialraum"]["rect"]
        # screen near the top of SOZIALRAUM (one row inside so the boundary
        # with terrasse_top stays an open passage).
        objects.append(rect("screen_mrr",
                            r["x"] + 3, r["y"] + 1, min(8, r["w"] - 6), 1))

    # leaderboard — west wall of WC area / lobby
    if "wc_h" in rooms_by_id:
        r = rooms_by_id["wc_h"]["rect"]
        objects.append(rect("screen_leaderboard",
                            r["x"] - 4, r["y"] + 1, 4, 4))

    return objects


def tilelayer(name, data, lid, W, H):
    return {
        "name": name, "type": "tilelayer", "id": lid,
        "width": W, "height": H, "x": 0, "y": 0,
        "opacity": 1, "visible": True, "data": data,
    }


def compose(plan, s, objects):
    W, H = s["W"], s["H"]
    return {
        "type": "map", "version": "1.10", "tiledversion": "1.10.2",
        "renderorder": "right-down", "orientation": "orthogonal",
        "infinite": False, "compressionlevel": -1,
        "width": W, "height": H, "tilewidth": TILE, "tileheight": TILE,
        "nextlayerid": 100, "nextobjectid": 999,
        "tilesets": TILESETS,
        "properties": [
            {"name": "mapName",        "type": "string", "value": "Chatarmin Office"},
            {"name": "mapDescription", "type": "string", "value": "ChatArmin Dachgeschoss — virtual replica of the new office."},
            {"name": "mapImage",       "type": "string", "value": "chatarmin-office.png"},
            {"name": "script",         "type": "string", "value": "src/wa-bridge.ts"},
            {"name": "mapCopyright",   "type": "string", "value": "© ChatArmin GmbH. Tiles: WorkAdventure (CC-BY-SA 3.0)."},
        ],
        "layers": [
            tilelayer("start",      s["start"],      1, W, H),
            tilelayer("collisions", s["collisions"], 2, W, H),
            {"name": "floor", "type": "group", "id": 10, "visible": True, "opacity": 1, "x": 0, "y": 0,
             "layers": [tilelayer("floor1", s["floor"], 11, W, H)]},
            {"name": "walls", "type": "group", "id": 20, "visible": True, "opacity": 1, "x": 0, "y": 0,
             "layers": [tilelayer("walls1", s["walls"], 21, W, H)]},
            {"name": "furniture", "type": "group", "id": 30, "visible": True, "opacity": 1, "x": 0, "y": 0,
             "layers": [
                tilelayer("furniture1", s["furniture1"], 31, W, H),
                tilelayer("furniture2", s["furniture2"], 32, W, H),
                tilelayer("furniture3", s["furniture3"], 33, W, H),
             ]},
            {"name": "floorLayer", "type": "objectgroup", "id": 40, "visible": True, "opacity": 1, "x": 0, "y": 0,
             "draworder": "topdown", "objects": objects},
            {"name": "above", "type": "group", "id": 50, "visible": True, "opacity": 1, "x": 0, "y": 0,
             "layers": [tilelayer("above1", s["above1"], 51, W, H)]},
        ],
    }


def main(argv):
    plan_path = Path(argv[1]) if len(argv) > 1 else ROOT / "floorplan.json"
    plan = json.loads(plan_path.read_text())

    s, seats, spawn = build(plan)
    objects = build_objects(plan, seats, spawn)
    map_obj = compose(plan, s, objects)

    out = ROOT / "chatarmin-office.tmj"
    out.write_text(json.dumps(map_obj, indent=1))
    print(f"wrote {out} ({s['W']}x{s['H']} tiles, {out.stat().st_size:,} bytes)")
    print(f"  rooms: {len(plan['rooms'])}, outdoor areas: {len(plan.get('outdoor_areas', []))}")
    print(f"  total furniture items: {sum(len(r.get('furniture', [])) for r in plan['rooms']) + sum(len(a.get('furniture', [])) for a in plan.get('outdoor_areas', []))}")
    print(f"  named desks: {sum(1 for o in objects if o['name'].startswith('desk_'))}")
    print(f"  object zones: {len(objects)}")


if __name__ == "__main__":
    main(sys.argv)
