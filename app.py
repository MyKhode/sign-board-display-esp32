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

# Segments for long lines
MAX_SEGMENT_WIDTH = 192
SEGMENT_DELAY_S   = 0.010

esp32_clients = set()
browser_clients = set()

# ======== Utils ========
def parse_color(c, fb):
    if isinstance(c, (list, tuple)) and len(c) == 3:
        try:
            r,g,b = [max(0,min(255,int(v))) for v in c]
            return (r,g,b)
        except:
            return fb
    if isinstance(c, str):
        s = c.strip()[1:] if c.strip().startswith("#") else c.strip()
        if len(s)==6:
            try:
                return (int(s[0:2],16), int(s[2:4],16), int(s[4:6],16))
            except:
                pass
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

def font_available(name: str) -> bool:
    try:
        fm = PangoCairo.FontMap.get_default()
        return any(f.get_name() == name for f in fm.list_families())
    except Exception:
        return False

def render_line_surface(text, height, khmer_font, latin_font, emoji_font,
                        font_pt, fg, bg, y_offset=0, fg2=None,
                        use_gradient=False, gradient_dir="horizontal"):
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
    y = max(0, (height - text_h)//2 + int(y_offset))

    # Gradient (horizontal or vertical)
    if use_gradient and fg2:
        if gradient_dir == "vertical":
            grad = cairo.LinearGradient(0, 0, 0, height)
        else:
            grad = cairo.LinearGradient(0, 0, text_w, 0)
        grad.add_color_stop_rgb(0, fg[0]/255, fg[1]/255, fg[2]/255)
        grad.add_color_stop_rgb(1, fg2[0]/255, fg2[1]/255, fg2[2]/255)
        cr.set_source(grad)
    else:
        cr.set_source_rgb(*[c/255 for c in fg])

    layout2 = PangoCairo.create_layout(cr)
    layout2.set_font_description(base)
    layout2.set_text(text, -1)
    layout2.set_attributes(build_attrlist(text, khmer_font, latin_font, emoji_font, font_pt))
    layout2.set_width(-1); layout2.set_single_paragraph_mode(True)
    cr.move_to(0, y)
    PangoCairo.show_layout(cr, layout2)
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
async def send_text_segmented(text, height, font_family, font_pt, fg, bg, y_offset,
                              animate, bg_noise, fg2=None, use_gradient=False,
                              gradient_dir="horizontal", khmer_font_override=None):
    await send_config(animate, bg_noise)
    khmer_font = khmer_font_override or KHMER_FONT_DEFAULT
    # NOTE: render_line_surface takes (khmer_font, latin_font, emoji_font, ...)
    surf = render_line_surface(
        text, height,
        khmer_font, font_family, EMOJI_FONT_DEFAULT,
        font_pt, fg, bg, y_offset, fg2, use_gradient, gradient_dir
    )
    rgb = surface_to_rgb565(surf)
    h, total_w = rgb.shape
    x = 0; sent = 0
    while x < total_w:
        seg_w = min(MAX_SEGMENT_WIDTH, total_w - x)
        await send_segment(total_w, x, rgb[:, x:x+seg_w])
        sent += 1
        await broadcast_status(f"Sent segment {sent} / total width {total_w}")
        x += seg_w
        if sent >= 2 and SEGMENT_DELAY_S > 0 and x < total_w:
            await asyncio.sleep(SEGMENT_DELAY_S)

# ======== HTTP / WS ========
async def index(request):
    return web.FileResponse("templates/index.html")

# --- WebSocket handler ---
# --- WebSocket handler ---
async def websocket_handler(request):
    # Accept both browser and ESP32 connections
    ws = web.WebSocketResponse(
        autoping=True,
        heartbeat=20.0,
        protocols=("arduino", "browser", "esp32")
    )
    await ws.prepare(request)
    peer = request.remote
    await broadcast_status(f"Client {peer} connected")

    # Default: assume ESP32 client
    esp32_clients.add(ws)

    try:
        async for msg in ws:
            # --- Handle text messages ---
            if msg.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except Exception:
                    await broadcast_status(f"Bad JSON: {msg.data}", level="error")
                    continue

                msg_type = payload.get("type")
                # --- Handshake / registration ---
                if msg_type == "hello":
                    role = payload.get("role", "unknown")
                    if role == "browser":
                        browser_clients.add(ws)
                        esp32_clients.discard(ws)
                        await broadcast_status("Browser registered")
                    elif role == "esp32":
                        esp32_clients.add(ws)
                        browser_clients.discard(ws)
                        await broadcast_status("ESP32 registered")
                    else:
                        await broadcast_status(f"Unknown hello role: {role}")
                    continue

                # --- Command from browser ---
                if msg_type == "command" and payload.get("action") in ("render_and_send", "render_and_send_text"):
                    # inside: if msg_type == "command" ...
                    text         = payload.get("text", "")
                    height       = int(payload.get("height", HEIGHT))
                    selected     = payload.get("font_family", LATIN_FONT_DEFAULT)
                    font_size_pt = int(payload.get("font_size_pt", FONT_SIZE_PT_DEFAULT))
                    y_offset     = int(payload.get("y_offset", 0))
                    fg  = parse_color(payload.get("fg_color", "#FFFFFF"), FG_COLOR_DEFAULT)
                    fg2 = parse_color(payload.get("fg_color2", "#FF00FF"), FG_COLOR_DEFAULT)
                    bg  = parse_color(payload.get("bg_color", "#000000"), BG_COLOR_DEFAULT)
                    animate      = payload.get("animate", "scroll")
                    bg_noise     = bool(payload.get("bg_noise", False))
                    use_gradient = bool(payload.get("use_gradient", False))
                    gradient_dir = payload.get("gradient_dir", "horizontal")

                    # Khmer font set from your dropdown
                    KHMER_FONTS = {"Bayon", "Bokor", "Koulen", "Moul", "Siemreap", "Khmer OS", "Khmer OS System"}

                    # Decide which families to ask Pango for
                    if selected in KHMER_FONTS:
                        khmer_font = selected if font_available(selected) else KHMER_FONT_DEFAULT
                        latin_font = selected if font_available(selected) else LATIN_FONT_DEFAULT
                    else:
                        khmer_font = KHMER_FONT_DEFAULT
                        latin_font = selected if font_available(selected) else LATIN_FONT_DEFAULT

                    # Helpful logs if fonts aren’t installed
                    if selected not in KHMER_FONTS and not font_available(selected):
                        await broadcast_status(f"⚠️ Font '{selected}' not found. Using '{latin_font}'.", level="warn")
                    if selected in KHMER_FONTS and not font_available(selected):
                        await broadcast_status(f"⚠️ Khmer font '{selected}' not found. Using '{khmer_font}'.", level="warn")

                    try:
                        await broadcast_status(f"Rendering text with Khmer='{khmer_font}', Latin='{latin_font}' …")
                        await send_text_segmented(
                            text, height,
                            latin_font, font_size_pt, fg, bg, y_offset,
                            animate, bg_noise, fg2, use_gradient, gradient_dir,
                            khmer_font_override=khmer_font
                        )
                        await broadcast_status("All segments sent ✅")
                    except Exception as e:
                        await broadcast_status(f"Render/send error: {e}", level="error")


            # --- Handle binary messages (image data from browser) ---
            elif msg.type == WSMsgType.BINARY:
                data = msg.data
                if len(data) < 4:
                    await broadcast_status("Binary too small", level="error")
                    continue

                w = data[0] | (data[1] << 8)
                h = data[2] | (data[3] << 8)
                px = data[4:4 + w * h * 2]

                try:
                    arr = np.frombuffer(px, dtype=np.uint16).reshape((h, w))
                except Exception:
                    await broadcast_status("Bad image buffer", level="error")
                    continue

                x = 0
                sent = 0
                while x < w:
                    seg_w = min(MAX_SEGMENT_WIDTH, w - x)
                    await send_segment(w, x, arr[:, x:x + seg_w])
                    x += seg_w
                    sent += 1
                    if sent >= 2 and SEGMENT_DELAY_S > 0 and x < w:
                        await asyncio.sleep(SEGMENT_DELAY_S)

                await broadcast_status(f"Image forwarded in {sent} segments")

            # --- Handle WebSocket errors ---
            elif msg.type == WSMsgType.ERROR:
                await broadcast_status(f"WebSocket error: {ws.exception()}", level="error")

    finally:
        browser_clients.discard(ws)
        esp32_clients.discard(ws)
        await broadcast_status(f"Client {peer} disconnected")

    return ws


# --- CORS (allow all) + static serving ---
@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"]  = request.headers.get("Origin", "*")
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    return resp


app = web.Application(middlewares=[cors_middleware])
app.router.add_get("/", index)
app.router.add_get("/ws", websocket_handler)

# serve static font files
app.router.add_static("/Bayon", "./Bayon")
app.router.add_static("/Bokor", "./Bokor")
app.router.add_static("/Koulen", "./Koulen")
app.router.add_static("/Moul", "./Moul")

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=9122)
