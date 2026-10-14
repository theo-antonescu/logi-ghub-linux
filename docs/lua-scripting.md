# Lua scripts

G HUB (and Logitech Gaming Software before it) lets every profile carry a Lua
script. This program runs the same scripts, with the same API, while a
computer profile is active in host mode.

## How G HUB does it

The reference is Logitech's *G-series Lua API* (v8.45, the PDF G HUB links to).
What matters for a Linux version:

* **The script never goes to the device.** G HUB runs it on the PC, on its own
  thread, while G HUB is open and the profile is active. Nothing about scripts
  is in on-board memory, so there is nothing to capture or reverse engineer:
  running the script in `logi-ghubd` is exactly equivalent.
* **One entry point.** The script defines `OnEvent(event, arg, family)`. G HUB
  calls it for `PROFILE_ACTIVATED` (first), `PROFILE_DEACTIVATED` (last),
  `G_PRESSED`/`G_RELEASED` (G-keys), `M_PRESSED`/`M_RELEASED` (M-keys) and
  `MOUSE_BUTTON_PRESSED`/`MOUSE_BUTTON_RELEASED` (mouse buttons; the left button
  only after `EnablePrimaryMouseButtonEvents(true)`). `family` is `"kb"`,
  `"lhc"` or `"mouse"`.
* **Events are handled one at a time, in order.** While `OnEvent` is busy
  (for example in a `Sleep` loop) new events wait. When the profile is switched
  off the script gets 1 second to finish, then it is aborted.
* **Output functions** simulate input: `PressKey`/`ReleaseKey`/`PressAndReleaseKey`
  (key names or Windows scancodes), mouse buttons, relative and absolute pointer
  moves, the wheel, and `PlayMacro`/`AbortMacro` for the profile's macros.
* **Device functions**: `SetBacklightColor`, `SetMouseDPITable`,
  `SetMouseDPITableIndex`, M-key state, the G13/G15 LCD and steering wheels.
* **Queries**: `IsModifierPressed`, `IsKeyLockOn`, `IsMouseButtonPressed`,
  `GetMousePosition`, `GetRunningTime`, `GetDate`.
* **Libraries**: string, table and math only. No `io`, `os`, `require`.

The most common real-world use is a loop started by a button press that runs
until the button is released, for example:

```lua
EnablePrimaryMouseButtonEvents(true)
function OnEvent(event, arg)
    if event == "MOUSE_BUTTON_PRESSED" and arg == 5 then
        repeat
            PressAndReleaseKey("f")
            Sleep(50)
        until not IsMouseButtonPressed(5)
    end
end
```

## In this program

Each computer profile can hold a script (Script page in the app, or
`logi-ghub-cli profile script`). When the profile runs in host mode, the
`logi-ghubd` service starts it on its own thread. Saving the profile restarts
the script; switching the profile off sends `PROFILE_DEACTIVATED`, waits a
second, then stops it and lets go of any key or button it still holds.

Lua is provided by the [lupa](https://pypi.org/project/lupa/) package, which
`scripts/install.sh` installs into the app's environment. Scripts run on
**Lua 5.4**; the Lua 5.1 functions the reference lists that 5.4 dropped
(`math.pow`, `math.log10`, `math.ldexp`, `math.frexp`, `math.sinh`/`cosh`/`tanh`,
`table.maxn`, `table.getn`, `unpack`) are added back.

Scripts are sandboxed: they see the API and the string/table/math libraries,
plus `pairs`, `ipairs`, `tostring` and the other basic functions, but not `io`,
`os`, `require`, `load`, `debug` or Python. A script pasted from the internet
can type and click, but it can't read files or start programs. Each script may
use up to 64 MB.

### Events on these devices

| Device | Events a script gets |
|---|---|
| G502 X LIGHTSPEED (host mode) | `PROFILE_*`, and `MOUSE_BUTTON_*` for all 11 buttons |
| G915 X LIGHTSPEED TKL | `PROFILE_*` only: it has no G-keys or M-keys (G HUB gives it no others either). Scripts on any profile can still colour it with `SetBacklightColor`. |
| G600 | none yet: it has no host mode here (its buttons are handled by its own memory) |
| Keyboards with G-keys (HID++ 0x8010) | `G_PRESSED`/`G_RELEASED`, family `"kb"` (not tested; no such keyboard here) |

Mouse button numbers follow G HUB: 1 left, 2 right, 3 middle, then the G-number
printed on the button. For the G502 X:

| arg | Button |
|---|---|
| 1 | Left click |
| 2 | Right click |
| 3 | Middle click (wheel) |
| 4 | Back (G4) |
| 5 | Forward (G5) |
| 6 | Sniper / DPI shift (G6) |
| 7 | DPI down (G7) |
| 8 | DPI up (G8) |
| 9 | Profile button (G9) |
| 10 | Wheel tilt left |
| 11 | Wheel tilt right |

`OnEvent` uses this numbering, while `PressMouseButton` and
`IsMouseButtonPressed` use Windows' (1 left, 2 middle, 3 right, 4 back,
5 forward). That mismatch is G HUB's, and scripts written for G HUB depend on it.

A button keeps doing what it is assigned on the Assignments page; the script
is told about the press as well. Assign *Disabled* to a button you want only
the script to handle.

### Function by function

| Function | On Linux |
|---|---|
| `OnEvent` | as above |
| `Sleep(ms)` | waits on the script's thread; returns early (and stops the script) when the profile is switched off |
| `OutputLogMessage(fmt, ...)`, `print` | appended to the profile's script log: the Output box on the Script page, or `logi-ghub-cli profile log NAME -f` |
| `ClearLog()` | empties that log |
| `OutputDebugMessage` | the service's journal (`journalctl --user -u logi-ghubd`) |
| `GetRunningTime()`, `GetDate()` | as documented (`GetDate` is `os.date`) |
| `PressKey`, `ReleaseKey`, `PressAndReleaseKey` | through the service's virtual keyboard; every key name and scancode in Appendix A, plus the media-key scancodes |
| `PressMouseButton`, `ReleaseMouseButton`, `PressAndReleaseMouseButton` | virtual mouse, buttons 1-5 |
| `MoveMouseRelative(x, y)`, `MoveMouseWheel(n)` | virtual mouse |
| `MoveMouseTo(x, y)`, `MoveMouseToVirtual(x, y)` | a virtual absolute pointer created on first use; as on Windows, 0-65535 covers the primary monitor for `MoveMouseTo` and every monitor for `MoveMouseToVirtual` (outside GNOME both cover every monitor) |
| `GetMousePosition()` | the last position the script set with `MoveMouseTo`; Wayland doesn't let programs read the real pointer position |
| `IsMouseButtonPressed(n)` | true while a Logitech mouse running a host profile has a button acting as that button held down (from the mouse itself, so the left button works), or the script holds it |
| `IsModifierPressed(name)` | true when the script holds the key; also physical keys on input devices this user can read (by default none: they belong to the `input` group) |
| `IsKeyLockOn(name)` | the caps/num/scroll lock LEDs in `/sys/class/leds` |
| `PlayMacro(name)`, `AbortMacro()` | the profile's macros (Macros page), played once; one script macro at a time |
| `SetBacklightColor(r, g, b[, family])` | whole-keyboard colour on a keyboard running a host profile, kept when the keyboard wakes from idle; not saved to the keyboard |
| `SetMouseDPITable(t[, i])`, `SetMouseDPITableIndex(i)` | the mouse running a host profile; DPI buttons then step through the script's table |
| `EnablePrimaryMouseButtonEvents(on)` | as documented |
| `GetMKeyState`, `SetMKeyState` | kept by the script host (no M-keys here) |
| `OutputLCDMessage`, `ClearLCD` | text goes to the script log |
| `SetSteeringWheelProperty`, G13 mouse speed functions | ignored, with one note in the log |

### Trying scripts without running them

The Script page's *Try it* box and `logi-ghub-cli script run` feed events to a
script on a simulated clock and list what it would do. `Sleep` takes no real
time, `IsMouseButtonPressed` follows the timed events, and nothing is typed:

```sh
logi-ghub-cli script check rapid-fire.lua
logi-ghub-cli script run rapid-fire.lua -e MOUSE_BUTTON_PRESSED:5@0 -e MOUSE_BUTTON_RELEASED:5@300
logi-ghub-cli -d mouse profile script Gaming --set rapid-fire.lua
logi-ghub-cli -d mouse profile activate Gaming
logi-ghub-cli -d mouse profile log Gaming --follow
```

## Not possible / not done

* Scripts can't run without the service: there is no on-board equivalent, in
  G HUB either.
* The real pointer position (`GetMousePosition`) isn't readable on Wayland.
* Taking over the G915 X's lighting (a script colour or any host-mode lighting)
  makes it forget a custom animation chosen with brightness + 8/9. After the
  hand-back it shows the active on-board profile's own lighting; press
  brightness + 8/9 again. The keyboard doesn't report the chosen slot, so this
  waits for the on-board profile write command from a G HUB capture.
* G HUB queues a second `PlayMacro` of the macro that is already playing; here
  it is ignored.
* The G915 X can raise script events once keyboard key assignments (host-mode
  key grab) are merged: a key could then be assigned to "send G-key n to the
  script".
