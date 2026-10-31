# Cómo colaborar

Gracias por mirar el proyecto. Es un asistente de escritorio (Tkinter) que ayuda a configurar
[OpenTabletDriver](https://opentabletdriver.net) para osu!. No tiene dependencias de `pip`.

## Empezar

```
py -3 -m osu_tablet_assistant --dry-run     # abre el asistente sin tocar OpenTabletDriver
py -3 -m unittest discover -s tests         # tests (Python 3.10+, solo biblioteca estándar)
```

Los tests de ventanas se activan con la variable `OSU_TABLET_GUI_TESTS=1` (necesitan pantalla).
Si cambias los datos de `osu_tablet_assistant/knowledge/`, regenera `data/` con `python tools/refresh_data.py`:
los tests fallan si no están al día.

## Dónde está cada cosa

- `docs/ARCHITECTURE.md`: módulos y cómo se conectan.
- `docs/PLAY_COACH.md`, `docs/COMPANION.md`, `docs/DESIGN.md`: el coach de partidas, el modo acompañante y el diseño.
- `docs/research/`: de dónde salen los datos de la comunidad y del catálogo de filtros.
- `osu_tablet_assistant/engine/`: lógica pura (sin ventanas). `gui/`: pantallas. `otd/`: lo que toca OpenTabletDriver.

## Normas del proyecto

- Solo biblioteca estándar de Python. Nada de `pip install`.
- Todo texto de la interfaz va en `osu_tablet_assistant/locales/es/` **y** `en/` (un test comprueba que coinciden).
  Lenguaje llano, sin jerga.
- Sin emoji ni símbolos usados como iconos en la interfaz (un test lo vigila).
- Cada cambio de comportamiento lleva su test. La lógica va en módulos sin Tk para poder probarla.
- Nada se aplica a OpenTabletDriver sin copia de seguridad ni confirmación, y nunca se instalan plugins ni se
  activa predicción por cuenta propia.
- Sé honesto en los documentos: lo que no se ha probado con un lápiz y osu! reales se dice como no verificado.

## Publicar una versión

`main` siempre es la versión actual. Las anteriores se conservan como etiquetas de Git y en `CHANGELOG.md`.
Para publicar la siguiente, con el árbol limpio:

```
powershell -ExecutionPolicy Bypass -File tools\release.ps1 -Version 0.9.0 -Notes "Qué cambia, en una frase"
```

El script comprueba los tests, sube el número de versión, añade la entrada al `CHANGELOG.md`, hace el commit y
la etiqueta `v0.9.0`, compila el `.exe` y mueve el ejecutable anterior de la carpeta del Escritorio a
`Versiones anteriores`, dejando solo el nuevo. No sube nada: al final muestra el comando `git push`, el archivo
`Osu-Open-Tablet-vX.Y.Z.exe` (en la carpeta del Escritorio, junto a `LICENSE`) que hay que subir a un *Release* de GitHub para que cualquiera lo descargue
(con su huella SHA-256 en las notas) y avisa de que el ejecutable no está firmado.
Con `-DryRun` solo muestra lo que haría.
