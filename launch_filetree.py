#!/usr/bin/env python3
"""
launch_filetree.py — Lanzador del árbol de ficheros con sincronización de tabs.

El script se daemoniza al instante: el prompt vuelve y el árbol aparece
en un split pane izquierdo sin output visible en el terminal.

Requisitos:
    pip3 install iterm2
    iTerm2: Settings → General → Magic → Enable Python API

Uso:
    python3 launch_filetree.py
"""

import iterm2
import asyncio
import os
import sys
import signal
import time as _time
from pathlib import Path

PIPE_PATH       = "/tmp/filetree_sync.pipe"
DAEMON_LOG      = "/tmp/filetree_daemon.log"
PID_FILE        = "/tmp/filetree_daemon.pid"
FILETREE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "filetree.py")
PANEL_WIDTH_COLS = 36


# ── PID lock — evita múltiples daemons simultáneos ────────────────────────────

def kill_existing_daemon():
    """Mata TODOS los procesos launch_filetree.py anteriores."""
    import subprocess
    my_pid = os.getpid()
    try:
        result = subprocess.run(
            ["pgrep", "-f", "launch_filetree.py"],
            capture_output=True, text=True
        )
        pids = [int(p) for p in result.stdout.strip().split() if p.strip()]
        killed = 0
        for pid in pids:
            if pid != my_pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed += 1
                except (ProcessLookupError, PermissionError):
                    pass
        if killed:
            _time.sleep(0.3)
    except Exception:
        pass
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def write_pid_file():
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def remove_pid_file():
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


# ── Daemonización ─────────────────────────────────────────────────────────────

def daemonize():
    """Double-fork: desacopla el proceso del terminal y devuelve el prompt."""
    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    os.setsid()

    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    # Redirigir stdio al log
    sys.stdout.flush()
    sys.stderr.flush()
    devnull_fd = os.open(os.devnull, os.O_RDONLY)
    log_fd = os.open(DAEMON_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    for target_fd in (0, 1, 2):
        try:
            src = devnull_fd if target_fd == 0 else log_fd
            os.dup2(src, target_fd)
        except OSError:
            pass
    os.close(devnull_fd)
    os.close(log_fd)

    write_pid_file()
    signal.signal(signal.SIGTERM, lambda *_: (remove_pid_file(), sys.exit(0)))


# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = _time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def write_to_pipe(path: str, source: str = "?"):
    log(f"PIPE ← {source}: {path}")
    try:
        fd = os.open(PIPE_PATH, os.O_WRONLY | os.O_NONBLOCK)
        os.write(fd, (path + "\n").encode())
        os.close(fd)
    except OSError:
        pass


async def get_session_cwd(session) -> str | None:
    for var in ("user.path", "path", "currentDirectory"):
        try:
            cwd = await session.async_get_variable(var)
            if cwd and Path(cwd).is_dir():
                return cwd
        except Exception:
            continue
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(connection):
    app = await iterm2.async_get_app(connection)
    window = app.current_terminal_window

    if window is None:
        log("ERROR: no hay ventana iTerm2 activa")
        return

    current_session = window.current_tab.current_session
    start_dir = await get_session_cwd(current_session) or os.getcwd()
    log(f"start_dir={start_dir}  session={current_session.session_id}")

    # ── Split pane izquierdo ───────────────────────────────────────────────
    tree_session = await current_session.async_split_pane(vertical=True, before=True)
    tree_session_id = tree_session.session_id
    log(f"tree_session_id={tree_session_id}")

    current_size = tree_session.grid_size
    await tree_session.async_set_grid_size(iterm2.Size(PANEL_WIDTH_COLS, current_size.height))
    await tree_session.async_send_text(f"python3 '{FILETREE_SCRIPT}' '{start_dir}'\n")
    await current_session.async_activate()

    log(f"daemon ready")

    HOME = str(Path.home())
    excluded = {tree_session_id}
    last_path = start_dir          # último path "de trabajo" enviado al árbol

    # ── Watcher de foco entre paneles/tabs ────────────────────────────────
    # Actualiza el árbol cuando el usuario cambia de panel.
    # Regla anti-ruido: si el CWD de la sesión enfocada es $HOME pero el
    # último path conocido no lo era, se ignora (evita que Claude Code u
    # otras sesiones estáticas en ~ sobreescriban el directorio de trabajo).
    async def sync_session(session, source: str):
        """Lee el CWD de una sesión y lo envía al árbol.
        Si el primer intento devuelve HOME, reintenta tras 400 ms para dar
        tiempo a que shell integration actualice la variable path."""
        nonlocal last_path
        sid = session.session_id
        cwd = await get_session_cwd(session)
        log(f"{source} sid={sid[:8]} cwd={cwd}")
        if not cwd:
            return
        if cwd == HOME and last_path != HOME:
            log(f"{source} HOME→retry sid={sid[:8]}")
            await asyncio.sleep(0.4)
            # Verificar que esta sesión siga siendo la activa
            win = app.current_terminal_window
            if not win:
                return
            active = win.current_tab.current_session
            if not active or active.session_id != sid or sid in excluded:
                return
            cwd = await get_session_cwd(session)
            log(f"{source} retry sid={sid[:8]} cwd={cwd}")
            if not cwd or (cwd == HOME and last_path != HOME):
                log(f"{source} still HOME, skip sid={sid[:8]}")
                return
        last_path = cwd
        write_to_pipe(cwd, source)

    async def on_focus_change(focus_event):
        try:
            sid = focus_event.session_id
            if not sid or sid in excluded:
                log(f"FOCUS skip sid={sid[:8] if sid else 'none'}")
                return
            for w in app.terminal_windows:
                for tab in w.tabs:
                    for session in tab.sessions:
                        if session.session_id == sid:
                            await sync_session(session, f"focus:{sid[:8]}")
                            return
        except Exception as e:
            log(f"FOCUS ERROR: {e}")

    # ── Watcher de cd dentro de sesión ────────────────────────────────────
    # El primer valor de VariableMonitor es el CWD actual (no un cambio real),
    # se descarta para evitar escrituras espurias al inicio.
    watched = set()

    async def watch_session_path(session):
        if session.session_id in watched or session.session_id in excluded:
            return
        watched.add(session.session_id)
        log(f"watching cd for sid={session.session_id[:8]}")

        async with iterm2.VariableMonitor(
            connection,
            iterm2.VariableScopes.SESSION,
            "path",
            session.session_id
        ) as mon:
            skip_initial = True   # descartar el valor actual al suscribirse
            while True:
                try:
                    value = await mon.async_get()
                    if skip_initial:
                        skip_initial = False
                        log(f"skip initial cwd={value} sid={session.session_id[:8]}")
                        continue
                    if not value or not Path(value).is_dir():
                        continue
                    if session.session_id in excluded:
                        break
                    win = app.current_terminal_window
                    if not win:
                        continue
                    active = win.current_tab.current_session
                    if (active
                            and active.session_id == session.session_id
                            and active.session_id not in excluded):
                        write_to_pipe(value, f"cd:{session.session_id[:8]}")
                except Exception:
                    break
        watched.discard(session.session_id)
        log(f"stopped watching sid={session.session_id[:8]}")

    for w in app.terminal_windows:
        for tab in w.tabs:
            for session in tab.sessions:
                asyncio.ensure_future(watch_session_path(session))

    async def on_new_session(new_session_event):
        sid = new_session_event.session_id
        for w in app.terminal_windows:
            for tab in w.tabs:
                for session in tab.sessions:
                    if session.session_id == sid:
                        asyncio.ensure_future(watch_session_path(session))
                        return

    async with iterm2.FocusMonitor(connection) as focus_mon:
        asyncio.ensure_future(_watch_new_sessions(connection, on_new_session))
        while True:
            update = await focus_mon.async_get_next_update()
            if update.active_session_changed:
                await on_focus_change(update.active_session_changed)


async def _watch_new_sessions(connection, callback):
    """Escucha nuevas sesiones y arranca su watcher de cd."""
    async with iterm2.NewSessionMonitor(connection) as mon:
        while True:
            session_id = await mon.async_get()

            class _Ev:
                pass
            e = _Ev()
            e.session_id = session_id
            await callback(e)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    kill_existing_daemon()   # matar daemon anterior antes de forkear
    daemonize()
    iterm2.run_until_complete(main)
