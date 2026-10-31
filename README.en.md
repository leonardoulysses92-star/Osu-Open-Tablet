# osu-open-tablet

[Español](README.md) is the main README; this is a short summary.

A desktop assistant (Tkinter) that helps configure [OpenTabletDriver](https://opentabletdriver.net) (OTD) for osu!: tablet area, aspect ratio and smoothing filters. It works with any tablet OTD supports.

It measures the pen with four short tests, generates several candidate configurations, tries them one by one and recommends the best one, with a reason for every change. It also compares against configurations published by top players, with source and date.

Early stage (0.1.x). The live-apply path (OTD Console and daemon pipe) is written from OTD's source and has not been verified on real hardware.

## Requirements

- Windows 10 or 11. Linux and macOS are untested.
- Python 3.10+ with `tkinter`. No `pip` dependencies.
- OpenTabletDriver 0.6.x, installed or portable. 0.6.8.1 or later is preferable on Windows when using interpolators.

## Run

```
py -3 -m osu_tablet_assistant
```

Or double-click `run.bat`. Options: `--lang es|en`, `--config-dir PATH`, `--install-dir PATH`, `--dry-run` (never applies anything to OTD).

For a standalone `.exe`: `powershell -ExecutionPolicy Bypass -File build_exe.ps1` (needs PyInstaller).

## Safety

A backup of `settings.json` is written to `<config>\Backup\` before the first change of each session, every change asks for confirmation, and `--dry-run` does not touch OTD. It never closes or restarts OTD and never installs plugins.

## Tests

```
py -3 -m unittest discover -s tests
```

## Docs

[Architecture](docs/ARCHITECTURE.md), [companion (live-tuning) mode](docs/COMPANION.md) (English) and [research notes](docs/research/). The usage guide, community data and filter catalog ([guia](docs/guia.md), [comunidad](docs/comunidad.md), [filtros](docs/filtros.md)) are in Spanish.

## License

[MIT](LICENSE)
