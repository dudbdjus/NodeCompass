"""One opt-in Windows logon entry for the current Windows user."""
from pathlib import Path
from subprocess import list2cmdline
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
ENTRY = "ProxyManager"


class StartupRegistration:
    def __init__(self, executable, directory, *, registry=winreg):
        self.registry = registry
        self.executable = Path(executable).resolve()
        self.command = list2cmdline((str(self.executable), "--data-dir", str(Path(directory).resolve())))

    def registered_command(self):
        r = self.registry
        try:
            with r.OpenKey(r.HKEY_CURRENT_USER, RUN_KEY, 0, r.KEY_QUERY_VALUE) as key:
                value, kind = r.QueryValueEx(key, ENTRY)
                return value if kind == r.REG_SZ else ""
        except FileNotFoundError:
            return None

    def set_enabled(self, enabled):
        r = self.registry
        if enabled:
            if len(self.command.encode("utf-16-le")) // 2 > 260 or not self.executable.is_file():
                raise ValueError("startup_path")
            with r.CreateKeyEx(r.HKEY_CURRENT_USER, RUN_KEY, 0, r.KEY_SET_VALUE) as key:
                r.SetValueEx(key, ENTRY, 0, r.REG_SZ, self.command)
        elif self.registered_command() == self.command:
            try:
                with r.OpenKey(r.HKEY_CURRENT_USER, RUN_KEY, 0, r.KEY_SET_VALUE) as key:
                    r.DeleteValue(key, ENTRY)
            except FileNotFoundError:
                pass
