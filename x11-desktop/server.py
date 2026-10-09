"""MCP server that lets Claude Code see and drive an X11 desktop (e.g. an xrdp session).

Mirrors the actions of Anthropic's computer-use tool: screenshot, click, drag,
type, key, scroll. Screenshots are downscaled so the model sees them without
further resizing; every coordinate the tools accept or report is in screenshot
space and is mapped back to real screen pixels here.

Config (env):
  X11_DISPLAY   display to drive, e.g. ":10" (default: this user's lowest X socket)
  XAUTHORITY    X auth file (default: ~/.Xauthority)
"""

import math
import os
import re
import subprocess
import time
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

# Claude vision downsamples above ~1568px on the long edge or ~1.15 MP.
MAX_LONG_EDGE = 1568
MAX_PIXELS = 1_150_000
SETTLE_SECONDS = 0.6

mcp = MCPServer(
    "x11-desktop",
    instructions=(
        "Control the user's X11 desktop. Call screenshot first; all x/y "
        "coordinates are in the pixel space of the most recent screenshot. "
        "Actions return a fresh screenshot unless screenshot=false."
    ),
)


def _display() -> str:
    if d := os.environ.get("X11_DISPLAY"):
        return d
    uid = os.getuid()
    nums = sorted(
        int(m.group(1))
        for p in Path("/tmp/.X11-unix").glob("X*")
        if (m := re.fullmatch(r"X(\d+)", p.name)) and p.stat().st_uid == uid
    )
    if not nums:
        raise ToolError("No X session found for this user. Connect over RDP first.")
    return f":{nums[0]}"


def _env() -> dict:
    env = dict(os.environ)
    env["DISPLAY"] = _display()
    env.setdefault("XAUTHORITY", str(Path.home() / ".Xauthority"))
    return env


def _run(*args: str, input: bytes | None = None) -> bytes:
    try:
        r = subprocess.run(args, env=_env(), input=input, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise ToolError(f"{args[0]} timed out after 30s")
    if r.returncode != 0:
        raise ToolError(f"{args[0]} failed: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def _screen_size() -> tuple[int, int]:
    w, h = _run("xdotool", "getdisplaygeometry").split()
    return int(w), int(h)


def _scale(w: int, h: int) -> float:
    return min(1.0, MAX_LONG_EDGE / max(w, h), math.sqrt(MAX_PIXELS / (w * h)))


def _to_screen(x: int, y: int) -> tuple[int, int]:
    w, h = _screen_size()
    s = _scale(w, h)
    return min(w - 1, max(0, round(x / s))), min(h - 1, max(0, round(y / s)))


def _capture() -> list:
    w, h = _screen_size()
    s = _scale(w, h)
    sw, sh = round(w * s), round(h * s)
    # JPEG: ~10x smaller than PNG for desktops with wallpaper, and UI text stays legible.
    jpg = _run("magick", "import", "-silent", "-window", "root", "-resize", f"{sw}x{sh}!",
               "-strip", "-quality", "80", "jpg:-")
    mx, my = (int(v.split(":")[1]) for v in _run("xdotool", "getmouselocation").decode().split()[:2])
    note = (
        f"display {_display()}: screenshot {sw}x{sh} (screen {w}x{h}); "
        f"cursor at ({round(mx * s)}, {round(my * s)})"
    )
    return [note, Image(data=jpg, format="jpeg")]


def _pick(table: dict, value: str, what: str) -> str:
    if value not in table:
        raise ToolError(f"{what} must be one of {', '.join(table)}; got {value!r}")
    return table[value]


def _after(screenshot: bool, msg: str) -> list | str:
    if not screenshot:
        return msg
    time.sleep(SETTLE_SECONDS)
    return [msg, *_capture()]


@mcp.tool(structured_output=False)
def screenshot() -> list:
    """Capture the desktop. Coordinates for other tools use this image's pixel space."""
    return _capture()


@mcp.tool(structured_output=False)
def click(x: int, y: int, button: str = "left", count: int = 1, modifiers: str = "", screenshot: bool = True) -> list | str:
    """Click at (x, y). button: left|middle|right. count: 2 for double-click.
    modifiers: optional xdotool keys held during the click, e.g. "ctrl" or "shift+ctrl"."""
    btn = _pick({"left": "1", "middle": "2", "right": "3"}, button, "button")
    sx, sy = _to_screen(x, y)
    mods = [m for m in modifiers.split("+") if m]
    cmd = ["xdotool"]
    if mods:
        cmd += ["keydown", *mods]
    cmd += ["mousemove", str(sx), str(sy), "click", "--repeat", str(count), "--delay", "80", btn]
    if mods:
        cmd += ["keyup", *mods]
    try:
        _run(*cmd)
    except ToolError:
        if mods:
            _run("xdotool", "keyup", *mods)
        raise
    return _after(screenshot, f"{button} click x{count} at ({x}, {y})")


@mcp.tool(structured_output=False)
def mouse_move(x: int, y: int, screenshot: bool = False) -> list | str:
    """Move the pointer to (x, y) without clicking (e.g. to reveal a hover)."""
    sx, sy = _to_screen(x, y)
    _run("xdotool", "mousemove", str(sx), str(sy))
    return _after(screenshot, f"moved to ({x}, {y})")


@mcp.tool(structured_output=False)
def drag(x1: int, y1: int, x2: int, y2: int, screenshot: bool = True) -> list | str:
    """Press the left button at (x1, y1), drag to (x2, y2), release."""
    a, b = _to_screen(x1, y1), _to_screen(x2, y2)
    try:
        _run("xdotool", "mousemove", str(a[0]), str(a[1]), "mousedown", "1",
             "mousemove", str(b[0]), str(b[1]), "mouseup", "1")
    except ToolError:
        _run("xdotool", "mouseup", "1")
        raise
    return _after(screenshot, f"dragged ({x1}, {y1}) -> ({x2}, {y2})")


@mcp.tool(structured_output=False)
def type_text(text: str, screenshot: bool = True) -> list | str:
    """Type text into the focused window. Newlines press Enter."""
    # xdotool type doesn't reliably send Enter for "\n" under xrdp, so press Return explicitly.
    for i, line in enumerate(text.split("\n")):
        if i:
            _run("xdotool", "key", "Return")
        if line:
            _run("xdotool", "type", "--delay", "12", "--", line)
    return _after(screenshot, f"typed {len(text)} chars")


@mcp.tool(structured_output=False)
def key(keys: str, screenshot: bool = True) -> list | str:
    """Press a key or chord in xdotool syntax, e.g. "Return", "ctrl+l", "alt+F4", "super".
    Several space-separated chords are pressed in order."""
    _run("xdotool", "key", "--delay", "40", *keys.split())
    return _after(screenshot, f"pressed {keys}")


@mcp.tool(structured_output=False)
def scroll(x: int, y: int, direction: str = "down", amount: int = 3, screenshot: bool = True) -> list | str:
    """Scroll at (x, y). direction: up|down|left|right. amount: wheel clicks."""
    btn = _pick({"up": "4", "down": "5", "left": "6", "right": "7"}, direction, "direction")
    sx, sy = _to_screen(x, y)
    # Re-anchor before every wheel click: a connected RDP client can move the pointer
    # between our events, and a stray wheel on the Xfce desktop switches workspaces.
    # No --sync in chains: it hangs when the pointer is already there, and XTEST events
    # from one connection are processed in order anyway.
    cmd = ["xdotool"]
    for _ in range(max(1, amount)):
        cmd += ["mousemove", str(sx), str(sy), "click", btn, "sleep", "0.04"]
    _run(*cmd)
    return _after(screenshot, f"scrolled {direction} x{amount} at ({x}, {y})")


@mcp.tool(structured_output=False)
def wait(seconds: float = 1.0, screenshot: bool = True) -> list | str:
    """Wait (max 30s), e.g. for an app to load, then optionally screenshot."""
    time.sleep(max(0.0, min(seconds, 30.0)))
    return _after(screenshot, f"waited {seconds}s")


if __name__ == "__main__":
    mcp.run()
