"""Control one configured llama-server through a standalone Windows tray process."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil


_WINDOWS_ENV = {
    "APPDATA", "COMSPEC", "CUDA_PATH", "CUDA_VISIBLE_DEVICES", "LOCALAPPDATA", "PATH", "PATHEXT",
    "PROGRAMDATA", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "WINDIR",
}


def _environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if name.upper() in _WINDOWS_ENV}


class LlamaTray:
    def __init__(self, home: Path):
        self.directory = home.resolve() / "ai-usage-tracker"
        self.settings_file = self.directory / "llama.json"
        self.error_file = self.directory / "llama-error.txt"
        suffix = hashlib.sha256(os.path.normcase(str(self.directory)).encode()).hexdigest()[:24]
        self.name = "Local\\HermesLlamaTray-" + suffix

    def _settings(self) -> dict:
        config = json.loads(self.settings_file.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("Invalid llama.json")
        executable = Path(config["executable"])
        args, port = config["arguments"], config["port"]
        if not executable.is_absolute() or executable.name.lower() != "llama-server.exe" or not executable.is_file():
            raise ValueError("Set executable to the full path of llama-server.exe")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Set port to an integer from 1 to 65535")
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise ValueError("Set arguments to a list of strings")
        if any(arg.partition("=")[0] in ("--host", "--port", "--api-key", "--api-key-file") for arg in args):
            raise ValueError("The tray sets the loopback host and port")
        env = config.get("environment", {})
        if not isinstance(env, dict) or not all(isinstance(value, str) for value in env.values()):
            raise ValueError("Set environment to string values")
        config["command"] = [str(executable), *args, "--host", "127.0.0.1", "--port", str(port)]
        return config

    def _stop_event(self):
        import pywintypes
        import win32event

        try:
            return win32event.OpenEvent(0x100002, False, self.name + "-stop")  # SYNCHRONIZE | EVENT_MODIFY_STATE
        except pywintypes.error as exc:
            if exc.winerror != 2:  # ERROR_FILE_NOT_FOUND
                raise
            return None

    @staticmethod
    def _listener(config: dict) -> tuple[bool, psutil.Process | None]:
        listeners = [conn for conn in psutil.net_connections(kind="tcp")
                     if conn.status == psutil.CONN_LISTEN and conn.laddr.port == config["port"]]
        for conn in listeners:
            if conn.laddr.ip != "127.0.0.1" or conn.pid is None:
                continue
            try:
                process = psutil.Process(conn.pid)
                path, *args = config["command"]
                if (os.path.normcase(process.exe()) == os.path.normcase(path)
                        and process.cmdline()[1:] == args):
                    return True, process
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return bool(listeners), None

    @staticmethod
    def _ready(config: dict) -> bool:
        try:
            response = httpx.get(f"http://127.0.0.1:{config['port']}/health", timeout=1, trust_env=False)
            if response.status_code != 200:
                return False
            payload = response.json()
            return isinstance(payload, dict) and payload.get("status") == "ok"
        except (httpx.HTTPError, ValueError):
            return False

    def status(self) -> dict:
        if sys.platform != "win32":
            return {"state": "unsupported"}
        try:
            config = self._settings()
        except FileNotFoundError:
            return {"state": "unconfigured"}
        except (OSError, KeyError, ValueError, TypeError):
            return {"state": "invalid"}

        import win32event

        event = self._stop_event()
        try:
            busy, process = self._listener(config)
            if event is not None and win32event.WaitForSingleObject(event, 0) == win32event.WAIT_OBJECT_0:
                state = "stopping"
            elif event is not None:
                state = "running" if process is not None and self._ready(config) else "starting"
            elif busy:
                state = "external" if process is not None else "busy"
            else:
                state = "failed" if self.error_file.is_file() else "stopped"
        finally:
            if event is not None:
                event.Close()
        return {"state": state, "provider": config.get("provider"), "model": config.get("model"), "port": config["port"]}

    def start(self) -> dict:
        state = self.status()
        if state["state"] not in ("stopped", "failed", "external"):
            return state

        import win32api
        import win32job

        self.error_file.unlink(missing_ok=True)
        argv = [sys.executable, str(Path(__file__).resolve()), str(self.directory.parent)]
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        env = _environment()
        # Hermes can load the active environment into a bundled Python binary.
        env["PYTHONPATH"] = os.pathsep.join(path for path in sys.path if os.path.isabs(path))
        with (self.directory / "llama-tray.log").open("ab") as log_file:
            spawn = dict(cwd=self.directory, stdin=subprocess.DEVNULL, stdout=log_file,
                         stderr=subprocess.STDOUT, env=env)
            try:
                child = subprocess.Popen(argv, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **spawn)
            except OSError as exc:
                if exc.winerror != 5 or win32job.IsProcessInJob(win32api.GetCurrentProcess(), None):
                    raise
                child = subprocess.Popen(argv, creationflags=flags, **spawn)

        for _ in range(30):
            event = self._stop_event()
            if event is not None:
                event.Close()
                return self.status()
            if child.poll() is not None:
                return {**self.status(), "state": "failed"}
            time.sleep(0.05)
        return {**state, "state": "starting"}

    def stop(self) -> dict:
        if sys.platform == "win32":
            import win32event

            event = self._stop_event()
            if event is not None:
                try:
                    win32event.SetEvent(event)
                finally:
                    event.Close()
                return {"state": "stopping"}
        return self.status()

    def _run(self) -> None:
        import win32event

        mutex = win32event.CreateMutex(None, False, self.name)
        try:
            if win32event.WaitForSingleObject(mutex, 0) not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
                return
            try:
                config = self._settings()
                busy, incumbent = self._listener(config)
                if busy and incumbent is None:
                    raise RuntimeError(f"Port {config['port']} is occupied by another process")
                stop = win32event.CreateEvent(None, True, False, self.name + "-stop")
                try:
                    self._serve(config, incumbent, stop)
                finally:
                    stop.Close()
            finally:
                win32event.ReleaseMutex(mutex)
        finally:
            mutex.Close()

    def _serve(self, config, incumbent, stop) -> None:
        import win32api
        import win32con
        import win32event
        import win32gui

        message = win32con.WM_APP + 1
        taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
        icon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        exit_label = "Остановить Llama-сервер и выйти" if config.get("language") == "ru" else "Stop llama-server and exit"

        def window_proc(hwnd, event, wp, lp):
            if event == message and lp in (win32con.WM_RBUTTONUP, win32con.WM_LBUTTONDBLCLK):
                menu = win32gui.CreatePopupMenu()
                try:
                    win32gui.AppendMenu(menu, win32con.MF_STRING, 1, exit_label)
                    x, y = win32gui.GetCursorPos()
                    win32gui.SetForegroundWindow(hwnd)
                    if win32gui.TrackPopupMenu(menu, win32con.TPM_RETURNCMD | win32con.TPM_RIGHTBUTTON,
                                             x, y, 0, hwnd, None) == 1:
                        win32event.SetEvent(stop)
                    win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
                finally:
                    win32gui.DestroyMenu(menu)
                return 0
            if event == taskbar_created:
                win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, (hwnd, 0, flags, message, icon, "Llama Server"))
                return 0
            if event == win32con.WM_CLOSE or (event == win32con.WM_ENDSESSION and wp):
                win32event.SetEvent(stop)
                return 0
            return win32gui.DefWindowProc(hwnd, event, wp, lp)

        window_class = win32gui.WNDCLASS()
        window_class.hInstance = win32api.GetModuleHandle(None)
        window_class.lpszClassName = "HermesLlamaTray"
        window_class.lpfnWndProc = window_proc
        atom = win32gui.RegisterClass(window_class)
        hwnd = win32gui.CreateWindow(atom, "Llama Server", 0, 0, 0, 0, 0, 0, 0, window_class.hInstance, None)
        process = None
        child = None
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, (hwnd, 0, flags, message, icon, "Llama Server"))
            print(f"Tray icon registered (pid {os.getpid()})", flush=True)
            process = incumbent
            if process is None:
                with (self.directory / "llama-server.log").open("ab") as log_file:
                    child = subprocess.Popen(config["command"], cwd=Path(config["executable"]).parent,
                                             env={**_environment(), **config.get("environment", {})},
                                             stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
                                             creationflags=subprocess.CREATE_NO_WINDOW)
                process = psutil.Process(child.pid)
            deadline = time.monotonic() + 180
            ready = False
            while process.is_running() and (child is None or child.poll() is None):
                result = win32event.MsgWaitForMultipleObjects((stop,), False, 750, win32con.QS_ALLINPUT)
                if result == win32event.WAIT_OBJECT_0:
                    return
                if result == win32event.WAIT_OBJECT_0 + 1 and win32gui.PumpWaitingMessages():
                    return
                if not ready:
                    ready = self._ready(config)
                    if not ready and time.monotonic() > deadline:
                        raise RuntimeError("llama-server did not become ready in 180 seconds")
            raise RuntimeError(f"llama-server exited (code {child.poll() if child is not None else 'unknown'})")
        finally:
            try:
                if child is not None and child.poll() is None:
                    child.terminate()
                    child.wait(timeout=10)
                elif child is None and process is not None and process.is_running():
                    process.terminate()
                    process.wait(timeout=10)
            except psutil.NoSuchProcess:
                pass
            finally:
                try:
                    win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, 0))
                finally:
                    win32gui.DestroyWindow(hwnd)


if __name__ == "__main__":
    tray = LlamaTray(Path(sys.argv[1]))
    try:
        # A bundled interpreter that receives its modules through PYTHONPATH
        # does not run the virtual environment's pywin32.pth at startup.
        import pywin32_bootstrap  # noqa: F401

        tray._run()
    except Exception as error:
        tray.error_file.write_text(f"{type(error).__name__}: {error}\n", encoding="utf-8")
        raise
