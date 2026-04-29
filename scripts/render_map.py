#!/usr/bin/env python3
"""
Render a .tmj map to a flattened PNG so we can preview it without booting WA.

  python3 scripts/render_map.py [chatarmin-office.tmj] [out.png]

Honors all tile layers and tile-layer groups, in document order. Object
layers (zones) and `start`/`collisions` special-zone layers are skipped
visually because their tiles are pure markers — but we DO draw a coloured
outline + label for every object zone, plus the room labels from
floorplan.json, so it's obvious at a glance what's what.
"""

import json
import sys
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent

# Layers we don't want rendered visually (they're invisible markers in WA).
SKIP_LAYERS = {"start", "collisions"}

# colour palette per object-zone family (RGBA)
ZONE_COLOURS = {
    "desk":     (240,  80, 130, 255),  # pink
    "start":    (255, 215,   0, 255),  # gold
    "screen":   ( 80, 200, 255, 255),  # cyan
    "jitsi":    (130, 220, 130, 255),  # green
    "default":  (200, 200, 200, 255),
}
ROOM_LABEL_RGBA = (255, 255, 255, 230)
ROOM_LABEL_BG   = (  0,   0,   0, 160)

FONT_CANDIDATES = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def load_font(size):
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    return ImageFont.load_default()


def zone_colour(name):
    head = name.split("_", 1)[0]
    return ZONE_COLOURS.get(head, ZONE_COLOURS["default"])


def load_tilesets(map_obj, root: Path):
    """Return list of (firstgid, image, columns, tilewidth, tileheight)."""
    out = []
    for ts in map_obj["tilesets"]:
        img_path = root / ts["image"]
        img = Image.open(img_path).convert("RGBA")
        out.append({
            "firstgid": ts["firstgid"],
            "image": img,
            "columns": ts["columns"],
            "tw": ts["tilewidth"],
            "th": ts["tileheight"],
            "tilecount": ts["tilecount"],
            "name": ts["name"],
        })
    return out


def find_tileset(gid, tilesets):
    chosen = None
    for ts in tilesets:
        if ts["firstgid"] <= gid:
            chosen = ts
        else:
            break
    if not chosen:
        return None, 0
    local = gid - chosen["firstgid"]
    if local < 0 or local >= chosen["tilecount"]:
        return None, 0
    return chosen, local


def tile_image(ts, local_id):
    col = local_id % ts["columns"]
    row = local_id // ts["columns"]
    box = (col * ts["tw"], row * ts["th"], (col + 1) * ts["tw"], (row + 1) * ts["th"])
    return ts["image"].crop(box)


FLIP_H = 0x80000000
FLIP_V = 0x40000000
FLIP_D = 0x20000000


def paste_layer(canvas, layer, W, H, TW, TH, tilesets):
    data = layer.get("data") or []
    for i, gid in enumerate(data):
        if gid == 0:
            continue
        flip_h = bool(gid & FLIP_H)
        flip_v = bool(gid & FLIP_V)
        flip_d = bool(gid & FLIP_D)
        gid_clean = gid & 0x1FFFFFFF
        ts, local = find_tileset(gid_clean, tilesets)
        if ts is None:
            continue
        tile = tile_image(ts, local)
        if flip_d:
            tile = tile.transpose(Image.TRANSPOSE)
        if flip_h:
            tile = tile.transpose(Image.FLIP_LEFT_RIGHT)
        if flip_v:
            tile = tile.transpose(Image.FLIP_TOP_BOTTOM)
        x = (i % W) * TW
        y = (i // W) * TH
        canvas.alpha_composite(tile, dest=(x, y))


def collect_objects(layers):
    """Flatten every objectgroup we find in the map."""
    out = []
    for l in layers:
        if l.get("type") == "objectgroup":
            out.extend(l.get("objects", []))
        elif l.get("type") == "group":
            out.extend(collect_objects(l.get("layers", [])))
    return out


def draw_room_labels(canvas, plan, TW, TH):
    """Outline every room/outdoor-area rect and centre its label inside it."""
    if plan is None:
        return
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font_room = load_font(28)

    def label_rect(rect, text, outline=(255, 255, 255, 200), fill=(0, 0, 0, 90)):
        x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
        x0, y0 = x * TW, y * TH
        x1, y1 = (x + w) * TW, (y + h) * TH
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), outline=outline, width=2)
        # measure
        bbox = draw.textbbox((0, 0), text, font=font_room)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        cx = x0 + (x1 - x0 - tw) // 2
        cy = y0 + (y1 - y0 - th) // 2
        pad = 8
        draw.rectangle(
            (cx - pad, cy - pad // 2, cx + tw + pad, cy + th + pad // 2),
            fill=ROOM_LABEL_BG,
        )
        draw.text((cx, cy), text, font=font_room, fill=ROOM_LABEL_RGBA)

    for room in plan.get("rooms", []):
        label_rect(room["rect"], room.get("label") or room["id"])
    for area in plan.get("outdoor_areas", []):
        label_rect(area["rect"], area.get("label") or area["id"],
                   outline=(120, 220, 255, 200), fill=(0, 0, 0, 80))

    canvas.alpha_composite(overlay)


def draw_object_zones(canvas, objects, TW, TH):
    """Outline each object zone and stamp its name above it."""
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font_obj = load_font(18)

    for o in objects:
        x0, y0 = int(o["x"]), int(o["y"])
        x1, y1 = x0 + int(o["width"]), y0 + int(o["height"])
        col = zone_colour(o["name"])
        # filled translucent + bold outline
        fill = (col[0], col[1], col[2], 70)
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=fill, outline=col, width=3)
        # label pinned just above the rect (or inside if it would clip top)
        text = o["name"]
        bbox = draw.textbbox((0, 0), text, font=font_obj)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pad = 4
        ly = y0 - th - pad * 2
        if ly < 0:
            ly = y0 + pad
        lx = x0
        draw.rectangle(
            (lx - pad, ly - pad, lx + tw + pad, ly + th + pad),
            fill=(0, 0, 0, 200),
        )
        draw.text((lx, ly), text, font=font_obj, fill=col)

    canvas.alpha_composite(overlay)


def render(map_path: Path, out_path: Path, clean_path: Optional[Path] = None):
    """Render the .tmj.

    Always produces ``out_path`` with the debug overlays (room labels, object
    zones) drawn on top — that's the file we use ourselves for sanity checks.

    If ``clean_path`` is given, an overlay-free version is also written to that
    path. The clean image is what WorkAdventure uses as the room thumbnail
    (referenced from the .tmj's ``mapImage`` property), so it must be free of
    any debug drawing.
    """
    m = json.loads(map_path.read_text())
    W, H = m["width"], m["height"]
    TW, TH = m["tilewidth"], m["tileheight"]
    tilesets = load_tilesets(m, ROOT)
    canvas = Image.new("RGBA", (W * TW, H * TH), (40, 40, 50, 255))

    def walk(layers):
        for l in layers:
            if l.get("type") == "tilelayer":
                if l["name"] in SKIP_LAYERS or not l.get("visible", True):
                    continue
                paste_layer(canvas, l, W, H, TW, TH, tilesets)
            elif l.get("type") == "group" and l.get("visible", True):
                walk(l["layers"])

    walk(m["layers"])

    # Snapshot the clean tile composite BEFORE we paint any overlays — that's
    # the version WA will show as the room thumbnail.
    if clean_path is not None:
        canvas.convert("RGB").save(clean_path, "PNG", optimize=True)
        print(f"rendered {clean_path} ({W*TW}x{H*TH}, {clean_path.stat().st_size:,} bytes)")

    # Now paint the debug overlays on top: room outlines/labels (from
    # floorplan.json if available) and every object zone in the .tmj.
    plan_path = ROOT / "floorplan.json"
    plan = json.loads(plan_path.read_text()) if plan_path.exists() else None
    draw_room_labels(canvas, plan, TW, TH)
    draw_object_zones(canvas, collect_objects(m["layers"]), TW, TH)

    canvas.convert("RGB").save(out_path, "PNG", optimize=True)
    print(f"rendered {out_path} ({W*TW}x{H*TH}, {out_path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "chatarmin-office.tmj"
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "chatarmin-office.preview.png"
    # Conventional companion path: chatarmin-office.preview.png ->
    # chatarmin-office.png. Caller can opt out by passing a 3rd arg of "-".
    if len(sys.argv) > 3:
        clean = None if sys.argv[3] == "-" else Path(sys.argv[3])
    else:
        clean = ROOT / "chatarmin-office.png"
    render(src, dst, clean)
