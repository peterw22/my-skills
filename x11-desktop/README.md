# x11-desktop

A small MCP server (`server.py`, ~200 lines of Python) that lets Claude Code **see and drive an X11 desktop**, for example an xrdp session on a Fedora/XFCE box. It provides the same actions as Anthropic's computer-use tool (screenshot, click, type, key, scroll, drag), built on `xdotool` and ImageMagick.

## How it works

1. **Finding the display.** It uses `$X11_DISPLAY` if set. Otherwise it picks the lowest-numbered X socket in `/tmp/.X11-unix/` owned by the current user, so it attaches to your RDP session automatically. `XAUTHORITY` defaults to `~/.Xauthority`. If no session exists, the tool errors with "Connect over RDP first".
2. **Screenshots.** It runs `magick import -window root` and downscales to fit Claude's vision limits (≤1568 px on the long edge, ≤1.15 MP). Images are JPEG at quality 80, about 10× smaller than PNG, and text stays readable. Each screenshot comes with a note giving the display, the screenshot and real screen sizes, and the cursor position.
3. **Coordinate mapping.** Every x/y the model sends or receives is in *screenshot space*. The server divides by the same scale factor to get real screen pixels and clamps to the screen bounds. The model never has to think about the real resolution.
4. **Actions return a fresh screenshot.** After any action the server waits 0.6 s for the UI to settle, then takes a new screenshot (pass `screenshot=false` to skip it). This lets the model act, check the result and act again in one loop.

## Tools

| Tool | What it does |
|---|---|
| `screenshot()` | Capture the desktop. Always call this first. |
| `click(x, y, button, count, modifiers)` | left/middle/right click; `count=2` double-clicks; `modifiers="ctrl"` or `"shift+ctrl"` holds keys during the click. |
| `mouse_move(x, y)` | Move without clicking (for hover menus and tooltips). |
| `drag(x1, y1, x2, y2)` | Left-button drag. |
| `type_text(text)` | Types into the focused window. `\n` is sent as an explicit Return. |
| `key(keys)` | xdotool key syntax: `Return`, `ctrl+l`, `alt+F4`. Space-separated chords are pressed in order. |
| `scroll(x, y, direction, amount)` | up/down/left/right wheel clicks at a point. |
| `wait(seconds)` | Sleep (max 30 s), then screenshot. |

## xrdp workarounds in the code

- **Enter:** `xdotool type` doesn't reliably send Enter for `\n` under xrdp, so the server splits the text on newlines and presses `Return` itself.
- **Scroll:** the server moves the pointer back to the target before *every* wheel click. A connected RDP client can move the pointer between events, and a stray wheel on the XFCE desktop switches workspaces. It avoids `--sync`, which hangs when the pointer is already in place.
- **Stuck buttons/modifiers:** if a click or drag fails, the server sends `keyup` or `mouseup` so no modifier or mouse button stays held.
- **Timeouts:** every subprocess has a 30 s timeout and reports errors as MCP `ToolError`s.

## Install

```bash
mkdir -p ~/.local/share/x11-desktop-mcp && cp server.py ~/.local/share/x11-desktop-mcp/
python3 -m venv ~/.local/share/x11-desktop-mcp/venv
~/.local/share/x11-desktop-mcp/venv/bin/pip install "mcp>=2.2"
sudo dnf install xdotool ImageMagick
claude mcp add -s user x11-desktop -- \
  ~/.local/share/x11-desktop-mcp/venv/bin/python ~/.local/share/x11-desktop-mcp/server.py
```

Tools show up as `mcp__x11-desktop__*`. Other skills build on it. For example, the `wecom` skill drives 企业微信 under Wine with these tools.

## Tips for using it

- Take a screenshot first. Coordinates are only valid for the most recent screenshot.
- Use `screenshot=false` on chained actions to save tokens, and screenshot once at the end.
- Use the keyboard (`ctrl+l`, `alt+F4`, `Return`) when you can. It's more reliable than clicking small targets.
