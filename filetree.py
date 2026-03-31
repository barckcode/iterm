#!/usr/bin/env python3
"""
filetree.py — iTerm2 File Tree Navigator con sincronización automática de tabs.

El árbol se actualiza automáticamente cuando:
    - Cambias de pestaña en iTerm2
    - Haces cd en cualquier sesión

Comunicación interna: FIFO en /tmp/filetree_sync.pipe
El watcher (launch_filetree.py) escribe en ese pipe cada vez que el directorio cambia.

Controles:
    j / ↓            Bajar
    k / ↑            Subir
    Enter / → / l    Expandir carpeta / abrir fichero
    ← / h            Colapsar / subir al padre
    y                Copiar path absoluto al portapapeles
    Y                Copiar path relativo
    /                Buscar (filtrar por nombre)
    Esc              Salir búsqueda
    H                Toggle archivos ocultos
    r                Recargar árbol manualmente
    q                Salir

    Click            Seleccionar / expandir
    Doble click      Expandir carpeta / abrir fichero
    Click derecho    Menú contextual
"""

import curses
import os
import sys
import subprocess
import signal
import time
import threading
import stat
from pathlib import Path

PIPE_PATH = "/tmp/filetree_sync.pipe"

# ── Paleta ────────────────────────────────────────────────────────────────────
COLOR_PAIRS = {
    "normal":          (1,  250,  -1),
    "dir_open":        (2,  curses.COLOR_CYAN, -1),
    "dir_closed":      (3,  75,   -1),
    "selected":        (4,  curses.COLOR_WHITE, 236),
    "selected_dir":    (5,  curses.COLOR_CYAN,  236),
    "header":          (6,  curses.COLOR_WHITE, 234),
    "status":          (7,  244,  234),
    "search_label":    (8,  214,  234),
    "search_text":     (9,  curses.COLOR_WHITE, 234),
    "menu_normal":     (10, curses.COLOR_WHITE, 235),
    "menu_select":     (11, 232,  214),
    "menu_border":     (12, 238,  235),
    "dot_file":        (13, 239,  -1),
    "symlink":         (14, curses.COLOR_MAGENTA, -1),
    "path_cwd":        (17, 214,  234),
    "count":           (19, 239,  -1),
    "selected_count":  (20, 239,  236),
    "sync_ok":         (21, 114,  234),
    "syncing":         (22, 214,  234),
}

ICONS = {
    "dir_open":   "▾ ",
    "dir_closed": "▸ ",
    "file":       "  ",
    "symlink":    "⇢ ",
    "root":       "⌂ ",
}

CONTEXT_MENU_OPTIONS = [
    ("y", "Copiar path absoluto",  "copy_path"),
    ("Y", "Copiar path relativo",  "copy_rel_path"),
    ("n", "Copiar nombre",         "copy_name"),
    ("o", "Abrir en Finder",       "open_finder"),
    ("─", None,                    None),
    ("d", "CD a este directorio",  "cd_here"),
    ("r", "Recargar árbol",        "reload"),
]


# ── Nodo ──────────────────────────────────────────────────────────────────────

class FileNode:
    __slots__ = ("path", "name", "is_dir", "is_symlink", "is_expanded",
                 "children", "depth", "parent")

    def __init__(self, path: Path, depth: int = 0, parent=None):
        self.path = path
        self.name = path.name or str(path)
        self.is_dir = path.is_dir()
        self.is_symlink = path.is_symlink()
        self.is_expanded = False
        self.children = []
        self.depth = depth
        self.parent = parent

    def load_children(self):
        if not self.is_dir:
            return
        try:
            entries = sorted(
                self.path.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower())
            )
        except PermissionError:
            self.children = []
            return
        self.children = [FileNode(e, self.depth + 1, self) for e in entries]

    def toggle(self):
        if not self.is_dir:
            return
        self.is_expanded = not self.is_expanded
        if self.is_expanded and not self.children:
            self.load_children()

    @property
    def is_hidden(self):
        return self.name.startswith(".")

    def flat_visible(self):
        result = [self]
        if self.is_dir and self.is_expanded:
            for child in self.children:
                result.extend(child.flat_visible())
        return result


# ── Menú contextual ───────────────────────────────────────────────────────────

class ContextMenu:
    def __init__(self, options, y, x, max_h, max_w):
        self.options = options
        self.selected = 0
        while self.selected < len(self.options) and self.options[self.selected][2] is None:
            self.selected += 1

        width = max((len(label) + 6 for _, label, _ in options if label), default=20) + 2
        width = min(width, max_w - x - 2)
        height = len(options) + 2

        if y + height > max_h:
            y = max_h - height - 1
        if x + width > max_w:
            x = max_w - width - 1

        self.win = curses.newwin(height, width, max(0, y), max(0, x))
        self.win.keypad(True)
        self.width = width

    def draw(self, pairs):
        self.win.erase()
        self.win.attron(pairs["menu_border"])
        self.win.border()
        self.win.attroff(pairs["menu_border"])
        for i, (key, label, action) in enumerate(self.options):
            row = i + 1
            if action is None:
                self.win.attron(pairs["menu_border"])
                try:
                    self.win.addstr(row, 1, "─" * (self.width - 2))
                except curses.error:
                    pass
                self.win.attroff(pairs["menu_border"])
                continue
            attr = pairs["menu_select"] if i == self.selected else pairs["menu_normal"]
            self.win.attron(attr)
            try:
                self.win.addstr(row, 1, f" {key}  {label:<{self.width - 7}}")
            except curses.error:
                pass
            self.win.attroff(attr)
        self.win.refresh()

    def navigate(self, direction):
        step = 1 if direction > 0 else -1
        idx = self.selected + step
        while 0 <= idx < len(self.options):
            if self.options[idx][2] is not None:
                self.selected = idx
                return
            idx += step

    def handle_key(self, key):
        if key in (curses.KEY_DOWN, ord("j")):
            self.navigate(1)
        elif key in (curses.KEY_UP, ord("k")):
            self.navigate(-1)
        elif key in (curses.KEY_ENTER, ord("\n"), ord("\r")):
            return self.options[self.selected][2]
        elif key == 27:
            return "cancel"
        for shortcut, _, action in self.options:
            if action and key == ord(shortcut):
                return action
        return None


# ── App ───────────────────────────────────────────────────────────────────────

class FileTreeApp:

    def __init__(self, root_path: Path):
        self.root_path = root_path.resolve()
        self.root = self._build_root(self.root_path)

        self.flat = []
        self.cursor = 0
        self.scroll_offset = 0
        self.search_mode = False
        self.search_query = ""
        self.status_msg = ""
        self.status_time = 0.0
        self.show_hidden = False
        self.pairs = {}

        self._sync_lock = threading.Lock()
        self._pending_path = None
        self._is_syncing = False
        self._last_synced_path = None

        self._ensure_pipe()
        threading.Thread(target=self._pipe_reader, daemon=True).start()

    # ── Setup ──────────────────────────────────────────────────────────────

    def _build_root(self, path: Path) -> FileNode:
        node = FileNode(path, depth=0)
        node.load_children()
        node.is_expanded = True
        return node

    def _ensure_pipe(self):
        if os.path.exists(PIPE_PATH):
            if not stat.S_ISFIFO(os.stat(PIPE_PATH).st_mode):
                os.remove(PIPE_PATH)
                os.mkfifo(PIPE_PATH)
        else:
            os.mkfifo(PIPE_PATH)

    def _pipe_reader(self):
        """Hilo daemon: escucha el FIFO y encola paths nuevos."""
        while True:
            try:
                with open(PIPE_PATH, "r") as pipe:
                    for line in pipe:
                        path_str = line.strip()
                        if not path_str:
                            continue
                        new_path = Path(path_str).resolve()
                        if new_path.is_dir() and new_path != self._last_synced_path:
                            with self._sync_lock:
                                self._pending_path = new_path
                                self._is_syncing = True
            except Exception:
                time.sleep(0.5)

    # ── Sync ───────────────────────────────────────────────────────────────

    def check_and_apply_sync(self) -> bool:
        with self._sync_lock:
            pending = self._pending_path
            self._pending_path = None

        if pending is None:
            return False

        self._load_new_root(pending)
        with self._sync_lock:
            self._is_syncing = False
        self._last_synced_path = pending
        name = pending.name or str(pending)
        self.set_status(f"↻  {name}", duration=2.0)
        return True

    def _load_new_root(self, new_path: Path):
        old_cursor_path = self.flat[self.cursor].path if self.flat else None
        self.root_path = new_path
        self.root = self._build_root(new_path)
        self.rebuild_flat()
        if old_cursor_path:
            for i, node in enumerate(self.flat):
                if node.path == old_cursor_path:
                    self.cursor = i
                    return
        self.cursor = 0
        self.scroll_offset = 0

    def reload_tree(self):
        self._load_new_root(self.root_path)
        self.set_status("Árbol recargado")

    # ── Helpers ────────────────────────────────────────────────────────────

    def init_colors(self):
        curses.start_color()
        curses.use_default_colors()
        for name, (pair_id, fg, bg) in COLOR_PAIRS.items():
            try:
                curses.init_pair(pair_id, fg, bg)
                self.pairs[name] = curses.color_pair(pair_id)
            except Exception:
                self.pairs[name] = curses.A_NORMAL

    def set_status(self, msg: str, duration: float = 2.5):
        self.status_msg = msg
        self.status_time = time.time() + duration

    def copy_to_clipboard(self, text: str) -> bool:
        try:
            subprocess.run(["pbcopy"], input=text.encode(), check=True,
                           capture_output=True)
            return True
        except Exception:
            try:
                subprocess.run(["xclip", "-selection", "clipboard"],
                               input=text.encode(), check=True,
                               capture_output=True)
                return True
            except Exception:
                return False

    def open_in_finder(self, path: Path):
        subprocess.Popen(["open", str(path if path.is_dir() else path.parent)])

    # ── Árbol ──────────────────────────────────────────────────────────────

    def rebuild_flat(self):
        raw = self.root.flat_visible()
        if not self.show_hidden:
            raw = [n for n in raw if not n.is_hidden or n is self.root]
        if self.search_query:
            q = self.search_query.lower()
            raw = [n for n in raw if q in n.name.lower()]
        self.flat = raw
        if self.cursor >= len(self.flat):
            self.cursor = max(0, len(self.flat) - 1)

    # ── Render ─────────────────────────────────────────────────────────────

    def get_node_attr(self, node: FileNode, selected: bool) -> int:
        p = self.pairs
        if selected:
            return p["selected_dir"] if node.is_dir else p["selected"]
        if node.is_symlink:
            return p["symlink"]
        if node.is_hidden:
            return p["dot_file"]
        if node.is_dir:
            return p["dir_open"] if node.is_expanded else p["dir_closed"]
        return p["normal"]

    def render_node_line(self, win, row: int, node: FileNode,
                         selected: bool, max_w: int):
        if node is self.root:
            icon = ICONS["root"]
        elif node.is_symlink:
            icon = ICONS["symlink"]
        elif node.is_dir:
            icon = ICONS["dir_open"] if node.is_expanded else ICONS["dir_closed"]
        else:
            icon = ICONS["file"]

        indent = "  " * node.depth
        prefix = indent + icon
        name = node.name
        attr = self.get_node_attr(node, selected)

        if selected:
            win.attron(attr)
            try:
                win.addstr(row, 0, " " * max_w)
            except curses.error:
                pass
            win.attroff(attr)

        available = max_w - len(prefix) - 1
        if available < 3:
            return
        display = name if len(name) <= available else name[:available - 1] + "…"

        win.attron(attr)
        try:
            win.addstr(row, 0, prefix + display)
        except curses.error:
            pass

        if node.is_dir and node.is_expanded and node.children:
            count_str = f" {len(node.children)}"
            cx = len(prefix) + len(display)
            if cx + len(count_str) < max_w - 1:
                win.attroff(attr)
                ca = self.pairs["selected_count"] if selected else self.pairs["count"]
                win.attron(ca)
                try:
                    win.addstr(row, cx, count_str)
                except curses.error:
                    pass
                win.attroff(ca)
                return

        win.attroff(attr)

    def draw_header(self, win, max_w: int):
        win.attron(self.pairs["header"])
        try:
            win.addstr(0, 0, " " * max_w)
        except curses.error:
            pass
        win.attroff(self.pairs["header"])

        with self._sync_lock:
            syncing = self._is_syncing

        icon = " ↻ " if syncing else " ✦ "
        icon_attr = self.pairs["syncing"] if syncing else self.pairs["sync_ok"]
        win.attron(icon_attr)
        try:
            win.addstr(0, 0, icon)
        except curses.error:
            pass
        win.attroff(icon_attr)

        title = "TREE "
        win.attron(self.pairs["header"])
        try:
            win.addstr(0, len(icon), title)
        except curses.error:
            pass
        win.attroff(self.pairs["header"])

        if self.flat:
            node = self.flat[self.cursor]
            try:
                rel = node.path.relative_to(self.root_path)
                path_str = f"/{rel}" if str(rel) != "." else "/"
            except ValueError:
                path_str = str(node.path)

            prefix_len = len(icon) + len(title)
            avail = max_w - prefix_len - 2
            if len(path_str) > avail:
                path_str = "…" + path_str[-(avail - 1):]
            win.attron(self.pairs["path_cwd"])
            try:
                win.addstr(0, prefix_len, path_str)
            except curses.error:
                pass
            win.attroff(self.pairs["path_cwd"])

    def draw_status(self, win, max_h: int, max_w: int):
        row = max_h - 1
        win.attron(self.pairs["status"])
        try:
            win.addstr(row, 0, " " * max_w)
        except curses.error:
            pass

        if self.search_mode:
            label = " / "
            win.attron(self.pairs["search_label"])
            try:
                win.addstr(row, 0, label)
            except curses.error:
                pass
            win.attroff(self.pairs["search_label"])
            win.attron(self.pairs["search_text"])
            try:
                win.addstr(row, len(label),
                           (self.search_query + "█")[:max_w - len(label) - 1])
            except curses.error:
                pass
            win.attroff(self.pairs["search_text"])
        elif self.status_msg and time.time() < self.status_time:
            win.attron(self.pairs["search_label"])
            try:
                win.addstr(row, 0, f" {self.status_msg}"[:max_w - 1])
            except curses.error:
                pass
            win.attroff(self.pairs["search_label"])
        else:
            win.attron(self.pairs["status"])
            try:
                win.addstr(row, 0,
                           " y:copy  /:find  H:hidden  r:reload  q:quit"[:max_w - 1])
            except curses.error:
                pass
            win.attroff(self.pairs["status"])

    def draw_tree(self, win, max_h: int, max_w: int):
        tree_h = max_h - 2
        if self.cursor < self.scroll_offset:
            self.scroll_offset = self.cursor
        elif self.cursor >= self.scroll_offset + tree_h:
            self.scroll_offset = self.cursor - tree_h + 1

        for i in range(tree_h):
            idx = self.scroll_offset + i
            row = i + 1
            if idx >= len(self.flat):
                try:
                    win.addstr(row, 0, " " * max_w)
                except curses.error:
                    pass
                continue
            self.render_node_line(win, row, self.flat[idx], idx == self.cursor, max_w)

    def draw(self, win, stdscr):
        max_h, max_w = stdscr.getmaxyx()
        self.rebuild_flat()
        win.erase()
        self.draw_tree(win, max_h, max_w)
        self.draw_header(win, max_w)
        self.draw_status(win, max_h, max_w)
        win.refresh()

    # ── Menú contextual ────────────────────────────────────────────────────

    def show_context_menu(self, win, stdscr, node: FileNode):
        max_h, max_w = stdscr.getmaxyx()
        menu_row = (self.cursor - self.scroll_offset) + 1
        menu_col = min(node.depth * 2 + 6, max_w // 2)
        menu = ContextMenu(CONTEXT_MENU_OPTIONS, menu_row, menu_col, max_h, max_w)

        while True:
            self.draw(win, stdscr)
            menu.draw(self.pairs)
            key = menu.win.getch()
            action = menu.handle_key(key)
            if not action:
                continue
            if action == "cancel":
                break
            elif action == "copy_path":
                if self.copy_to_clipboard(str(node.path)):
                    self.set_status(f"✓ {node.path}")
                break
            elif action == "copy_rel_path":
                try:
                    text = str(node.path.relative_to(self.root_path))
                except ValueError:
                    text = str(node.path)
                if self.copy_to_clipboard(text):
                    self.set_status(f"✓ Relativo: {text}")
                break
            elif action == "copy_name":
                if self.copy_to_clipboard(node.name):
                    self.set_status(f"✓ Nombre: {node.name}")
                break
            elif action == "open_finder":
                self.open_in_finder(node.path)
                self.set_status("Abierto en Finder")
                break
            elif action == "cd_here":
                target = node.path if node.is_dir else node.path.parent
                print(f"\x1b]1337;CurrentDir={target}\x07", end="", flush=True)
                self.set_status(f"CD → {target}")
                break
            elif action == "reload":
                self.reload_tree()
                break
            else:
                break

    # ── Bucle principal ────────────────────────────────────────────────────

    def run(self, stdscr):
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.nodelay(True)   # <-- no bloqueante para poder hacer polling del pipe
        self.init_colors()
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        print("\033[?1003h", end="", flush=True)

        self.rebuild_flat()
        last_draw = 0.0

        while True:
            now = time.time()
            synced = self.check_and_apply_sync()

            if synced or (now - last_draw) > 0.033:
                self.draw(stdscr, stdscr)
                last_draw = now

            key = stdscr.getch()

            if key == curses.ERR:
                time.sleep(0.02)
                continue

            # ── Búsqueda ───────────────────────────────────────────────────
            if self.search_mode:
                if key == 27:
                    self.search_mode = False
                    self.search_query = ""
                elif key in (curses.KEY_BACKSPACE, 127, 8):
                    self.search_query = self.search_query[:-1]
                elif key == ord("\n"):
                    self.search_mode = False
                elif 32 <= key < 127:
                    self.search_query += chr(key)
                continue

            # ── Keys ───────────────────────────────────────────────────────
            if key in (ord("q"), ord("Q")):
                break
            elif key in (curses.KEY_DOWN, ord("j")):
                if self.cursor < len(self.flat) - 1:
                    self.cursor += 1
            elif key in (curses.KEY_UP, ord("k")):
                if self.cursor > 0:
                    self.cursor -= 1
            elif key == curses.KEY_NPAGE:
                max_h, _ = stdscr.getmaxyx()
                self.cursor = min(len(self.flat) - 1, self.cursor + max_h - 4)
            elif key == curses.KEY_PPAGE:
                max_h, _ = stdscr.getmaxyx()
                self.cursor = max(0, self.cursor - max_h + 4)
            elif key in (curses.KEY_HOME, ord("g")):
                self.cursor = 0
            elif key in (curses.KEY_END, ord("G")):
                self.cursor = len(self.flat) - 1
            elif key in (curses.KEY_ENTER, ord("\n"), ord("\r"),
                         curses.KEY_RIGHT, ord("l")):
                if self.flat:
                    node = self.flat[self.cursor]
                    if node.is_dir:
                        node.toggle()
                    else:
                        subprocess.Popen(["open", str(node.path)])
            elif key in (curses.KEY_LEFT, ord("h")):
                if self.flat:
                    node = self.flat[self.cursor]
                    if node.is_dir and node.is_expanded:
                        node.toggle()
                    elif node.parent and node.parent is not self.root:
                        try:
                            self.cursor = self.flat.index(node.parent)
                        except ValueError:
                            pass
            elif key in (ord("y"), ord("c")):
                if self.flat:
                    if self.copy_to_clipboard(str(self.flat[self.cursor].path)):
                        self.set_status(f"✓ {self.flat[self.cursor].path}")
            elif key == ord("Y"):
                if self.flat:
                    node = self.flat[self.cursor]
                    try:
                        text = str(node.path.relative_to(self.root_path))
                    except ValueError:
                        text = str(node.path)
                    if self.copy_to_clipboard(text):
                        self.set_status(f"✓ Relativo: {text}")
            elif key == ord("/"):
                self.search_mode = True
                self.search_query = ""
            elif key == ord("H"):
                self.show_hidden = not self.show_hidden
                self.set_status("Mostrando ocultos" if self.show_hidden else "Ocultando ocultos")
            elif key == ord("r"):
                self.reload_tree()
            elif key == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                    max_h, max_w = stdscr.getmaxyx()
                    tree_h = max_h - 2

                    # Scroll wheel — BUTTON5_PRESSED no está definido en Python 3.12
                    # macOS ncurses usa BUTTON4=0x80000 (up) y bit 25=0x2000000 (down)
                    _SCROLL_DOWN = getattr(curses, 'BUTTON5_PRESSED', 0) or 0x2000000
                    if bstate & curses.BUTTON4_PRESSED:
                        self.cursor = max(0, self.cursor - 3)
                        self.scroll_offset = max(0, self.scroll_offset - 3)
                        continue
                    elif bstate & _SCROLL_DOWN:
                        self.cursor = min(len(self.flat) - 1, self.cursor + 3)
                        self.scroll_offset = min(
                            max(0, len(self.flat) - tree_h),
                            self.scroll_offset + 3
                        )
                        continue

                    if 1 <= my <= tree_h:
                        clicked_idx = self.scroll_offset + (my - 1)
                        if 0 <= clicked_idx < len(self.flat):
                            node = self.flat[clicked_idx]
                            if bstate & curses.BUTTON1_CLICKED:
                                if clicked_idx == self.cursor and node.is_dir:
                                    node.toggle()
                                else:
                                    self.cursor = clicked_idx
                            elif bstate & curses.BUTTON1_DOUBLE_CLICKED:
                                self.cursor = clicked_idx
                                if node.is_dir:
                                    node.toggle()
                                else:
                                    subprocess.Popen(["open", str(node.path)])
                            elif bstate & curses.BUTTON3_CLICKED:
                                self.cursor = clicked_idx
                                self.show_context_menu(stdscr, stdscr, node)
                except curses.error:
                    pass

        print("\033[?1003l", end="", flush=True)
        curses.mousemask(0)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    if not root.exists():
        print(f"Error: '{root}' no existe.", file=sys.stderr)
        sys.exit(1)
    if not root.is_dir():
        root = root.parent

    app = FileTreeApp(root.resolve())
    signal.signal(signal.SIGWINCH, lambda s, f: None)

    try:
        curses.wrapper(app.run)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
