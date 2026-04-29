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
T_CHAIR_LEFT  = 1494   # left half of a chair pair (faces right)
T_CHAIR_RIGHT = 1495   # right half (faces left)
T_MONITOR_L   = 134
T_MONITOR_R   = 133
T_ROUND       = 1525   # 1×1 round table with chairs
T_PLANT       = 90
T_SCREEN      = 187    # whiteboard / screen
T_TOILET      = 245    # WA_Other_Furniture toilet (approx)
T_SINK        = 240    # WA_Other_Furniture sink (approx)
T_COUNTER     = 1567   # generic table; reused as kitchen counter

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
    set_tile(s["furniture2"], s, x, y, T_DESK_TOP)
    set_tile(s["furniture3"], s, x, y, T_MONITOR_L)
    set_tile(s["furniture2"], s, x, y + 1, T_DESK_BOT)
    set_tile(s["furniture2"], s, x, y + 2, T_CHAIR_LEFT)
    return [(x, y)]


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
        # n side-by-side desks; 2 chairs per desk (above & below) — but only
        # half count: top row = ceil(n/2), bottom row = floor(n/2). For
        # simplicity, model as n single-sided desks all facing the same way.
        for i in range(n):
            cx = x + i
            set_tile(s["furniture2"], s, cx, y,     T_DESK_TOP)
            set_tile(s["furniture3"], s, cx, y,     T_MONITOR_L if i % 2 == 0 else T_MONITOR_R)
            set_tile(s["furniture2"], s, cx, y + 1, T_DESK_BOT)
            set_tile(s["furniture2"], s, cx, y + 2, T_CHAIR_LEFT if i % 2 == 0 else T_CHAIR_RIGHT)
            seats.append((cx, y))
    else:  # vertical
        for i in range(n):
            cy = y + i * 2
            set_tile(s["furniture2"], s, x,     cy,     T_DESK_TOP)
            set_tile(s["furniture3"], s, x,     cy,     T_MONITOR_L)
            set_tile(s["furniture2"], s, x,     cy + 1, T_DESK_BOT)
            set_tile(s["furniture2"], s, x + 1, cy,     T_CHAIR_LEFT)
            seats.append((x, cy))
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
    "round_table":         render_round_table,
    "round_table_outdoor": render_round_table_outdoor,
    "meeting_table":       render_meeting_table,
    "kitchen_counter":     render_kitchen_counter,
    "lounge_set":          render_lounge_set,
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

    # 5. open passages between connected rooms (handles 1-tile gaps)
    for room in plan["rooms"]:
        for door in room.get("doors", []):
            open_door_passage(s, room, rooms_by_id.get(door.get("to")), door)

    # 6. wall tile picker
    pick_wall_tiles(s)

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
      - jitsi room over a sensible meeting area (sozialraum if present)
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
        # screen at the top of SOZIALRAUM
        objects.append(rect("screen_mrr",
                            r["x"] + 3, r["y"], min(8, r["w"] - 6), 1))

    # leaderboard — west wall of WC area / lobby
    if "wc_h" in rooms_by_id:
        r = rooms_by_id["wc_h"]["rect"]
        objects.append(rect("screen_leaderboard",
                            r["x"] - 4, r["y"] + 1, 4, 4))

    # jitsi over büro_1 (Gemini's first office)
    if "buro_1" in rooms_by_id:
        r = rooms_by_id["buro_1"]["rect"]
        objects.append(rect("jitsi_meetingroom",
                            r["x"] + 1, r["y"] + 1, r["w"] - 2, r["h"] - 2,
                            klass="area",
                            properties=[{"name": "jitsiRoom", "type": "string", "value": "chatarmin-buro1"}]))

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
            {"name": "mapImage",       "type": "string", "value": "office.png"},
            {"name": "script",         "type": "string", "value": "https://live-armin.vercel.app/scripting/wa-bridge.js"},
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
