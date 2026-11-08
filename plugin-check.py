#!/usr/bin/env python3
"""
plugin-check: test audio plugins in every format with the validator made for each,
and get one report.

    VST3  .vst3        pluginval (Tracktion)
    AU    .component   auval (Apple), pluginval, and auv3-test if it's installed
    CLAP  .clap        clap-validator (free-audio)
    all                bundle checks: the binary is there, its architectures, code
                       signing, notarization and quarantine (macOS)

The validators are downloaded the first time they're needed, into a cache folder,
from their official GitHub releases. Python 3.8+, standard library only; runs on
macOS, Windows and Linux.

Exit codes: 0 every check passed (there may be warnings), 1 at least one failed,
2 nothing to test or bad options.
"""

import argparse
import io
import json
import os
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

VERSION = "0.1.4"

PASS, WARN, FAIL, SKIP = "passed", "warning", "failed", "skipped"
SYMBOL = {PASS: "✓", WARN: "!", FAIL: "✗", SKIP: "-"}
ASCII_SYMBOL = {PASS: "+", WARN: "!", FAIL: "x", SKIP: "-"}

FORMATS = {".vst3": "VST3", ".clap": "CLAP", ".component": "AU"}

SYSTEM = platform.system()  # Darwin, Windows, Linux


# MARK: - Results


class Check:
    def __init__(self, id, title, status, detail="", hint=""):
        self.id, self.title, self.status, self.detail, self.hint = id, title, status, detail, hint

    def to_json(self):
        return {"id": self.id, "title": self.title, "status": self.status, "detail": self.detail, "hint": self.hint}


# MARK: - Tools


def cache_dir():
    if SYSTEM == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    elif SYSTEM == "Darwin":
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "plugin-check")


def log(message, verbose):
    if verbose:
        print(message, file=sys.stderr)


def download(url, verbose):
    for attempt in range(3):
        try:
            return _download(url, verbose)
        except (urllib.error.HTTPError, RuntimeError) as e:
            code = getattr(e, "code", None)
            transient = (code is not None and code >= 500) or " 50" in str(e)
            if not transient or attempt == 2:
                raise
            log(f"{url}: {e}; trying again", verbose)
            time.sleep(2 * (attempt + 1))


def _download(url, verbose):
    """Python's own HTTPS, or curl when Python has no certificates to check with (the
    python.org installer on macOS, until its Install Certificates step is run)."""
    log(f"downloading {url}", verbose)
    headers = {"User-Agent": f"plugin-check/{VERSION}"}
    # GitHub's API allows few anonymous calls from shared CI machines: use a token if there is one.
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" not in str(e):
            raise
    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError("Python can't verify HTTPS certificates here and curl isn't installed. "
                           "On macOS, run 'Install Certificates.command' in your Python folder.")
    log("Python has no CA certificates; downloading with curl", verbose)
    p = subprocess.run([curl, "-fsSL", "-A", f"plugin-check/{VERSION}", url], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=300)
    if p.returncode != 0:
        raise RuntimeError(f"curl couldn't download {url}: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout


def redirected_url(url, verbose):
    """Where a URL redirects to, with Python's HTTPS or curl."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": f"plugin-check/{VERSION}"}, method="HEAD")
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.geturl()
    except urllib.error.URLError as e:
        log(f"couldn't follow {url} with Python ({e}); trying curl", verbose)
    curl = shutil.which("curl")
    if not curl:
        return ""
    p = subprocess.run([curl, "-sIL", "-o", os.devnull, "-w", "%{url_effective}", url],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    return p.stdout.decode().strip()


def latest_assets(repo, verbose):
    """The latest release's tag and assets: from GitHub's API, or, when that's rate
    limited, from the release page itself."""
    try:
        release = json.loads(download(f"https://api.github.com/repos/{repo}/releases/latest", verbose))
        return release.get("tag_name", ""), release.get("assets", [])
    except urllib.error.HTTPError as e:
        if e.code not in (403, 429):
            raise
        log(f"GitHub's API said {e.code}; reading the release page instead", verbose)
    # /releases/latest redirects to /releases/tag/<tag>; the assets list loads from expanded_assets.
    final = redirected_url(f"https://github.com/{repo}/releases/latest", verbose)
    tag = final.rsplit("/", 1)[-1] if "/tag/" in final else ""
    if not tag:
        raise RuntimeError(f"couldn't find {repo}'s latest release")
    page = download(f"https://github.com/{repo}/releases/expanded_assets/{tag}", verbose).decode("utf-8", "replace")
    links = sorted(set(re.findall(rf'href="(/{re.escape(repo)}/releases/download/[^"]+)"', page)))
    return tag, [{"name": l.rsplit("/", 1)[-1], "browser_download_url": "https://github.com" + l} for l in links]


def unpack(data, name, dest):
    os.makedirs(dest, exist_ok=True)
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for member in z.infolist():
                target = os.path.realpath(os.path.join(dest, member.filename))
                if not target.startswith(os.path.realpath(dest)):
                    raise RuntimeError(f"unsafe path in {name}: {member.filename}")
                z.extract(member, dest)
                # Keep the executable bit, which zipfile drops.
                mode = member.external_attr >> 16
                if mode & 0o111:
                    os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    elif name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            for member in t.getmembers():
                target = os.path.realpath(os.path.join(dest, member.name))
                if not target.startswith(os.path.realpath(dest)) or member.issym() or member.islnk():
                    raise RuntimeError(f"unsafe entry in {name}: {member.name}")
            t.extractall(dest)
    else:
        raise RuntimeError(f"don't know how to unpack {name}")


def find_file(root, names):
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f in names:
                return os.path.join(dirpath, f)
    return None


def find_archive(root):
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f.endswith((".zip", ".tar.gz", ".tgz")):
                return os.path.join(dirpath, f)
    return None


class Tools:
    """Finds each validator: on PATH, in the cache, or downloaded into the cache."""

    def __init__(self, tools_dir, allow_download, verbose):
        self.dir = tools_dir
        self.allow_download = allow_download
        self.verbose = verbose
        self._found = {}
        self._errors = {}

    def _get(self, name, fetch):
        """A tool's path; a download that failed is reported again for each plugin, not skipped."""
        if name in self._errors:
            raise RuntimeError(self._errors[name])
        if name not in self._found:
            try:
                self._found[name] = fetch()
            except Exception as e:
                self._errors[name] = f"couldn't get {name}: {type(e).__name__}: {e}"
                raise RuntimeError(self._errors[name]) from e
        return self._found[name]

    def pluginval(self):
        return self._get("pluginval", self._fetch_pluginval)

    def _fetch_pluginval(self):
        exe = "pluginval.exe" if SYSTEM == "Windows" else "pluginval"
        path = shutil.which("pluginval")
        if not path:
            root = os.path.join(self.dir, "pluginval")
            path = find_file(root, {exe}) if os.path.isdir(root) else None
            if not path and self.allow_download:
                asset = {"Darwin": "pluginval_macOS.zip", "Windows": "pluginval_Windows.zip"}.get(SYSTEM, "pluginval_Linux.zip")
                url = f"https://github.com/Tracktion/pluginval/releases/latest/download/{asset}"
                unpack(download(url, self.verbose), asset, root)
                path = find_file(root, {exe})
        return path

    def clap_validator(self):
        return self._get("clap-validator", self._fetch_clap_validator)

    def _fetch_clap_validator(self):
        exe = "clap-validator.exe" if SYSTEM == "Windows" else "clap-validator"
        path = shutil.which("clap-validator")
        if not path:
            root = os.path.join(self.dir, "clap-validator")
            path = find_file(root, {exe}) if os.path.isdir(root) else None
            if not path and self.allow_download:
                _, assets = latest_assets("free-audio/clap-validator", self.verbose)
                want = {"Darwin": "macos-universal", "Windows": "windows"}.get(SYSTEM, "ubuntu")
                asset = next((a for a in assets if want in a["name"]), None)
                if asset:
                    unpack(download(asset["browser_download_url"], self.verbose), asset["name"], root)
                    # Its zip holds a tarball (to keep the executable bit): unpack that too.
                    inner = find_archive(root)
                    while inner and not find_file(root, {exe}):
                        with open(inner, "rb") as f:
                            unpack(f.read(), os.path.basename(inner), os.path.dirname(inner))
                        os.remove(inner)
                        inner = find_archive(root)
                    path = find_file(root, {exe})
        return path


def run(cmd, timeout, verbose):
    log("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), verbose)
    started = time.time()
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        out = p.stdout.decode("utf-8", errors="replace")
        return p.returncode, out, time.time() - started
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", errors="replace")
        return None, out, time.time() - started
    except OSError as e:
        return -1, str(e), time.time() - started


def tail(text, lines=12):
    rows = [r for r in text.strip().splitlines() if r.strip()]
    return "\n".join(rows[-lines:])


# MARK: - Bundle checks


def plugin_binary(path, fmt):
    """The plugin's executable inside its bundle (or the file itself)."""
    if os.path.isfile(path):
        return path
    if SYSTEM == "Darwin" or os.path.isdir(os.path.join(path, "Contents", "MacOS")):
        macos = os.path.join(path, "Contents", "MacOS")
        if os.path.isdir(macos):
            info = os.path.join(path, "Contents", "Info.plist")
            name = None
            if os.path.isfile(info):
                with open(info, "rb") as f:
                    name = plistlib.load(f).get("CFBundleExecutable")
            candidate = os.path.join(macos, name) if name else None
            if candidate and os.path.isfile(candidate):
                return candidate
            files = [os.path.join(macos, f) for f in os.listdir(macos)]
            return next((f for f in files if os.path.isfile(f)), None)
    if fmt == "VST3":
        # Windows and Linux bundles: Contents/<arch>-win/<name>.vst3 or Contents/<arch>-linux/<name>.so
        contents = os.path.join(path, "Contents")
        if os.path.isdir(contents):
            for arch in sorted(os.listdir(contents)):
                folder = os.path.join(contents, arch)
                if os.path.isdir(folder) and (arch.endswith("-win") or arch.endswith("-linux")):
                    for f in os.listdir(folder):
                        if f.endswith((".vst3", ".so")):
                            return os.path.join(folder, f)
    return None


def bundle_checks(path, fmt, verbose):
    checks = []
    binary = plugin_binary(path, fmt)
    if not binary:
        checks.append(Check("bundle", "Has its binary", FAIL, f"No executable found in {path}.",
                            "A VST3 or AU bundle keeps it in Contents/MacOS (macOS) or Contents/<arch>-win (Windows)."))
        return checks, None
    checks.append(Check("bundle", "Has its binary", PASS, os.path.relpath(binary, os.path.dirname(path))))

    if SYSTEM == "Darwin":
        code, out, _ = run(["lipo", "-archs", binary], 30, verbose)
        archs = out.split() if code == 0 else []
        if not archs:
            checks.append(Check("architectures", "Architectures", WARN, "lipo couldn't read the binary."))
        elif "arm64" in archs and "x86_64" in archs:
            checks.append(Check("architectures", "Universal (Apple Silicon and Intel)", PASS, " ".join(archs)))
        else:
            missing = "Intel (x86_64)" if "arm64" in archs else "Apple Silicon (arm64)"
            checks.append(Check("architectures", "Universal (Apple Silicon and Intel)", WARN,
                                f"{' '.join(archs)} only: it won't load on {missing} Macs.",
                                "Build both, e.g. CMAKE_OSX_ARCHITECTURES=\"arm64;x86_64\"."))

        code, out, _ = run(["codesign", "-dv", "--verbose=2", path], 30, verbose)
        authority = re.findall(r"Authority=(.+)", out)
        if code != 0:
            checks.append(Check("signing", "Code signed", WARN, "Not signed.",
                                "Downloaded plugins need a Developer ID signature or macOS blocks them."))
        elif "Signature=adhoc" in out or not authority:
            checks.append(Check("signing", "Code signed with a Developer ID", WARN, "Ad-hoc signature only.",
                                "Fine on the machine that built it; downloads need a Developer ID Application "
                                "certificate (codesign --timestamp --options runtime) and notarization."))
        else:
            ok = any(a.startswith("Developer ID Application") for a in authority)
            checks.append(Check("signing", "Code signed with a Developer ID", PASS if ok else WARN, authority[0],
                                "" if ok else "Signed, but not with a Developer ID: other Macs will block it."))
            code, out, _ = run(["spctl", "--assess", "--type", "install", "-vv", path], 60, verbose)
            accepted = code == 0 and "accepted" in out
            notarized = "Notarized" in out
            checks.append(Check("notarization", "Notarized (Gatekeeper accepts it)",
                                PASS if accepted and notarized else WARN, tail(out, 3),
                                "" if accepted else "Notarize it (xcrun notarytool submit --wait) and staple "
                                "the ticket (xcrun stapler staple)."))

        code, out, _ = run(["xattr", "-p", "com.apple.quarantine", path], 30, verbose)
        if code == 0 and out.strip():
            checks.append(Check("quarantine", "Not quarantined", WARN, "It carries the quarantine flag from a download.",
                                "If it's yours, xattr -dr com.apple.quarantine <bundle>; if not, it must be "
                                "notarized to load."))
    return checks, binary


# MARK: - Validators


def check_pluginval(path, fmt, tools, args):
    exe = tools.pluginval()
    if not exe:
        return Check("pluginval", f"pluginval ({fmt})", SKIP, "pluginval isn't available (and downloading is off).")
    cmd = [exe, "--strictness-level", str(args.strictness), "--validate-in-process", "--timeout-ms", "120000"]
    if args.no_gui:
        cmd.append("--skip-gui-tests")
    cmd += ["--validate", path]
    code, out, seconds = run(cmd, args.timeout, args.verbose)
    if code is None:
        return Check("pluginval", f"pluginval ({fmt}), strictness {args.strictness}", FAIL,
                     f"Timed out after {args.timeout} s.\n" + tail(out),
                     "A hang usually means a deadlock in the plugin's UI or state code.")
    failures = [l.strip() for l in out.splitlines() if "!!! FAILED" in l or "*** FAILED" in l or "FAILED:" in l]
    # JUCE's test runner logs "Starting test: pluginval / <name>..." for each test it runs.
    started = len(re.findall(r"Starting tests?(?: in)?:", out))
    if code == 0 and started:
        return Check("pluginval", f"pluginval ({fmt}), strictness {args.strictness}", PASS,
                     f"{started} tests passed in {seconds:.0f} s.")
    if code == 0:
        return Check("pluginval", f"pluginval ({fmt}), strictness {args.strictness}", WARN,
                     "pluginval exited cleanly but ran no tests that plugin-check could see.\n" + tail(out),
                     "Rerun with --verbose and check it found the plugin.")
    return Check("pluginval", f"pluginval ({fmt}), strictness {args.strictness}", FAIL,
                 ("\n".join(failures[:12]) if failures else tail(out)),
                 "Rerun with --verbose for pluginval's full log.")


def check_clap_validator(path, tools, args):
    exe = tools.clap_validator()
    if not exe:
        return Check("clap-validator", "clap-validator (CLAP)", SKIP, "clap-validator isn't available (and downloading is off).")
    code, out, seconds = run([exe, "validate", "--json", path], args.timeout, args.verbose)
    if code is None:
        return Check("clap-validator", "clap-validator (CLAP)", FAIL, f"Timed out after {args.timeout} s.\n" + tail(out))
    failed, warned, skipped, total = [], [], 0, 0
    try:
        # {"results": [{"test": {"plugin-instance": {"test": "name", ...}}, "status": {"code": "success", ...}}]}
        report = json.loads(out[out.index("{"):] if out.lstrip().startswith("{") else out[out.index("\n{") + 1:])
        for result in report.get("results", []):
            total += 1
            test = result.get("test", {})
            inner = next(iter(test.values()), {}) if isinstance(test, dict) and test else {}
            name = inner.get("test", "?") if isinstance(inner, dict) else str(test)
            status = result.get("status", {})
            code_name = status.get("code", "") if isinstance(status, dict) else str(status)
            reason = (status.get("details") or "") if isinstance(status, dict) else ""
            line = f"{name}: {reason}".strip(": ")
            if code_name in ("failed", "crashed"):
                failed.append(line)
            elif code_name == "warning":
                warned.append(line)
            elif code_name == "skipped":
                skipped += 1
    except (ValueError, AttributeError):
        pass
    if not total and not failed:
        return Check("clap-validator", "clap-validator (CLAP)", FAIL if code not in (0, None) else WARN,
                     "Couldn't read clap-validator's results.\n" + tail(out),
                     "Run it yourself with --verbose; its output format may have changed.")
    if failed or (code not in (0, None) and not total):
        return Check("clap-validator", "clap-validator (CLAP)", FAIL,
                     "\n".join(failed[:12]) if failed else tail(out),
                     "Rerun clap-validator with --only-failed for the full reasons.")
    status = WARN if warned else PASS
    detail = (f"{total} tests ({total - skipped - len(warned)} passed, {skipped} skipped), none failed, in {seconds:.0f} s."
              + ("\n" + "\n".join(warned[:8]) if warned else ""))
    return Check("clap-validator", "clap-validator (CLAP)", status, detail)


def au_codes(path):
    with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
        info = plistlib.load(f)
    components = info.get("AudioComponents") or []
    return [(c.get("type", ""), c.get("subtype", ""), c.get("manufacturer", ""), c.get("name", "")) for c in components]


def check_auval(path, args):
    if SYSTEM != "Darwin":
        return [Check("auval", "auval (AU)", SKIP, "auval is macOS only.")]
    try:
        codes = au_codes(path)
    except (OSError, plistlib.InvalidFileException) as e:
        return [Check("auval", "auval (AU)", FAIL, f"Couldn't read Info.plist: {e}")]
    if not codes:
        return [Check("auval", "auval (AU)", FAIL, "Info.plist lists no AudioComponents.",
                      "An AU bundle declares its type, subtype and manufacturer codes under AudioComponents.")]
    checks = []
    archs = ["arm64", "x86_64"] if platform.machine() == "arm64" else [None]
    for type_, sub, manu, name in codes:
        for arch in archs:
            cmd = (["arch", f"-{arch}"] if arch else []) + ["auval", "-v", type_, sub, manu]
            code, out, _ = run(cmd, args.timeout, args.verbose)
            label = f"auval {type_} {sub} {manu}" + (f" ({arch})" if arch else "")
            if code is None:
                checks.append(Check("auval", label, FAIL, "Timed out.\n" + tail(out)))
            elif "AU VALIDATION SUCCEEDED" in out:
                checks.append(Check("auval", label, PASS, name))
            elif arch == "x86_64" and ("Cannot get Component" in out or "didn't find the component" in out):
                checks.append(Check("auval", label, SKIP, "Not registered for this architecture."))
            else:
                errors = [l.strip() for l in out.splitlines() if "FAIL" in l or "ERROR" in l]
                checks.append(Check("auval", label, FAIL, "\n".join(errors[:10]) or tail(out),
                                    "If it's a new build, killall -9 AudioComponentRegistrar and try again."))
    return checks


def check_auv3_test(path, args):
    exe = shutil.which("auv3-test")
    if SYSTEM != "Darwin" or not exe:
        return []
    checks = []
    for type_, sub, manu, _ in au_codes(path):
        cmd = [exe, type_, sub, manu, "--json"] + (["--no-ui"] if args.no_gui else [])
        code, out, _ = run(cmd, args.timeout, args.verbose)
        try:
            report = json.loads(out[out.index("{"):])
        except ValueError:
            checks.append(Check("auv3-test", f"auv3-test {type_} {sub} {manu}", FAIL, tail(out)))
            continue
        for c in report.get("checks", []):
            status = {"passed": PASS, "warning": WARN, "failed": FAIL, "skipped": SKIP}.get(c.get("status"), WARN)
            checks.append(Check("auv3-test:" + c.get("id", "?"), "auv3-test: " + c.get("title", ""), status,
                                c.get("detail", ""), c.get("hint", "") if status != PASS else ""))
    return checks


# MARK: - Main


def plugin_paths(paths):
    found = []
    for p in paths:
        p = os.path.abspath(os.path.expanduser(p))
        ext = os.path.splitext(p.rstrip("/\\"))[1].lower()
        if ext in FORMATS:
            found.append((p.rstrip("/\\"), FORMATS[ext]))
        elif os.path.isdir(p):
            # A folder of plugins (a build's bin/, or a Plug-Ins folder): test each one.
            for name in sorted(os.listdir(p)):
                e = os.path.splitext(name)[1].lower()
                if e in FORMATS:
                    found.append((os.path.join(p, name), FORMATS[e]))
    return found


def guarded(id, title, step):
    """Runs one step; a problem in it (a failed download, say) is reported as that
    step's failure rather than hiding the others."""
    try:
        result = step()
        return result if isinstance(result, list) else [result]
    except Exception as e:
        return [Check(id, title, FAIL, f"{type(e).__name__}: {e}",
                      "GitHub may be having trouble; try again shortly." if "HTTP Error 5" in str(e) or " 50" in str(e)
                      else "Rerun with --verbose to see what plugin-check was doing.")]


def check_plugin(path, fmt, tools, args):
    checks = []
    if "bundle" in args.only:
        checks += guarded("bundle", "Bundle checks", lambda: bundle_checks(path, fmt, args.verbose)[0])
    if fmt in ("VST3", "AU") and "pluginval" in args.only:
        checks += guarded("pluginval", f"pluginval ({fmt})", lambda: check_pluginval(path, fmt, tools, args))
    if fmt == "CLAP" and "clap-validator" in args.only:
        checks += guarded("clap-validator", "clap-validator (CLAP)", lambda: check_clap_validator(path, tools, args))
    if fmt == "AU" and "auval" in args.only:
        checks += guarded("auval", "auval (AU)", lambda: check_auval(path, args))
    if fmt == "AU" and "auv3-test" in args.only:
        checks += guarded("auv3-test", "auv3-test", lambda: check_auv3_test(path, args))
    return checks


def main():
    parser = argparse.ArgumentParser(
        prog="plugin-check",
        description="Test VST3, AU and CLAP plugins with the validator made for each, and get one report.")
    parser.add_argument("paths", nargs="+", help="plugin bundles (.vst3, .component, .clap), or folders of them")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--strictness", type=int, default=5, choices=range(1, 11), metavar="1-10",
                        help="pluginval's strictness level (default 5; 10 is the most thorough)")
    parser.add_argument("--no-gui", action="store_true", help="don't open plugin windows (headless machines, CI)")
    parser.add_argument("--only", default="bundle,pluginval,clap-validator,auval,auv3-test",
                        help="comma-separated checks to run (default: all)")
    parser.add_argument("--timeout", type=int, default=600, help="seconds per validator run (default 600)")
    parser.add_argument("--tools-dir", default=cache_dir(), help="where downloaded validators are kept")
    parser.add_argument("--no-download", action="store_true", help="use validators on PATH or already cached only")
    parser.add_argument("--verbose", action="store_true", help="print progress and commands to stderr")
    parser.add_argument("--version", action="version", version=f"plugin-check {VERSION}")
    args = parser.parse_args()
    # A console that can't show ✓ and ✗ (Windows' cp1252, say) gets plain markers.
    global SYMBOL
    try:
        "✓✗→".encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        SYMBOL = ASCII_SYMBOL
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
    args.only = {s.strip() for s in args.only.split(",") if s.strip()}

    plugins = plugin_paths(args.paths)
    if not plugins:
        print("plugin-check: no .vst3, .component or .clap found in " + ", ".join(args.paths), file=sys.stderr)
        return 2

    tools = Tools(args.tools_dir, not args.no_download, args.verbose)
    results = []
    for path, fmt in plugins:
        log(f"checking {path}", args.verbose)
        try:
            checks = check_plugin(path, fmt, tools, args)
        except Exception as e:  # a broken download, say: report it rather than crash
            checks = [Check("plugin-check", "plugin-check", FAIL, f"{type(e).__name__}: {e}")]
        results.append({"path": path, "format": fmt, "checks": checks})

    failed = sum(1 for r in results for c in r["checks"] if c.status == FAIL)
    warned = sum(1 for r in results for c in r["checks"] if c.status == WARN)

    if args.json:
        print(json.dumps({
            "tool": "plugin-check", "version": VERSION, "platform": f"{SYSTEM} {platform.machine()}",
            "plugins": [{"path": r["path"], "format": r["format"], "checks": [c.to_json() for c in r["checks"]]}
                        for r in results],
            "failed": failed, "warnings": warned,
        }, indent=2))
    else:
        print(f"plugin-check {VERSION}  ({SYSTEM} {platform.machine()})")
        for r in results:
            print(f"\n{r['format']}  {r['path']}")
            for c in r["checks"]:
                print(f"  {SYMBOL[c.status]} {c.title}")
                for line in c.detail.splitlines():
                    print(f"      {line}")
                if c.hint and c.status in (FAIL, WARN):
                    print(f"      → {c.hint}")
        total = sum(len(r["checks"]) for r in results)
        verdict = "FAILED" if failed else "PASSED"
        print(f"\n{verdict}  ({len(results)} plugin(s), {total} checks, {failed} failed, {warned} warnings)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
