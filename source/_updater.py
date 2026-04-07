# region File (auto-generated)
# endregion

# _updater.py (With Test Mode Support)
"""
A module to handle safe, in-place updates for a packaged Python application (.exe)
by checking against the latest release on GitHub.

TEST MODE: Set environment variable UPDATER_TEST_MODE=1 to use local test server
"""

from _imports import (
    # Standard library
    os, subprocess, sys, tempfile,
    # Typing
    Optional, Tuple,
)

import logging
import ctypes
from error_handler import ErrorPolicy, PolicyConfig, handle_exception


try:
    import requests
except ImportError:
    requests = None

try:
    from packaging import version
except ImportError:
    version = None

logger = logging.getLogger(__name__)

def _simple_version_tuple(v: str) -> tuple:
    """Fallback version parser when packaging module unavailable."""
    try:
        s = str(v).lstrip('vV')
        parts = [int(p) for p in s.split('.') if p.isdigit()]
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts[:3])
    except Exception:
        return ()

class GitHubUpdater:
    """
    Handles checking for and applying updates from a GitHub repository's releases.
    """
    def __init__(self, repo_owner: str, repo_name: str, current_version: str):
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.current_version = current_version

        # TEST MODE: Use local server if environment variable set
        if os.environ.get('UPDATER_TEST_MODE') == '1':
            self.api_url = "http://localhost:8765/api/releases/latest"
            logger.warning("⚠️  TEST MODE ACTIVE - Using local test server")
            print("⚠️  TEST MODE ACTIVE - Using local test server")
        else:
            self.api_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/releases/latest"

        self.current_exe_path = os.path.realpath(sys.executable)
        self.exe_name = os.path.basename(self.current_exe_path)
        self.temp_dir = tempfile.gettempdir()

        self.latest_version: Optional[str] = None
        self.download_url: Optional[str] = None

    def _get_latest_release(self) -> Optional[Tuple[str, str]]:
        """Fetches the latest release data from the GitHub API."""
        try:
            logger.info(f"Checking for updates at {self.api_url}")
            data = None
            if requests:
                resp = requests.get(self.api_url, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            else:
                # Fallback to urllib if requests is not available
                import urllib.request as _ur
                import json as _json
                with _ur.urlopen(self.api_url, timeout=15) as r:
                    raw = r.read()
                    data = _json.loads(raw.decode('utf-8'))

            tag_name = data.get("tag_name")
            if not tag_name:
                logger.warning("Latest release has no tag_name.")
                return None

            # Prefer .exe asset and optionally capture a .sha256 sidecar if present
            exe_url = None
            sha_url = None
            for asset in data.get("assets", []):
                name = asset.get("name", "").lower()
                url = asset.get("browser_download_url")
                if name.endswith(".sha256"):
                    sha_url = url
                if name.endswith(".exe"):
                    exe_url = url
            # Stash sha url on the instance for later verification
            self.sha256_url = sha_url
            if exe_url:
                return tag_name, exe_url

            logger.warning("No .exe asset found in the latest release.")
            return None
        except Exception as e:
            logger.exception("Failed to fetch release info")
            return None

    def check_for_updates(self) -> bool:
        """Checks if a newer version of the application is available."""
        release_info = self._get_latest_release()
        if not release_info:
            return False

        latest_tag, download_url = release_info
        cleaned_latest_ver = str(latest_tag).lstrip('v')

        try:
            if version:
                is_newer = version.parse(cleaned_latest_ver) > version.parse(self.current_version)
            else:
                remote_t = _simple_version_tuple(cleaned_latest_ver)
                local_t = _simple_version_tuple(self.current_version)
                is_newer = remote_t > local_t if remote_t and local_t else False

            if is_newer:
                logger.info(f"New version found: {cleaned_latest_ver} (current: {self.current_version})")
                self.latest_version = cleaned_latest_ver
                self.download_url = download_url
                return True
            else:
                logger.info("Current version is up to date.")
                return False
        except Exception as e:
            logger.error(f"Version comparison failed: {e}")
            return False

    def _create_updater_script(self, new_exe_path: str) -> str:
        """Creates a robust batch script in the temp directory to perform the update."""
        backup_exe_path = f"{self.current_exe_path}.bak"
        script_path = os.path.join(self.temp_dir, "updater.bat")

        old_exe_filename = os.path.basename(self.current_exe_path)
        backup_filename = f"{old_exe_filename}.bak"

        script_content = f"""@echo off
setlocal

REM Paths passed from the Python script
SET "OLD_EXE={self.current_exe_path}"
SET "NEW_EXE={new_exe_path}"
SET "BACKUP_EXE={backup_exe_path}"

echo Waiting for application to close...

REM --- Robust retry loop to handle file locking ---
SET /A MAX_RETRIES=15
SET /A RETRY_COUNT=0
:RETRY_LOOP
REM Try to rename the file. "2>nul" suppresses error messages.
ren "%OLD_EXE%" "{backup_filename}" 2>nul
IF EXIST "%BACKUP_EXE%" (
    echo Successfully backed up old executable.
    GOTO UPDATE_SUCCESS
)

SET /A RETRY_COUNT+=1
IF %RETRY_COUNT% GEQ %MAX_RETRIES% (
    echo Failed to get exclusive access to the executable after %MAX_RETRIES% seconds.
    GOTO UPDATE_FAIL
)
echo Waiting for file lock release... (Attempt %RETRY_COUNT%/%MAX_RETRIES%)
timeout /t 1 /nobreak > nul
GOTO RETRY_LOOP
REM --- End of retry loop ---

:UPDATE_SUCCESS
echo Moving new executable into place...
move /Y "%NEW_EXE%" "%OLD_EXE%"
IF ERRORLEVEL 1 (
    echo FAILED to move the new executable.
    GOTO UPDATE_FAIL
)

echo Update complete. Relaunching application...
start "" "%OLD_EXE%"
GOTO CLEANUP

:UPDATE_FAIL
echo Update failed. Restoring backup...
IF EXIST "%BACKUP_EXE%" (
    move /Y "%BACKUP_EXE%" "%OLD_EXE%"
)
echo Please try the update again later.
pause
GOTO CLEANUP

:CLEANUP
echo Cleaning up temporary files...
REM Delete backup if update succeeded
IF EXIST "%BACKUP_EXE%" del "%BACKUP_EXE%"
REM The script deletes itself on completion.
del "%~f0"

endlocal
"""
        with open(script_path, "w") as f:
            f.write(script_content)
        return script_path

    def _run_as_admin(self, script_path: str) -> bool:
        """Executes a script with administrator privileges, triggering a UAC prompt."""
        if sys.platform != 'win32':
            logger.error("UAC elevation is only supported on Windows.")
            return False
        try:
            # Use cmd.exe to run the batch file elevated
            ret_code = ctypes.windll.shell32.ShellExecuteW(
                None,                    # hwnd
                "runas",                 # lpOperation (triggers UAC)
                "cmd.exe",               # lpFile (run cmd.exe elevated)
                f'/c "{script_path}"',   # lpParameters (execute batch file)
                None,                    # lpDirectory
                0                        # nShowCmd (0 = SW_HIDE, hidden window)
            )
            if ret_code <= 32:
                logger.error(f"Failed to elevate updater script. ShellExecuteW returned {ret_code}")
                return False
            return True
        except Exception as e:
            logger.exception("Failed to run script as admin")
            return False

    def _can_write_current_dir(self) -> bool:
        try:
            return os.access(os.path.dirname(self.current_exe_path), os.W_OK)
        except Exception:
            return False

    def run_update(self):
        """Downloads the update and executes the updater script with elevation."""
        if not self.download_url:
            logger.error("run_update() called without a pending update.")
            return

        try:
            logger.info(f"Downloading update from {self.download_url}")
            new_exe_path = os.path.join(self.temp_dir, self.exe_name)

            if requests:
                with requests.get(self.download_url, stream=True, timeout=30) as r:
                    r.raise_for_status()
                    with open(new_exe_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
            else:
                import urllib.request as _ur
                with _ur.urlopen(self.download_url, timeout=30) as r:
                    with open(new_exe_path, 'wb') as f:
                        f.write(r.read())

            logger.info(f"Download complete. New version saved to {new_exe_path}")

            # Optional integrity verification via sidecar .sha256 asset
            try:
                sha_url = getattr(self, 'sha256_url', None)
                if sha_url and requests:
                    resp = requests.get(sha_url, timeout=15)
                    resp.raise_for_status()
                    txt = resp.text.strip()
                    # expected format: '<sha256>  filename'
                    expected = txt.split()[0] if txt else None
                    if expected:
                        calc = self._compute_sha256(new_exe_path)
                        if calc.lower() != expected.lower():
                            logger.error("Downloaded update failed SHA-256 verification; aborting update")
                            try:
                                os.remove(new_exe_path)
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                            return
            except Exception:
                logger.warning("Skipping SHA-256 verification (sidecar missing or request failed)")

            updater_script_path = self._create_updater_script(new_exe_path)
            logger.info("Updater script created. Requesting elevation to run update.")

            # Prefer non-admin if the install directory is writable
            launched = False
            if self._can_write_current_dir():
                try:
                    subprocess.Popen(['cmd', '/c', updater_script_path])
                    launched = True
                except Exception:
                    logger.warning("Non-elevated updater launch failed; attempting elevation")

            if not launched:
                if self._run_as_admin(updater_script_path):
                    logger.info("Updater script launched with elevation. Exiting application.")
                    sys.exit(0)
                else:
                    logger.error("Could not launch updater script with admin privileges.")
                    # Clean up the downloaded file if elevation fails
                    if os.path.exists(new_exe_path):
                        os.remove(new_exe_path)

        except Exception as e:
            logger.exception("An error occurred during the update process")

    def _compute_sha256(self, path: str) -> str:
        import hashlib as _hl
        h = _hl.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()