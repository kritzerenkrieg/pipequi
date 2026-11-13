#!/usr/bin/env python3
"""pipequi: a native PipeWire equalizer GUI, with no background process.

The EQ itself is a PipeWire filter-chain loaded from a config file, so it runs
inside PipeWire whether this window is open or not. The window only edits it:
changes go to the running filter through `pw-cli`, and are written back to the
config file so they survive a restart.

Adding a band or changing its type would normally mean rebuilding the filter
(and restarting PipeWire, which drops Bluetooth). Instead the filter holds a
fixed pool of peaking, low-shelf and high-shelf slots chained together; unused
slots sit at 0 dB, which passes sound through unchanged. Bands are mapped onto
slots of their type each time something changes.
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

APP = "pipequi"
CONF_DIR = os.path.join(GLib.get_user_config_dir(), "pipewire", "pipewire.conf.d")
CONF = os.path.join(CONF_DIR, "pipequi.conf")
STATE_DIR = os.path.join(GLib.get_user_config_dir(), APP)
STATE = os.path.join(STATE_DIR, "state.json")

SINK = "effect_input.pipequi"
STREAM = "effect_output.pipequi"
DESCRIPTION = "Equalizer (pipequi)"

TYPES = ("peaking", "lowshelf", "highshelf")
POOL = {"peaking": 12, "lowshelf": 4, "highshelf": 4}
LABEL = {"peaking": "bq_peaking", "lowshelf": "bq_lowshelf", "highshelf": "bq_highshelf"}
PREFIX = {"peaking": "p", "lowshelf": "l", "highshelf": "h"}
TYPE_NAME = {"peaking": "Peak", "lowshelf": "Low shelf", "highshelf": "High shelf"}
COLOR = {"peaking": (0.33, 0.60, 0.95), "lowshelf": (0.95, 0.62, 0.15), "highshelf": (0.30, 0.78, 0.45)}

FMIN, FMAX = 20.0, 20000.0
GMAX = 15.0
QMIN, QMAX = 0.1, 10.0
PRE_MIN, PRE_MAX = -24.0, 6.0
FS = 48000.0  # only for drawing the curve; PipeWire uses the real rate

DEFAULT_BANDS = [
    ("lowshelf", 120.0, 0.5),
    ("peaking", 300.0, 1.0),
    ("peaking", 1000.0, 1.0),
    ("peaking", 3000.0, 1.0),
    ("peaking", 6000.0, 1.0),
    ("highshelf", 10000.0, 0.5),
]


# --- filter maths (RBJ cookbook, the same formulas PipeWire's biquads use) ---

def coefficients(kind, freq, q, gain, fs=FS):
    A = 10 ** (gain / 40)
    w0 = 2 * math.pi * min(freq, fs / 2 * 0.999) / fs
    alpha = math.sin(w0) / (2 * q)
    k = math.cos(w0)
    if kind == "peaking":
        b = (1 + alpha * A, -2 * k, 1 - alpha * A)
        a = (1 + alpha / A, -2 * k, 1 - alpha / A)
    else:
        s = 2 * math.sqrt(A) * alpha
        if kind == "lowshelf":
            b = (A * ((A + 1) - (A - 1) * k + s), 2 * A * ((A - 1) - (A + 1) * k), A * ((A + 1) - (A - 1) * k - s))
            a = ((A + 1) + (A - 1) * k + s, -2 * ((A - 1) + (A + 1) * k), (A + 1) + (A - 1) * k - s)
        else:
            b = (A * ((A + 1) + (A - 1) * k + s), -2 * A * ((A - 1) + (A + 1) * k), A * ((A + 1) + (A - 1) * k - s))
            a = ((A + 1) - (A - 1) * k + s, 2 * ((A - 1) - (A + 1) * k), (A + 1) - (A - 1) * k - s)
    return b, a


def magnitude_db(b, a, freq, fs=FS):
    w = 2 * math.pi * freq / fs
    z = complex(math.cos(w), -math.sin(w))
    num = b[0] + b[1] * z + b[2] * z * z
    den = a[0] + a[1] * z + a[2] * z * z
    return 20 * math.log10(max(abs(num), 1e-12) / max(abs(den), 1e-12))


def response(bands, freqs):
    total = [0.0] * len(freqs)
    for band in bands:
        if abs(band["gain"]) < 1e-6:
            continue
        b, a = coefficients(band["type"], band["freq"], band["q"], band["gain"])
        for i, f in enumerate(freqs):
            total[i] += magnitude_db(b, a, f)
    return total


# --- state and config ---

def default_state():
    return {
        "preamp": 0.0,
        "bypass": False,
        "target": None,
        "bands": [{"type": t, "freq": f, "q": q, "gain": 0.0} for t, f, q in DEFAULT_BANDS],
    }


def load_state():
    state = default_state()
    try:
        with open(STATE) as f:
            saved = json.load(f)
        state.update({k: saved[k] for k in state if k in saved})
        state["bands"] = [b for b in state["bands"] if b.get("type") in TYPES][:sum(POOL.values())]
    except (OSError, ValueError, TypeError):
        pass
    return state


def slot_values(state):
    """Every filter slot with the (freq, q, gain) it should have."""
    slots = {}
    used = {t: 0 for t in TYPES}
    for band in state["bands"]:
        t = band["type"]
        used[t] += 1
        gain = 0.0 if state["bypass"] else band["gain"]
        slots[f"{PREFIX[t]}{used[t]}"] = (band["freq"], band["q"], gain)
    for t in TYPES:
        for i in range(used[t] + 1, POOL[t] + 1):
            slots[f"{PREFIX[t]}{i}"] = (1000.0, 0.7, 0.0)
    slots["preamp"] = (0.0, 1.0, 0.0 if state["bypass"] else state["preamp"])
    return slots


def slot_names():
    names = ["preamp"]
    for t in TYPES:
        names += [f"{PREFIX[t]}{i}" for i in range(1, POOL[t] + 1)]
    return names


def node_label(name):
    if name == "preamp":
        return "bq_highshelf"  # a high shelf at 0 Hz is a plain gain
    return LABEL[{v: k for k, v in PREFIX.items()}[name[0]]]


def render_conf(state):
    slots = slot_values(state)
    names = slot_names()
    nodes = []
    for name in names:
        f, q, g = slots[name]
        nodes.append(f'          {{ type = builtin name = {name:<6} label = {node_label(name):<12} '
                     f'control = {{ "Freq" = {f:.1f} "Q" = {q:.3f} "Gain" = {g:.2f} }} }}')
    links = [f'          {{ output = "{a}:Out" input = "{b}:In" }}' for a, b in zip(names, names[1:])]
    target = f'\n        target.object = "{state["target"]}"' if state.get("target") else ""
    return f"""# Written by pipequi; edit with the pipequi window, not by hand.
context.modules = [
  {{ name = libpipewire-module-filter-chain
    args = {{
      node.description = "{DESCRIPTION}"
      media.name       = "{DESCRIPTION}"
      filter.graph = {{
        nodes = [
{chr(10).join(nodes)}
        ]
        links = [
{chr(10).join(links)}
        ]
      }}
      audio.channels = 2
      audio.position = [ FL FR ]
      capture.props = {{
        node.name   = "{SINK}"
        media.class = Audio/Sink
      }}
      playback.props = {{
        node.name    = "{STREAM}"
        node.passive = true{target}
      }}
    }}
  }}
]
"""


def write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def save(state, conf=True):
    write_atomic(STATE, json.dumps(state, indent=2) + "\n")
    if conf:
        write_atomic(CONF, render_conf(state))


# --- PipeWire ---

def run(cmd, timeout=3):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def pw_dump():
    out = run(["pw-dump"], timeout=5)
    try:
        return json.loads(out.stdout) if out and out.returncode == 0 else []
    except ValueError:
        return []


def props_of(obj):
    return (obj.get("info") or {}).get("props") or obj.get("props") or {}


def nodes(objs):
    return [(o["id"], props_of(o)) for o in objs if o.get("type") == "PipeWire:Interface:Node"]


def find_node_id(name=SINK, objs=None):
    for node_id, props in nodes(pw_dump() if objs is None else objs):
        if props.get("node.name") == name:
            return node_id
    return None


def list_sinks(objs=None):
    """Real outputs the EQ can play into, as (node name, description)."""
    sinks = []
    for _, props in nodes(pw_dump() if objs is None else objs):
        if props.get("media.class") == "Audio/Sink" and props.get("node.name") not in (None, SINK):
            sinks.append((props["node.name"], props.get("node.description") or props.get("node.nick") or props["node.name"]))
    return sinks


def default_sink(objs=None, key="default.audio.sink"):
    """The current default output; key="default.configured.audio.sink" gives the one the user picked,
    which survives while that device is briefly away (e.g. Bluetooth reconnecting)."""
    for obj in pw_dump() if objs is None else objs:
        if obj.get("type") == "PipeWire:Interface:Metadata" and props_of(obj).get("metadata.name") == "default":
            for entry in obj.get("metadata") or []:
                if entry.get("key") == key and isinstance(entry.get("value"), dict):
                    return entry["value"].get("name")
    return None


def set_default_sink(name):
    run(["pw-metadata", "-n", "default", "0", "default.configured.audio.sink",
         json.dumps({"name": name}), "Spa:String:JSON"])


def move_stream(target):
    """Point the EQ's output at another device right away (the config covers later starts)."""
    stream = find_node_id(STREAM)
    if stream is not None:
        run(["pw-metadata", str(stream), "target.object", target])


def restart_pipewire():
    """Restart the user's PipeWire services so a new config file is read. False if that isn't possible."""
    units = ["pipewire.service", "pipewire-pulse.service", "wireplumber.service"]
    active = [u for u in units if (r := run(["systemctl", "--user", "is-active", u])) and r.stdout.strip() == "active"]
    if "pipewire.service" not in active:
        return False
    out = run(["systemctl", "--user", "restart", *active], timeout=30)
    return bool(out) and out.returncode == 0


def bluetooth_connected():
    """Connected Bluetooth devices that PipeWire is playing to (not mice or keyboards)."""
    if not shutil.which("bluetoothctl"):
        return []
    out = run(["bluetoothctl", "devices", "Connected"])
    found = re.findall(r"^Device ([0-9A-F:]{17})", out.stdout, re.M) if out and out.returncode == 0 else []
    audio = bluetooth_sinks()
    return [a for a in found if a.upper() in audio]


def bluetooth_sinks():
    return {p.get("api.bluez5.address", "").upper() for _, p in nodes(pw_dump())
            if p.get("media.class") == "Audio/Sink" and p.get("api.bluez5.address")}


def bluetooth_reconnect(addresses):
    """Bring Bluetooth audio back after a PipeWire restart. Right after the restart a device can still
    look connected and then drop, and WirePlumber takes ~10 s to register its Bluetooth endpoints, so
    keep asking until PipeWire actually has an output for each device."""
    deadline = time.monotonic() + 40
    waiting = {a.upper() for a in addresses}
    while waiting and time.monotonic() < deadline:
        waiting -= bluetooth_sinks()
        for address in waiting:
            run(["bluetoothctl", "connect", address], timeout=15)
        if waiting:
            time.sleep(2)


def push_params(node_id, state):
    params = []
    for name, (f, q, g) in slot_values(state).items():
        params += [f'"{name}:Freq"', f"{f:.3f}", f'"{name}:Q"', f"{q:.4f}", f'"{name}:Gain"', f"{g:.3f}"]
    out = run(["pw-cli", "s", str(node_id), "Props", "{ params = [ " + " ".join(params) + " ] }"])
    return bool(out) and out.returncode == 0 and "Error" not in (out.stdout + out.stderr)


# --- window ---

class Graph(Gtk.DrawingArea):
    PAD_L, PAD_R, PAD_T, PAD_B = 40, 14, 12, 24
    HIT = 11

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.drag = None
        self.set_size_request(640, 300)
        self.set_can_focus(True)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK
                        | Gdk.EventMask.SMOOTH_SCROLL_MASK | Gdk.EventMask.KEY_PRESS_MASK)
        self.connect("draw", self.on_draw)
        self.connect("button-press-event", self.on_press)
        self.connect("button-release-event", self.on_release)
        self.connect("motion-notify-event", self.on_motion)
        self.connect("scroll-event", self.on_scroll)
        self.connect("key-press-event", self.on_key)

    # coordinates
    def area(self):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        return self.PAD_L, self.PAD_T, w - self.PAD_L - self.PAD_R, h - self.PAD_T - self.PAD_B

    def f2x(self, f):
        x, _, w, _ = self.area()
        return x + math.log(f / FMIN) / math.log(FMAX / FMIN) * w

    def x2f(self, px):
        x, _, w, _ = self.area()
        return FMIN * (FMAX / FMIN) ** min(max((px - x) / w, 0.0), 1.0)

    def g2y(self, g):
        _, y, _, h = self.area()
        return y + (GMAX - g) / (2 * GMAX) * h

    def y2g(self, py):
        _, y, _, h = self.area()
        return min(max(GMAX - (py - y) / h * 2 * GMAX, -GMAX), GMAX)

    def hit(self, px, py):
        best, dist = None, self.HIT
        for i, band in enumerate(self.win.state["bands"]):
            d = math.hypot(self.f2x(band["freq"]) - px, self.g2y(band["gain"]) - py)
            if d <= dist:
                best, dist = i, d
        return best

    # drawing
    def on_draw(self, widget, cr):
        ctx = self.get_style_context()
        w, h = self.get_allocated_width(), self.get_allocated_height()
        Gtk.render_background(ctx, cr, 0, 0, w, h)
        fg = ctx.get_color(Gtk.StateFlags.NORMAL)
        ax, ay, aw, ah = self.area()
        state = self.win.state

        cr.set_font_size(10)
        cr.set_line_width(1)
        for f in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
            x = round(self.f2x(f)) + 0.5
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
            cr.move_to(x, ay)
            cr.line_to(x, ay + ah)
            cr.stroke()
            text = f"{f // 1000}k" if f >= 1000 else str(f)
            ext = cr.text_extents(text)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.6)
            cr.move_to(min(max(x - ext.width / 2, ax), ax + aw - ext.width), ay + ah + 15)
            cr.show_text(text)
        for g in range(-15, 16, 5):
            y = round(self.g2y(g)) + 0.5
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.35 if g == 0 else 0.12)
            cr.move_to(ax, y)
            cr.line_to(ax + aw, y)
            cr.stroke()
            text = f"{g:+d}" if g else "0"
            ext = cr.text_extents(text)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.6)
            cr.move_to(ax - ext.width - 6, y + ext.height / 2)
            cr.show_text(text)

        n = max(int(aw), 2)
        freqs = [FMIN * (FMAX / FMIN) ** (i / (n - 1)) for i in range(n)]
        curve = response(state["bands"], freqs)
        cr.save()
        cr.rectangle(ax, ay, aw, ah)
        cr.clip()
        cr.set_line_width(2.5)
        if state["bypass"]:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.35)
            cr.set_dash([6, 4])
        else:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.9)
        for i, g in enumerate(curve):
            (cr.line_to if i else cr.move_to)(ax + i * aw / (n - 1), self.g2y(g))
        cr.stroke()

        # Guide from each point straight up or down to where the curve really
        # is at that frequency, since overlapping bands move the curve off the point.
        at = response(state["bands"], [band["freq"] for band in state["bands"]])
        cr.set_line_width(1.2)
        for i, band in enumerate(state["bands"]):
            x = round(self.f2x(band["freq"])) + 0.5
            y, cy = self.g2y(band["gain"]), self.g2y(at[i])
            r, g, b = COLOR[band["type"]]
            alpha = 1.0 if i == self.win.selected else 0.6
            cr.set_source_rgba(r, g, b, alpha)
            cr.set_dash([3, 3])
            cr.move_to(x, y)
            cr.line_to(x, cy)
            cr.stroke()
            cr.set_dash([])
            cr.arc(x, cy, 3.5, 0, 2 * math.pi)
            cr.fill()
        cr.restore()

        for i, band in enumerate(state["bands"]):
            x, y = self.f2x(band["freq"]), self.g2y(band["gain"])
            r, g, b = COLOR[band["type"]]
            if i == self.win.selected:
                cr.set_source_rgba(r, g, b, 0.35)
                cr.arc(x, y, 13, 0, 2 * math.pi)
                cr.fill()
            cr.set_source_rgb(r, g, b)
            cr.arc(x, y, 8, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgb(1, 1, 1)
            text = str(i + 1)
            ext = cr.text_extents(text)
            cr.move_to(x - ext.x_bearing - ext.width / 2, y - ext.y_bearing - ext.height / 2)
            cr.show_text(text)

    # interaction
    def on_press(self, widget, ev):
        self.grab_focus()
        i = self.hit(ev.x, ev.y)
        if ev.type == Gdk.EventType._2BUTTON_PRESS and ev.button == 1:
            if i is None:
                self.win.add_band("peaking", self.x2f(ev.x), self.y2g(ev.y))
            return True
        if ev.button == 3 and i is not None:
            self.win.select(i)
            self.win.band_menu(ev)
            return True
        if ev.button == 1:
            self.win.select(i)
            if i is not None:
                self.drag = i
        return True

    def on_release(self, widget, ev):
        if self.drag is not None:
            self.drag = None
            self.win.changed(save_only=True)
        return True

    def on_motion(self, widget, ev):
        if self.drag is None:
            hover = self.hit(ev.x, ev.y) is not None
            win = self.get_window()
            if win:
                win.set_cursor(Gdk.Cursor.new_from_name(self.get_display(), "grab" if hover else "default"))
            return False
        band = self.win.state["bands"][self.drag]
        band["freq"] = round(self.x2f(ev.x), 1)
        band["gain"] = round(self.y2g(ev.y) * 2) / 2 if ev.state & Gdk.ModifierType.CONTROL_MASK else round(self.y2g(ev.y), 1)
        self.win.changed(live_only=True)
        return True

    def on_scroll(self, widget, ev):
        i = self.hit(ev.x, ev.y)
        if i is None:
            i = self.win.selected
        if i is None:
            return False
        if ev.direction == Gdk.ScrollDirection.UP:
            step = -1.0
        elif ev.direction == Gdk.ScrollDirection.DOWN:
            step = 1.0
        elif ev.direction == Gdk.ScrollDirection.SMOOTH:
            step = ev.delta_y
        else:
            return False
        band = self.win.state["bands"][i]
        band["q"] = round(min(max(band["q"] * 1.12 ** -step, QMIN), QMAX), 3)
        self.win.select(i)
        self.win.changed()
        return True

    def on_key(self, widget, ev):
        if ev.keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace) and self.win.selected is not None:
            self.win.remove_band(self.win.selected)
            return True
        return False


class Window(Gtk.Window):
    def __init__(self):
        super().__init__(title="pipequi")
        self.set_default_size(820, 470)
        self.set_icon_name("multimedia-volume-control")
        self.state = load_state()
        self.selected = None
        self.node_id = None
        self.live_pending = False
        self.save_source = None
        self.updating = False

        header = Gtk.HeaderBar(show_close_button=True, title="pipequi", subtitle="Native PipeWire equalizer")
        self.set_titlebar(header)
        self.bypass = Gtk.Switch(active=self.state["bypass"], valign=Gtk.Align.CENTER,
                                 tooltip_text="Turn the EQ off without losing your settings")
        self.bypass.connect("notify::active", self.on_bypass)
        box = Gtk.Box(spacing=6)
        box.pack_start(Gtk.Label(label="Bypass"), False, False, 0)
        box.pack_start(self.bypass, False, False, 0)
        header.pack_end(box)
        reset = Gtk.Button(label="Reset", tooltip_text="Six flat bands and 0 dB preamp")
        reset.connect("clicked", self.on_reset)
        header.pack_start(reset)
        menu = Gtk.Menu()
        self.remove_item = Gtk.MenuItem(label="Remove from PipeWire…")
        self.remove_item.connect("activate", self.on_remove)
        menu.append(self.remove_item)
        menu.show_all()
        menu_button = Gtk.MenuButton(popup=menu, tooltip_text="More")
        menu_button.add(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        header.pack_end(menu_button)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=10)
        self.add(root)

        self.bar = Gtk.InfoBar(message_type=Gtk.MessageType.INFO, show_close_button=False)
        self.bar_label = Gtk.Label(xalign=0, wrap=True)
        self.bar.get_content_area().add(self.bar_label)
        self.setup_button = self.bar.add_button("Set up", 1)
        self.bar.connect("response", lambda _bar, r: r == 1 and self.on_setup())
        self.bar.set_no_show_all(True)
        root.pack_start(self.bar, False, False, 0)

        self.graph = Graph(self)
        frame = Gtk.Frame()
        frame.add(self.graph)
        root.pack_start(frame, True, True, 0)

        hint = Gtk.Label(xalign=0, label="Drag a point to move it · scroll on it to change width (Q) · "
                                          "double-click empty space to add · right-click or Delete to remove")
        hint.get_style_context().add_class("dim-label")
        root.pack_start(hint, False, False, 0)

        # selected band
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        root.pack_start(grid, False, False, 0)
        self.band_title = Gtk.Label(xalign=0, width_chars=8)
        self.kind = Gtk.ComboBoxText()
        for t in TYPES:
            self.kind.append(t, TYPE_NAME[t])
        self.kind.connect("changed", self.on_kind)
        self.freq = self.spin(FMIN, FMAX, 1, 0, self.on_spin)
        self.gain = self.spin(-GMAX, GMAX, 0.5, 1, self.on_spin)
        self.q = self.spin(QMIN, QMAX, 0.05, 2, self.on_spin)
        self.remove_btn = Gtk.Button(label="Remove")
        self.remove_btn.connect("clicked", lambda *_: self.remove_band(self.selected))
        widgets = [self.band_title, self.kind, Gtk.Label(label="Hz"), self.freq,
                   Gtk.Label(label="dB"), self.gain, Gtk.Label(label="Q"), self.q, self.remove_btn]
        for col, wdg in enumerate(widgets):
            grid.attach(wdg, col, 0, 1, 1)
        self.band_widgets = widgets[1:]

        # preamp and output
        grid2 = Gtk.Grid(column_spacing=8, row_spacing=6)
        root.pack_start(grid2, False, False, 0)
        grid2.attach(Gtk.Label(label="Preamp", xalign=0, width_chars=8), 0, 0, 1, 1)
        self.preamp = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, PRE_MIN, PRE_MAX, 0.5)
        self.preamp.set_value(self.state["preamp"])
        self.preamp.set_hexpand(True)
        self.preamp.add_mark(0, Gtk.PositionType.BOTTOM, None)
        self.preamp.connect("format-value", lambda s, v: f"{v:+.1f} dB")
        self.preamp.connect("value-changed", self.on_preamp)
        grid2.attach(self.preamp, 1, 0, 1, 1)
        auto = Gtk.Button(label="Auto", tooltip_text="Lower the preamp by the biggest boost so loud songs don't distort")
        auto.connect("clicked", self.on_auto)
        grid2.attach(auto, 2, 0, 1, 1)
        self.clip = Gtk.Label(xalign=0)
        grid2.attach(self.clip, 1, 1, 2, 1)

        grid2.attach(Gtk.Label(label="Output", xalign=0), 0, 2, 1, 1)
        self.output = Gtk.ComboBoxText(hexpand=True)
        self.output.connect("changed", self.on_output)
        grid2.attach(self.output, 1, 2, 1, 1)
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text("Refresh the list of outputs")
        refresh.connect("clicked", lambda *_: self.fill_outputs())
        grid2.attach(refresh, 2, 2, 1, 1)

        self.connect("delete-event", self.on_close)
        self.fill_outputs()
        self.select(None)
        self.update_clip()
        self.check_node()

    @staticmethod
    def spin(lo, hi, step, digits, cb):
        s = Gtk.SpinButton.new_with_range(lo, hi, step)
        s.set_digits(digits)
        s.connect("value-changed", cb)
        return s

    # PipeWire link
    def check_node(self):
        self.node_id = find_node_id()
        self.remove_item.set_sensitive(self.node_id is not None or os.path.exists(CONF))
        if self.node_id is None:
            self.show_bar("pipequi isn't set up yet. Set up adds the equalizer to PipeWire as a new output and makes it "
                          "your default. Sound stops for a second; Bluetooth headphones reconnect by themselves.")
        else:
            self.bar.hide()
            push_params(self.node_id, self.state)

    def show_bar(self, text, kind=Gtk.MessageType.INFO, button=True, busy=False):
        self.bar.set_message_type(kind)
        self.bar_label.set_text(text)
        self.bar.show_all()
        self.setup_button.set_visible(button)
        self.setup_button.set_sensitive(not busy)

    def restart_then(self, done):
        """Restart PipeWire off the UI thread, bring Bluetooth audio back, then call done(ok)."""
        bluetooth = bluetooth_connected()

        def work():
            ok = restart_pipewire()
            if ok:
                for _ in range(20):  # wait for PipeWire to come back
                    time.sleep(0.5)
                    if pw_dump():
                        break
                bluetooth_reconnect(bluetooth)
            GLib.idle_add(done, ok)

        threading.Thread(target=work, daemon=True).start()

    def on_setup(self):
        objs = pw_dump()
        if not self.state.get("target"):
            sinks = [name for name, _ in list_sinks(objs)]
            picks = [default_sink(objs, "default.configured.audio.sink"), default_sink(objs)] + sinks
            self.state["target"] = next((name for name in picks if name in sinks), None)
        save(self.state)
        self.show_bar("Setting up… sound will stop for a second.", busy=True)
        self.restart_then(self.after_setup)

    def after_setup(self, ok):
        if not ok:
            self.show_bar("pipequi saved its PipeWire settings but couldn't restart PipeWire. "
                          "Log out and back in to finish setting up.", Gtk.MessageType.WARNING, button=False)
            return False
        self.check_node()
        if self.node_id is None:
            self.show_bar("PipeWire restarted but the equalizer didn't load. Check `journalctl --user -u pipewire`.",
                          Gtk.MessageType.ERROR)
            return False
        set_default_sink(SINK)
        self.fill_outputs()
        return False

    def on_remove(self, *_):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.OK_CANCEL, text="Remove the equalizer from PipeWire?")
        dialog.format_secondary_text("Sound goes straight to your device again. Your bands are kept, so Set up "
                                     "brings them back. Sound stops for a second.")
        answer = dialog.run()
        dialog.destroy()
        if answer != Gtk.ResponseType.OK:
            return
        if self.save_source:
            GLib.source_remove(self.save_source)
            self.save_source = None
        write_atomic(STATE, json.dumps(self.state, indent=2) + "\n")
        try:
            os.remove(CONF)
        except FileNotFoundError:
            pass
        self.node_id = None
        self.show_bar("Removing… sound will stop for a second.", button=False)
        self.restart_then(self.after_remove)

    def after_remove(self, ok):
        if self.state.get("target"):
            set_default_sink(self.state["target"])
        self.check_node()
        if not ok:
            self.show_bar("The equalizer is removed and will be gone after you log out and back in.",
                          Gtk.MessageType.WARNING, button=False)
        return False

    # changes
    def changed(self, live_only=False, save_only=False):
        if not save_only:
            self.graph.queue_draw()
            self.sync_band_widgets()
            self.update_clip()
            if not self.live_pending:
                self.live_pending = True
                GLib.timeout_add(30, self.flush_live)
        if not live_only:
            if self.save_source:
                GLib.source_remove(self.save_source)
            self.save_source = GLib.timeout_add(400, self.flush_save)

    def flush_live(self):
        self.live_pending = False
        if self.node_id is not None and not push_params(self.node_id, self.state):
            self.node_id = find_node_id()
            if self.node_id is not None:
                push_params(self.node_id, self.state)
        return False

    def flush_save(self):
        self.save_source = None
        save(self.state, conf=os.path.exists(CONF))  # only once set up; Set up writes it the first time
        return False

    def on_close(self, *_):
        if self.save_source:
            GLib.source_remove(self.save_source)
            self.flush_save()
        if self.live_pending:
            self.flush_live()
        Gtk.main_quit()

    # bands
    def free_slot(self, kind, ignore=None):
        used = sum(1 for i, b in enumerate(self.state["bands"]) if b["type"] == kind and i != ignore)
        return used < POOL[kind]

    def add_band(self, kind, freq, gain):
        if not self.free_slot(kind):
            self.flash(f"No room for another {TYPE_NAME[kind].lower()} band (max {POOL[kind]}).")
            return
        self.state["bands"].append({"type": kind, "freq": round(freq, 1), "q": 1.0, "gain": round(gain, 1)})
        self.select(len(self.state["bands"]) - 1)
        self.changed()

    def remove_band(self, i):
        if i is None:
            return
        del self.state["bands"][i]
        self.select(None)
        self.changed()

    def select(self, i):
        self.selected = i
        self.sync_band_widgets()
        self.graph.queue_draw()

    def sync_band_widgets(self):
        self.updating = True
        band = self.state["bands"][self.selected] if self.selected is not None else None
        for w in self.band_widgets:
            w.set_sensitive(band is not None)
        if band:
            self.band_title.set_markup(f"<b>Band {self.selected + 1}</b>")
            self.kind.set_active_id(band["type"])
            self.freq.set_value(band["freq"])
            self.gain.set_value(band["gain"])
            self.q.set_value(band["q"])
        else:
            self.band_title.set_markup("<b>No band</b>")
        self.updating = False

    def on_spin(self, *_):
        if self.updating or self.selected is None:
            return
        band = self.state["bands"][self.selected]
        band.update(freq=self.freq.get_value(), gain=self.gain.get_value(), q=self.q.get_value())
        self.changed()

    def on_kind(self, combo):
        if self.updating or self.selected is None:
            return
        kind = combo.get_active_id()
        band = self.state["bands"][self.selected]
        if kind == band["type"]:
            return
        if not self.free_slot(kind, ignore=self.selected):
            self.flash(f"No room for another {TYPE_NAME[kind].lower()} band (max {POOL[kind]}).")
            self.sync_band_widgets()
            return
        band["type"] = kind
        self.changed()

    def band_menu(self, ev):
        menu = Gtk.Menu()
        band = self.state["bands"][self.selected]
        for t in TYPES:
            item = Gtk.CheckMenuItem(label=TYPE_NAME[t], active=band["type"] == t, draw_as_radio=True)
            item.connect("activate", lambda _w, t=t: self.kind.set_active_id(t))
            menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())
        remove = Gtk.MenuItem(label="Remove")
        remove.connect("activate", lambda *_: self.remove_band(self.selected))
        menu.append(remove)
        menu.show_all()
        menu.popup_at_pointer(ev)

    # preamp, bypass, output, reset
    def peak_boost(self):
        n = 400
        freqs = [FMIN * (FMAX / FMIN) ** (i / (n - 1)) for i in range(n)]
        return max(response(self.state["bands"], freqs))

    def update_clip(self):
        peak = self.peak_boost()
        headroom = peak + self.state["preamp"]
        if headroom > 0.25 and not self.state["bypass"]:
            self.clip.set_markup(f"<small>Biggest boost {peak:+.1f} dB with preamp {self.state['preamp']:+.1f} dB: "
                                 f"loud songs may distort. Press Auto.</small>")
        else:
            self.clip.set_markup(f"<small>Biggest boost {peak:+.1f} dB. No distortion risk.</small>")

    def on_preamp(self, scale):
        if self.updating:
            return
        self.state["preamp"] = round(scale.get_value(), 1)
        self.changed()

    def on_auto(self, *_):
        self.state["preamp"] = -math.ceil(max(self.peak_boost(), 0.0) * 2) / 2
        self.updating = True
        self.preamp.set_value(self.state["preamp"])
        self.updating = False
        self.changed()

    def on_bypass(self, switch, _param):
        self.state["bypass"] = switch.get_active()
        self.changed()

    def on_reset(self, *_):
        keep = {k: self.state.get(k) for k in ("target", "bypass")}
        self.state = default_state()
        self.state.update(keep)
        self.updating = True
        self.preamp.set_value(0.0)
        self.updating = False
        self.select(None)
        self.changed()

    def fill_outputs(self):
        self.updating = True
        self.output.remove_all()
        sinks = list_sinks()
        target = self.state.get("target")
        if target and target not in [n for n, _ in sinks]:
            sinks.append((target, f"{target} (not connected)"))
        for name, desc in sinks:
            self.output.append(name, desc)
        if target:
            self.output.set_active_id(target)
        self.updating = False

    def on_output(self, combo):
        if self.updating:
            return
        target = combo.get_active_id()
        if not target or target == self.state.get("target"):
            return
        self.state["target"] = target
        move_stream(target)
        self.changed(save_only=True)

    def flash(self, text):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.INFO,
                                   buttons=Gtk.ButtonsType.OK, text=text)
        dialog.run()
        dialog.destroy()


def main():
    if "--print-conf" in sys.argv:
        print(render_conf(load_state()), end="")
        return
    missing = [tool for tool in ("pw-cli", "pw-dump", "pw-metadata") if not shutil.which(tool)]
    if missing:
        dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                                   text="pipequi needs PipeWire's command-line tools")
        dialog.format_secondary_text(
            f"Missing: {', '.join(missing)}.\n\nInstall them with your package manager:\n"
            "Debian/Ubuntu: pipewire-bin\nFedora: pipewire-utils\nArch: pipewire")
        dialog.run()
        sys.exit(1)
    win = Window()
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
