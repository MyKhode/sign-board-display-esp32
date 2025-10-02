#!/usr/bin/env python3
import asyncio
import json
import numpy as np
import cairo
import gi
from aiohttp import web, WSMsgType

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo

# ---------------- Configuration ----------------
WIDTH = 128
HEIGHT = 32
KHMER_FONT_DEFAULT = "Siemreap"
LATIN_FONT_DEFAULT = "Noto Sans"
EMOJI_FONT_DEFAULT = "Noto Color Emoji"
FONT_SIZE_PT_DEFAULT = 22
FG_COLOR_DEFAULT = (255, 255, 255)
BG_COLOR_DEFAULT = (0, 0, 0)

esp32_clients = set()
browser_clients = set()

# ---------------- Utilities ----------------
def rgb_to_rgb565(r, g, b):
    r_565 = (r & 0xF8) << 8
    g_565 = (g & 0xFC) << 3
    b_565 = (b & 0xF8) >> 3
    return r_565 | g_565 | b_565

def parse_color(c, fallback):
    # Accept "#RRGGBB" or [r,g,b]
    if isinstance(c, (list, tuple)) and len(c) == 3:
        try:
            r, g, b = int(c[0]), int(c[1]), int(c[2])
            return (max(0,min(255,r)), max(0,min(255,g)), max(0,min(255,b)))
        except:
            return fallback
    if isinstance(c, str):
        s = c.strip()
        if s.startswith("#"): s = s[1:]
        if len(s) == 6:
            try:
                return (int(s[0:2],16), int(s[2:4],16), int(s[4:6],16))
            except:
                pass
    return fallback

# ---------------- Script Detection ----------------
def detect_script(ch):
    cp = ord(ch)
    if 0x1780 <= cp <= 0x17FF:   # Khmer
        return "khmer"
    if (0x1F300 <= cp <= 0x1FAFF) or (0x2600 <= cp <= 0x26FF):  # Emoji
        return "emoji"
    if (0x0000 <= cp <= 0x024F) or (0x1E00 <= cp <= 0x1EFF):    # Latin
        return "latin"
    return "latin"

def build_attrlist_for_mixed_text(text, khmer_font, latin_font, emoji_font, font_size_pt):
    byte_offsets = [0]
    for ch in text:
        byte_offsets.append(byte_offsets[-1] + len(ch.encode("utf-8")))

    runs, prev_script, run_start = [], None, 0
    for i, ch in enumerate(text):
        s = detect_script(ch)
        if prev_script is None:
            prev_script, run_start = s, i
        elif s != prev_script:
            runs.append((run_start, i, prev_script))
            prev_script, run_start = s, i
    if prev_script is not None:
        runs.append((run_start, len(text), prev_script))

    attrs = Pango.AttrList()
    for start_ch, end_ch, script in runs:
        start_byte = byte_offsets[start_ch]
        end_byte = byte_offsets[end_ch]
        pd = Pango.FontDescription()
        pd.set_absolute_size(font_size_pt * Pango.SCALE)
        if script == "khmer":
            pd.set_family(khmer_font)
        elif script == "emoji":
            pd.set_family(emoji_font)
        else:
            pd.set_family(latin_font)
        attr = Pango.attr_font_desc_new(pd)
        attr.start_index = start_byte
        attr.end_index = end_byte
        attrs.insert(attr)
    return attrs

# ---------------- Render ----------------
def render_text(
    text,
    width=WIDTH,
    height=HEIGHT,
    khmer_font=KHMER_FONT_DEFAULT,
    latin_font=LATIN_FONT_DEFAULT,
    emoji_font=EMOJI_FONT_DEFAULT,
    font_size_pt=FONT_SIZE_PT_DEFAULT,
    fg_color=FG_COLOR_DEFAULT,
    bg_color=BG_COLOR_DEFAULT
):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)

    # Background
    cr.set_source_rgb(bg_color[0]/255.0, bg_color[1]/255.0, bg_color[2]/255.0)
    cr.paint()

    # Foreground (glyph color)
    cr.set_source_rgb(fg_color[0]/255.0, fg_color[1]/255.0, fg_color[2]/255.0)

    layout = PangoCairo.create_layout(cr)
    base_desc = Pango.FontDescription()
    base_desc.set_family(latin_font)
    base_desc.set_absolute_size(font_size_pt * Pango.SCALE)
    layout.set_font_description(base_desc)
    layout.set_text(text, -1)

    attrs = build_attrlist_for_mixed_text(text, khmer_font, latin_font, emoji_font, font_size_pt)
    layout.set_attributes(attrs)
    layout.set_width(width * Pango.SCALE)
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)

    PangoCairo.update_layout(cr, layout)
    text_w, text_h = layout.get_pixel_size()
    x = (width - text_w) // 2
    y = (height - text_h) // 2
    cr.move_to(max(0, x), max(0, y))
    PangoCairo.show_layout(cr, layout)

    # Extract pixels → RGB565
    buf = surface.get_data()
    stride = surface.get_stride()
    rgb565_matrix = np.zeros((height, width), dtype=np.uint16)
    for yy in range(height):
        row = yy * stride
        for xx in range(width):
            i = row + xx * 4
            b, g, r, a = buf[i], buf[i+1], buf[i+2], buf[i+3]
            if a > 0:
                r = (r * a) // 255
                g = (g * a) // 255
                b = (b * a) // 255
            rgb565_matrix[yy, xx] = rgb_to_rgb565(r, g, b)
    return rgb565_matrix

# ---------------- Status / Broadcast ----------------
async def broadcast_status(message: str, level="info"):
    payload = json.dumps({"type":"status", "level":level, "message":message})
    stale = []
    for ws in list(browser_clients):
        try:
            await ws.send_str(payload)
        except:
            stale.append(ws)
    for s in stale:
        browser_clients.discard(s)

async def forward_frame_to_esp(frame_bytes: bytes):
    if not esp32_clients:
        await broadcast_status("No ESP32 clients connected; frame not sent", level="warn")
        return
    stale = []
    for client in list(esp32_clients):
        try:
            await client.send_bytes(frame_bytes)
        except:
            stale.append(client)
    for s in stale:
        esp32_clients.discard(s)

# ---------------- Web Handlers ----------------
async def index(request):
    return web.FileResponse("templates/index.html")

async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # IMPORTANT: behave like your old server
    # Default any new connection to the ESP32 sink set.
    esp32_clients.add(ws)
    await broadcast_status("Client connected")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # Try to parse JSON
                try:
                    payload = json.loads(msg.data)
                except:
                    await broadcast_status(f"Text message: {msg.data}")
                    continue

                # Role handshake
                if payload.get("type") == "hello":
                    role = payload.get("role")
                    if role == "esp32":
                        esp32_clients.add(ws)
                        browser_clients.discard(ws)
                        await broadcast_status("ESP32 registered")
                        continue
                    if role == "browser":
                        browser_clients.add(ws)
                        esp32_clients.discard(ws)
                        await broadcast_status("Browser registered")
                        continue

                # Render commands (support both names)
                if payload.get("type") == "command" and payload.get("action") in ("render_and_send", "render_and_send_text"):
                    text = payload.get("text", "")
                    width = int(payload.get("width", WIDTH))
                    height = int(payload.get("height", HEIGHT))
                    font_family = payload.get("font_family", LATIN_FONT_DEFAULT)
                    font_size_pt = int(payload.get("font_size_pt", FONT_SIZE_PT_DEFAULT))

                    fg = parse_color(payload.get("fg_color", "#FFFFFF"), FG_COLOR_DEFAULT)
                    bg = parse_color(payload.get("bg_color", "#000000"), BG_COLOR_DEFAULT)

                    # Use chosen font as latin base; keep Khmer/Emoji defaults unless user explicitly selects them
                    latin_font = font_family
                    khmer_font = KHMER_FONT_DEFAULT
                    emoji_font = EMOJI_FONT_DEFAULT
                    if font_family == KHMER_FONT_DEFAULT:
                        khmer_font = KHMER_FONT_DEFAULT
                    if font_family == EMOJI_FONT_DEFAULT:
                        emoji_font = EMOJI_FONT_DEFAULT

                    await broadcast_status("Rendering text…")
                    try:
                        rgb565 = render_text(
                            text,
                            width=width, height=height,
                            khmer_font=khmer_font,
                            latin_font=latin_font,
                            emoji_font=emoji_font,
                            font_size_pt=font_size_pt,
                            fg_color=fg, bg_color=bg
                        )
                        await broadcast_status("Sending frame to ESP32…")
                        await forward_frame_to_esp(rgb565.tobytes())
                        await broadcast_status("Frame sent")
                    except Exception as e:
                        await broadcast_status(f"Render/send error: {e}", level="error")
                    continue

                # Unknown command
                await broadcast_status(f"Unknown command: {payload}", level="warn")

            elif msg.type == WSMsgType.BINARY:
                # Browser pre-encoded frame passthrough (image/video)
                frame_bytes = msg.data
                await broadcast_status(f"Binary frame received: {len(frame_bytes)} bytes")
                expected = WIDTH * HEIGHT * 2
                if len(frame_bytes) != expected:
                    await broadcast_status(f"Warning: expected {expected} bytes for {WIDTH}x{HEIGHT}, got {len(frame_bytes)}", level="warn")
                await forward_frame_to_esp(frame_bytes)

            elif msg.type == WSMsgType.ERROR:
                await broadcast_status(f"WS error: {ws.exception()}", level="error")

    finally:
        browser_clients.discard(ws)
        esp32_clients.discard(ws)
        await broadcast_status("Client disconnected")

    return ws

# ---------------- Run ----------------
app = web.Application()
app.router.add_get("/", index)
app.router.add_get("/ws", websocket_handler)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=9122)
