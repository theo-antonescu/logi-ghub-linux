"""logi-ghub-tray: a panel icon with battery levels, profile switching and low-battery alerts.

It speaks the StatusNotifierItem and com.canonical.dbusmenu D-Bus protocols directly
(what Ubuntu's AppIndicator extension, KDE and most other panels show), so it needs
nothing beyond PyGObject's Gio.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from . import profiles  # noqa: E402
from .device import find_devices  # noqa: E402

log = logging.getLogger("logi-ghub-tray")

TRAY_SW_ID = 0x0D  # distinct from the app (0x0B) and the daemon (0x0C)
LOW_BATTERY = 15
POLL_SECONDS = 60

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
    <signal name="NewToolTip"/>
    <signal name="NewIcon"/>
    <signal name="NewStatus"><arg type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
      <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/>
    </method>
    <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
    <method name="AboutToShowGroup">
      <arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/>
    </method>
    <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
    <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
  </interface>
</node>
"""


class MenuItem:
    def __init__(self, label: str = "", action=None, *, enabled: bool = True, separator: bool = False,
                 radio: bool | None = None, check: bool | None = None, children: list["MenuItem"] | None = None):
        self.label, self.action, self.enabled, self.separator = label, action, enabled, separator
        self.radio, self.check = radio, check
        self.children = children or []
        self.id = 0

    def props(self) -> dict[str, GLib.Variant]:
        if self.separator:
            return {"type": GLib.Variant("s", "separator")}
        p = {"label": GLib.Variant("s", self.label.replace("_", "__")), "enabled": GLib.Variant("b", self.enabled)}
        if self.radio is not None:
            p["toggle-type"] = GLib.Variant("s", "radio")
            p["toggle-state"] = GLib.Variant("i", 1 if self.radio else 0)
        elif self.check is not None:
            p["toggle-type"] = GLib.Variant("s", "checkmark")
            p["toggle-state"] = GLib.Variant("i", 1 if self.check else 0)
        if self.children:
            p["children-display"] = GLib.Variant("s", "submenu")
        return p


class DeviceState:
    """What the tray knows about one device (refreshed in the background)."""

    def __init__(self, key: str, name: str, kind: str):
        self.key, self.name, self.kind = key, name, kind
        self.online = False
        self.battery: int | None = None
        self.charging = False
        self.slots: list[tuple[int, str]] = []  # (slot, label)
        self.active_slot = 0
        self.warned_low = False
        self.host_ok = True  # host mode needs HID++ (not the G600)


class Tray:
    def __init__(self, fake: bool = False):
        self.fake = fake
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.states: dict[str, DeviceState] = {}
        self.lock = threading.Lock()
        self.revision = 1
        self.items: dict[int, MenuItem] = {}
        self.root = MenuItem("root")
        self.loop = GLib.MainLoop()
        sni_info = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self.bus.register_object("/StatusNotifierItem", sni_info, self._sni_call, self._sni_get, None)
        self.bus.register_object("/MenuBar", menu_info, self._menu_call, self._menu_get, None)
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        Gio.bus_own_name_on_connection(self.bus, self.name, Gio.BusNameOwnerFlags.NONE, self._owned, None)
        self.rebuild()

    # -- registration -----------------------------------------------------------------
    def _owned(self, *_):
        try:
            self.bus.call_sync("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher",
                               "RegisterStatusNotifierItem", GLib.Variant("(s)", (self.name,)), None,
                               Gio.DBusCallFlags.NONE, 2000, None)
        except GLib.Error as e:
            log.warning("no panel to show the icon in (is the AppIndicator extension enabled?): %s", e.message)

    # -- StatusNotifierItem -----------------------------------------------------------
    def tooltip_text(self) -> str:
        lines = []
        for s in self.states.values():
            if not s.online:
                lines.append(f"{s.name}: asleep")
            elif s.battery is not None:
                lines.append(f"{s.name}: {s.battery}%" + (" (charging)" if s.charging else ""))
            else:
                lines.append(s.name)
        return "\n".join(lines) or "No Logitech devices"

    def _sni_get(self, _conn, _sender, _path, _iface, prop):
        values = {
            "Category": GLib.Variant("s", "Hardware"),
            "Id": GLib.Variant("s", "logi-ghub-linux"),
            "Title": GLib.Variant("s", "Logitech G devices"),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", self.icon_name()),
            "IconThemePath": GLib.Variant("s", ""),
            "ItemIsMenu": GLib.Variant("b", True),
            "Menu": GLib.Variant("o", "/MenuBar"),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], "Logitech G devices", self.tooltip_text())),
        }
        return values.get(prop)

    def icon_name(self) -> str:
        levels = [s.battery for s in self.states.values() if s.online and s.battery is not None and not s.charging]
        if levels and min(levels) <= LOW_BATTERY:
            return "battery-caution-symbolic"
        return "input-gaming-symbolic"

    def _sni_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            self.open_app()
        invocation.return_value(None)

    # -- dbusmenu ------------------------------------------------------------------------
    def _menu_get(self, _conn, _sender, _path, _iface, prop):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(prop)

    def _layout(self, item: MenuItem, depth: int) -> GLib.Variant:
        children = []
        if depth != 0:
            children = [self._layout(c, depth - 1) for c in item.children]
        return GLib.Variant("(ia{sv}av)", (item.id, item.props() if item is not self.root else
                                           {"children-display": GLib.Variant("s", "submenu")}, children))

    def _menu_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method == "GetLayout":
            parent, depth, _props = params.unpack()
            item = self.items.get(parent, self.root)
            invocation.return_value(GLib.Variant.new_tuple(GLib.Variant("u", self.revision), self._layout(item, depth)))
        elif method == "GetGroupProperties":
            ids, _props = params.unpack()
            out = [(i, self.items[i].props()) for i in ids if i in self.items]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (out,)))
        elif method == "GetProperty":
            i, name = params.unpack()
            props = self.items[i].props() if i in self.items else {}
            invocation.return_value(GLib.Variant("(v)", (props.get(name, GLib.Variant("s", "")),)))
        elif method == "Event":
            i, event, _data, _ts = params.unpack()
            if event == "clicked":
                self.click(i)
            invocation.return_value(None)
        elif method == "EventGroup":
            for i, event, _data, _ts in params.unpack()[0]:
                if event == "clicked":
                    self.click(i)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    def click(self, item_id: int) -> None:
        item = self.items.get(item_id)
        if item and item.action:
            GLib.idle_add(lambda: item.action() and False)

    # -- menu contents -------------------------------------------------------------------
    def build_menu(self) -> MenuItem:
        root = MenuItem("root")
        active = profiles.get_active()
        for s in self.states.values():
            status = "asleep" if not s.online else (f"{s.battery}%" if s.battery is not None else "connected")
            if s.charging:
                status += ", charging"
            sub = []
            if s.slots:
                sub.append(MenuItem("On-board profile", enabled=False))
                for slot, label in s.slots:
                    sub.append(MenuItem(label, lambda k=s.key, n=slot: self.select_slot(k, n),
                                        radio=(slot == s.active_slot and not active.get(s.key)), enabled=s.online))
            host = profiles.list_profiles(s.key) if s.host_ok else []
            if host:
                if sub:
                    sub.append(MenuItem(separator=True))
                sub.append(MenuItem("Run on this computer (host mode)", enabled=False))
                sub.append(MenuItem("Off", lambda k=s.key: self.set_host(k, None), radio=not active.get(s.key)))
                for p in host:
                    sub.append(MenuItem(p.name, lambda k=s.key, n=p.name: self.set_host(k, n), radio=active.get(s.key) == p.name))
            root.children.append(MenuItem(f"{s.name} — {status}", children=sub, enabled=bool(sub)))
        if not self.states:
            root.children.append(MenuItem("No Logitech devices found", enabled=False))
        root.children.append(MenuItem(separator=True))
        root.children.append(MenuItem("Open Logitech G devices", self.open_app))
        root.children.append(MenuItem("Refresh", lambda: threading.Thread(target=self.refresh, daemon=True).start()))
        root.children.append(MenuItem("Quit", lambda: self.loop.quit()))
        return root

    def rebuild(self) -> None:
        with self.lock:
            self.root = self.build_menu()
        self.items = {}
        counter = [1]

        def number(item: MenuItem):
            for c in item.children:
                c.id = counter[0]
                counter[0] += 1
                self.items[c.id] = c
                number(c)

        self.root.id = 0
        self.items[0] = self.root
        number(self.root)
        self.revision += 1
        self.bus.emit_signal(None, "/MenuBar", "com.canonical.dbusmenu", "LayoutUpdated", GLib.Variant("(ui)", (self.revision, 0)))
        self.bus.emit_signal(None, "/StatusNotifierItem", "org.kde.StatusNotifierItem", "NewToolTip", None)
        self.bus.emit_signal(None, "/StatusNotifierItem", "org.kde.StatusNotifierItem", "NewIcon", None)

    # -- device polling (background thread) -------------------------------------------------
    def refresh(self) -> None:
        if self.fake:
            from .hidpp.fake import fake_devices

            devs = fake_devices()
        else:
            devs = find_devices()
        seen = set()
        for d in devs:
            if d.hidpp is not None:
                d.hidpp.swid = TRAY_SW_ID
            st = self.states.get(d.key) or DeviceState(d.key, d.name, d.kind)
            st.name, st.kind, st.online = d.name, d.kind, d.online
            st.host_ok = d.hidpp is not None
            seen.add(d.key)
            if d.online:
                try:
                    b = d.battery()
                    st.battery = b.percent if b else None
                    st.charging = bool(b and b.charging in ("charging", "charging slowly"))
                    if d.onboard:
                        slots = d.onboard.read_profiles()
                        st.slots = [(p.sector, getattr(p, "display_name", "") or f"Slot {p.sector}") for p in slots if p.enabled]
                        st.active_slot = d.onboard.get_current_profile()
                except Exception as e:  # noqa: BLE001 - devices fall asleep mid-read
                    log.debug("%s: %s", d.name, e)
            self.states[d.key] = st
            d.close()
        for key in list(self.states):
            if key not in seen:
                del self.states[key]
        self.check_low_battery()
        GLib.idle_add(lambda: self.rebuild() and False)

    def check_low_battery(self) -> None:
        for s in self.states.values():
            if s.battery is None or not s.online:
                continue
            if s.battery <= LOW_BATTERY and not s.charging and not s.warned_low:
                s.warned_low = True
                notify(f"{s.name} battery low", f"{s.battery}% left. Plug it in soon.")
            elif s.battery > LOW_BATTERY + 5 or s.charging:
                s.warned_low = False

    def poll_forever(self) -> None:
        while True:
            try:
                self.refresh()
            except Exception:  # noqa: BLE001
                log.exception("refresh failed")
            time.sleep(POLL_SECONDS)

    # -- actions ---------------------------------------------------------------------------
    def select_slot(self, key: str, slot: int) -> None:
        def work():
            devs = find_devices()
            try:
                for d in devs:
                    if d.key == key and d.online and d.onboard:
                        d.onboard.set_mode(1)
                        d.onboard.set_current_profile(slot)
            finally:
                for d in devs:
                    d.close()
            if profiles.get_active().get(key):
                profiles.set_active(key, None)  # an on-board profile and a host profile can't both run
            self.refresh()

        threading.Thread(target=work, daemon=True).start()

    def set_host(self, key: str, name: str | None) -> None:
        profiles.set_active(key, name)
        if name and not service_running():
            notify("logi-ghubd is not running", "Host-mode profiles need it: systemctl --user enable --now logi-ghubd")
        self.rebuild()

    def open_app(self) -> None:
        exe = os.path.join(os.path.dirname(sys.executable), "logi-ghub")
        cmd = [exe] if os.path.exists(exe) else [sys.executable, "-m", "logi_ghub.gui.app"]
        subprocess.Popen(cmd, start_new_session=True)

    def run(self) -> None:
        threading.Thread(target=self.poll_forever, daemon=True).start()
        self.loop.run()


def service_running() -> bool:
    try:
        return subprocess.run(["systemctl", "--user", "is-active", "--quiet", "logi-ghubd"], timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def notify(title: str, body: str) -> None:
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        bus.call_sync("org.freedesktop.Notifications", "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
                      "Notify", GLib.Variant("(susssasa{sv}i)", ("Logitech G devices", 0, "input-gaming", title, body, [], {}, -1)),
                      None, Gio.DBusCallFlags.NONE, 2000, None)
    except GLib.Error as e:
        log.info("notification failed: %s", e.message)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="logi-ghub-tray")
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    if args.fake:
        from .config import use_sandbox

        use_sandbox()
    Tray(fake=args.fake).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
