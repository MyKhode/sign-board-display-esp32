#!/usr/bin/env python3
import asyncio, json, numpy as np, cairo, gi
from aiohttp import web, WSMsgType
gi.require_version("Pango", "1.0"); gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo

# ======== Config ========
HEIGHT = 32
KHMER_FONT_DEFAULT = "Siemreap"
LATIN_FONT_DEFAULT = "Noto Sans"
EMOJI_FONT_DEFAULT = "Noto Color Emoji"
FONT_SIZE_PT_DEFAULT = 22
FG_COLOR_DEFAULT = (255, 255, 255)
BG_COLOR_DEFAULT = (0, 0, 0)

# Safer segments for long lines
MAX_SEGMENT_WIDTH = 192
SEGMENT_DELAY_S   = 0.010

esp32_clients = set()
browser_clients = set()

# ======== Utils ========
def parse_color(c, fb):
    if isinstance(c, (list, tuple)) and len(c) == 3:
        try: r,g,b = [max(0,min(255,int(v))) for v in c]; return (r,g,b)
        except: return fb
    if isinstance(c, str):
        s = c.strip()[1:] if c.strip().startswith("#") else c.strip()
        if len(s)==6:
            try: return (int(s[0:2],16), int(s[2:4],16), int(s[4:6],16))
            except: pass
    return fb

def detect_script(ch):
    cp = ord(ch)
    if 0x1780 <= cp <= 0x17FF: return "khmer"
    if (0x1F300 <= cp <= 0x1FAFF) or (0x2600 <= cp <= 0x26FF): return "emoji"
    if (0x0000 <= cp <= 0x024F) or (0x1E00 <= cp <= 0x1EFF): return "latin"
    return "latin"

def build_attrlist(text, khmer_font, latin_font, emoji_font, font_size_pt):
    byte_offsets = [0]
    for ch in text: byte_offsets.append(byte_offsets[-1] + len(ch.encode("utf-8")))
    runs, prev, start = [], None, 0
    for i, ch in enumerate(text):
        s = detect_script(ch)
        if prev is None: prev, start = s, i
        elif s != prev: runs.append((start, i, prev)); prev, start = s, i
    if prev is not None: runs.append((start, len(text), prev))
    attrs = Pango.AttrList()
    for a,b,script in runs:
        pd = Pango.FontDescription(); pd.set_absolute_size(font_size_pt * Pango.SCALE)
        pd.set_family(khmer_font if script=="khmer" else emoji_font if script=="emoji" else latin_font)
        attr = Pango.attr_font_desc_new(pd); attr.start_index = byte_offsets[a]; attr.end_index = byte_offsets[b]
        attrs.insert(attr)
    return attrs

def render_line_surface(text, height, khmer_font, latin_font, emoji_font, font_pt, fg, bg, y_offset=0):
    dummy = cairo.ImageSurface(cairo.FORMAT_ARGB32, 16, 16)
    dcr = cairo.Context(dummy)
    layout = PangoCairo.create_layout(dcr)
    base = Pango.FontDescription(); base.set_family(latin_font); base.set_absolute_size(font_pt * Pango.SCALE)
    layout.set_font_description(base); layout.set_text(text, -1)
    layout.set_attributes(build_attrlist(text, khmer_font, latin_font, emoji_font, font_pt))
    layout.set_width(-1); layout.set_single_paragraph_mode(True)
    PangoCairo.update_layout(dcr, layout)
    text_w, text_h = layout.get_pixel_size()
    if text_w <= 0: text_w = 1
    if height <= 0: height = HEIGHT

    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, text_w, height)
    cr = cairo.Context(surf)
    cr.set_source_rgb(*[c/255 for c in bg]); cr.paint()
    cr.set_source_rgb(*[c/255 for c in fg])
    layout2 = PangoCairo.create_layout(cr); layout2.set_font_description(base)
    layout2.set_text(text, -1)
    layout2.set_attributes(build_attrlist(text, khmer_font, latin_font, emoji_font, font_pt))
    layout2.set_width(-1); layout2.set_single_paragraph_mode(True)
    PangoCairo.update_layout(cr, layout2)
    y = max(0, (height - text_h)//2 + int(y_offset))
    cr.move_to(0, y); PangoCairo.show_layout(cr, layout2)
    return surf

def surface_to_rgb565(surf: cairo.ImageSurface) -> np.ndarray:
    h, w = surf.get_height(), surf.get_width()
    buf = surf.get_data(); stride = surf.get_stride()
    out = np.zeros((h, w), dtype=np.uint16)
    for yy in range(h):
        row = yy * stride
        for xx in range(w):
            i = row + xx*4
            b,g,r,a = buf[i], buf[i+1], buf[i+2], buf[i+3]
            if a: r = (r*a)//255; g = (g*a)//255; b = (b*a)//255
            out[yy,xx] = ((r & 0xF8)<<8) | ((g & 0xFC)<<3) | (b >> 3)
    return out

async def broadcast_status(message, level="info"):
    payload = json.dumps({"type":"status","level":level,"message":message})
    dead=[]
    for ws in list(browser_clients):
        try: await ws.send_str(payload)
        except: dead.append(ws)
    for d in dead: browser_clients.discard(d)

async def send_config(animate:str, bg_noise:bool):
    payload = json.dumps({"type":"config","animate":animate,"bg_noise":bool(bg_noise)})
    dead=[]
    for ws in list(esp32_clients):
        try: await ws.send_str(payload)
        except: dead.append(ws)
    for d in dead: esp32_clients.discard(d)

async def send_segment(total_w:int, seg_x:int, seg:np.ndarray):
    h, seg_w = seg.shape
    header = bytearray(10)
    header[0] = ord('S'); header[1] = ord('G')
    header[2] = total_w & 0xFF; header[3] = (total_w >> 8) & 0xFF
    header[4] = seg_x & 0xFF;   header[5] = (seg_x >> 8) & 0xFF
    header[6] = seg_w & 0xFF;   header[7] = (seg_w >> 8) & 0xFF
    header[8] = h & 0xFF;       header[9] = (h >> 8) & 0xFF
    pkt = bytes(header) + seg.tobytes(order='C')
    dead=[]
    for ws in list(esp32_clients):
        try: await ws.send_bytes(pkt)
        except: dead.append(ws)
    for d in dead: esp32_clients.discard(d)

async def send_text_segmented(text, height, font_family, font_pt, fg, bg, y_offset, animate, bg_noise):
    # Send config first so ESP switches mode immediately
    await send_config(animate, bg_noise)

    surf = render_line_surface(
        text, height,
        KHMER_FONT_DEFAULT, font_family, EMOJI_FONT_DEFAULT,
        font_pt, fg, bg, y_offset
    )
    rgb = surface_to_rgb565(surf)  # (h, total_w)
    h, total_w = rgb.shape

    # Send tiles left→right (1..N)
    x = 0; sent = 0
    while x < total_w:
        seg_w = min(MAX_SEGMENT_WIDTH, total_w - x)
        seg = rgb[:, x:x+seg_w]
        await send_segment(total_w, x, seg)
        sent += 1
        await broadcast_status(f"Sent segment {sent} (x={x}, w={seg_w}) / total {total_w}")
        x += seg_w
        # tiny pacing after first two
        if sent >= 2 and SEGMENT_DELAY_S > 0 and x < total_w:
            await asyncio.sleep(SEGMENT_DELAY_S)

# ======== HTTP / WS ========
async def index(request): return web.FileResponse("templates/index.html")

async def websocket_handler(request):
    ws = web.WebSocketResponse(); await ws.prepare(request)
    esp32_clients.add(ws); await broadcast_status("Client connected")
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try: payload = json.loads(msg.data)
                except: await broadcast_status(f"Text: {msg.data}"); continue

                if payload.get("type") == "hello":
                    role = payload.get("role")
                    if role == "browser": browser_clients.add(ws); esp32_clients.discard(ws); await broadcast_status("Browser registered"); continue
                    if role == "esp32":   esp32_clients.add(ws); browser_clients.discard(ws); await broadcast_status("ESP32 registered"); continue

                if payload.get("type") == "command" and payload.get("action") in ("render_and_send","render_and_send_text"):
                    text         = payload.get("text", "")
                    height       = int(payload.get("height", HEIGHT))
                    font_family  = payload.get("font_family", LATIN_FONT_DEFAULT)
                    font_size_pt = int(payload.get("font_size_pt", FONT_SIZE_PT_DEFAULT))
                    y_offset     = int(payload.get("y_offset", 0))
                    fg = parse_color(payload.get("fg_color", "#FFFFFF"), FG_COLOR_DEFAULT)
                    bg = parse_color(payload.get("bg_color", "#000000"), BG_COLOR_DEFAULT)
                    animate      = payload.get("animate", "scroll")   # "scroll" or "static"
                    bg_noise     = bool(payload.get("bg_noise", False))
                    try:
                        await broadcast_status("Rendering text…")
                        await send_text_segmented(text, height, font_family, font_size_pt, fg, bg, y_offset, animate, bg_noise)
                        await broadcast_status("All segments sent")
                    except Exception as e:
                        await broadcast_status(f"Render/send error: {e}", level="error")
                    continue

                await broadcast_status(f"Unknown command: {payload}", level="warn")

            elif msg.type == WSMsgType.BINARY:
                # Legacy passthrough (W,H + pixels). Also respect current browser defaults if present:
                data = msg.data
                if len(data) >= 4:
                    w = data[0] | (data[1]<<8); h = data[2] | (data[3]<<8)
                    px = data[4:4+w*h*2]
                    arr = np.frombuffer(px, dtype=np.uint16).reshape((h, w))
                    # No extra config here; you can call send_config() before sending raw frames if needed
                    x = 0; sent = 0
                    while x < w:
                        seg_w = min(MAX_SEGMENT_WIDTH, w - x)
                        await send_segment(w, x, arr[:, x:x+seg_w])
                        x += seg_w; sent += 1
                        if sent >= 2 and SEGMENT_DELAY_S > 0 and x < w:
                            await asyncio.sleep(SEGMENT_DELAY_S)
                else:
                    await broadcast_status("Binary too small", level="error")

            elif msg.type == WSMsgType.ERROR:
                await broadcast_status(f"WS error: {ws.exception()}", level="error")
    finally:
        browser_clients.discard(ws); esp32_clients.discard(ws)
        await broadcast_status("Client disconnected")
    return ws

app = web.Application()
app.router.add_get("/", index)
app.router.add_get("/ws", websocket_handler)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=9122)
