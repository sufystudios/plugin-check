# plugin-check

Test audio plugins in every format with the validator made for each, and get one
report, from one command, on macOS, Windows or Linux.

```
$ plugin-check build/bin
plugin-check 0.1.0  (Darwin arm64)

CLAP  build/bin/MySynth.clap
  ✓ Has its binary
  ✓ Universal (Apple Silicon and Intel)
  ! Code signed with a Developer ID
      Ad-hoc signature only.
      → Fine on the machine that built it; downloads need a Developer ID Application
        certificate (codesign --timestamp --options runtime) and notarization.
  ✓ clap-validator (CLAP)

AU  build/bin/MySynth.component
  ✓ auval aumu MySy Acme (arm64)
  ✓ auval aumu MySy Acme (x86_64)
  ✓ pluginval (AU), strictness 5

VST3  build/bin/MySynth.vst3
  ✓ pluginval (VST3), strictness 5

PASSED  (3 plugin(s), 12 checks, 0 failed, 3 warnings)
```

| Format | File | Checked with |
|---|---|---|
| VST3 | `.vst3` | [pluginval](https://github.com/Tracktion/pluginval) (Tracktion) |
| AU | `.component` | `auval` (Apple), pluginval, and [auv3-test](https://github.com/sufystudios/auv3-test) if it's on your PATH |
| CLAP | `.clap` | [clap-validator](https://github.com/free-audio/clap-validator) (free-audio) |
| all | | bundle checks: the binary is there, and on macOS its architectures, code signing, notarization and quarantine |

pluginval and clap-validator are downloaded from their official GitHub releases
the first time they're needed, into a cache folder (`~/Library/Caches/plugin-check`,
`%LOCALAPPDATA%\plugin-check` or `~/.cache/plugin-check`). If they're already on
your PATH, those are used instead.

## Install

Needs Python 3.8 or later, nothing else.

```sh
git clone https://github.com/sufystudios/plugin-check.git
python3 plugin-check/plugin-check.py path/to/MySynth.vst3
```

## Use

```sh
plugin-check MySynth.vst3 MySynth.clap       # some plugins
plugin-check build/bin                        # every plugin in a folder
plugin-check ~/Library/Audio/Plug-Ins/VST3    # everything installed
plugin-check build/bin --json                 # for scripts and coding agents
```

| Option | What it does |
|---|---|
| `--json` | Print the report as JSON. |
| `--strictness <1-10>` | pluginval's strictness (default 5; 10 is the most thorough and slowest). |
| `--no-gui` | Don't open plugin windows (CI, headless machines). |
| `--only <checks>` | Run only some of `bundle,pluginval,clap-validator,auval,auv3-test`. |
| `--timeout <s>` | Seconds per validator run (default 600). |
| `--no-download` | Use validators on PATH or already cached; download nothing. |
| `--tools-dir <dir>` | Where downloaded validators are kept. |
| `--bottle <name>` | The CrossOver bottle for Windows builds (default `plugin-check`, created if missing). |
| `--crossover <app>` | Where CrossOver is, if it isn't found by itself. |
| `--verbose` | Print progress and every command run to stderr. |

**Exit codes:** `0` every check passed (there may be warnings), `1` at least one
failed, `2` nothing to test or bad options.

On macOS, an AU must be registered before `auval` can find it: put it in
`~/Library/Audio/Plug-Ins/Components` and, after replacing a build, run
`killall -9 AudioComponentRegistrar`.

## In CI

See [examples/github-actions.yml](examples/github-actions.yml): build your plugin on
macOS and Windows runners, then run `plugin-check build/bin --no-gui`. Pass
`GITHUB_TOKEN` in its environment: GitHub's API allows few anonymous calls from CI
machines (plugin-check falls back to the release pages if it's refused).

## Windows plugins on a Mac or Linux (CrossOver or Wine)

Give plugin-check a Windows build on a Mac or Linux (a `.vst3` with a Windows binary
inside, or a Windows `.clap`) and it runs the **Windows** pluginval and
clap-validator under [CrossOver](https://www.codeweavers.com/crossover) if it's
installed, or [Wine](https://www.winehq.org), and says so in the report:

```
$ plugin-check build-windows/
VST3  build-windows/MySynth.vst3
  ✓ Has its binary
  ✓ Windows 64-bit (x64)
  ✓ pluginval (VST3, in CrossOver), strictness 5
```

- CrossOver: a bottle named `plugin-check` (Windows 10 64-bit) is created the first
  time; pick another with `--bottle`. CrossOver is found in `/Applications`,
  `~/Applications` or `/Volumes/*/Applications`, or pass `--crossover <path to .app>`.
- Wine: a prefix of its own in plugin-check's cache folder.

Treat it as a rough check: Wine isn't Windows, so a pass is encouraging and a failure
may be Wine's. OpenGL windows, audio timing and drivers (rtpMIDI, ASIO) are where it
differs most. For a real answer, run plugin-check on a Windows CI runner.

## Licence

MIT. pluginval (GPL-3.0) and clap-validator (MIT) are separate programs, downloaded
from their own releases and run as they are; plugin-check doesn't include them.
