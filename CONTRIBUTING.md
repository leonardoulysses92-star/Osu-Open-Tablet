# Cómo colaborar

El programa es Python 3.10 o posterior, solo con la biblioteca estándar (la interfaz es Tkinter). No hace
falta instalar nada con `pip`.

## Ejecutar

```
py -3 -m osu_tablet_assistant --dry-run     # abre el asistente sin tocar OpenTabletDriver
```

`run.bat` hace lo mismo sin `--dry-run`. Las demás opciones están en [docs/guia.md](docs/guia.md#opciones-al-arrancar).

Los errores de los que el programa se recupera solo (un mapa raro, un replay que no se puede leer...) se
anotan en `%APPDATA%\OsuOpenTablet\osu-open-tablet.log` (unos 256 KB como máximo, más una copia anterior).
No se envía a ningún sitio; sirve para adjuntarlo a una incidencia.

## Tests

```
py -3 -m unittest discover -s tests
```

Tardan entre uno y dos minutos. La mayoría no necesitan pantalla. Los que construyen ventanas (siempre ocultas)
se saltan solos si Tk no puede abrir una. Hay además una prueba que abre de verdad la ventana de la prueba de
puntería unos segundos; solo corre con la variable `OSU_TABLET_GUI_TESTS=1`:

```
$env:OSU_TABLET_GUI_TESTS='1'; py -3 -m unittest discover -s tests
```

Es normal ver algunos `Exception ignored ... RuntimeError: main thread is not in main loop` mezclados con los
puntos. Son inofensivos: el recolector de basura libera imágenes o variables de ventanas ya cerradas desde
otro hilo de la prueba y Tk se queja. No afectan al resultado; lo que cuenta es el `OK` del final.

En GitHub los tests corren en Windows con Python 3.10 y 3.12 (`.github/workflows/tests.yml`).

## Dónde está cada cosa

- `osu_tablet_assistant/engine/`: lógica pura, sin ventanas (área, recomendaciones, métricas, coach).
- `osu_tablet_assistant/gui/`: pantallas y ventanas. `otd/`: todo lo que toca OpenTabletDriver.
- `osu_tablet_assistant/knowledge/`: tabletas, catálogo de filtros y datos de la comunidad.
- `osu_tablet_assistant/locales/{es,en}/`: los textos de la interfaz.
- `data/`: copia generada de los datos de la comunidad que descarga la app. No se edita a mano: se regenera
  con `python tools/refresh_data.py` y los tests fallan si no está al día.
- `tools/`: regenerar `data/`, capturas de pantalla, compilar el `.exe` y publicar versiones.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): los módulos y cómo se conectan.

## Recetas

### Cambiar un texto de la interfaz

Todos los textos están en `osu_tablet_assistant/locales/es/` y `en/`, con las mismas claves en los dos idiomas
(un test lo comprueba). El archivo depende del prefijo de la clave:

| Prefijo | Archivo |
|---|---|
| `core.`, `gui.`, `profile.` | `core.json` |
| `engine.` | `engine.json` |
| `companion.`, `adapt.`, `livetune.`, `passive.` | `companion.json` |
| `play.rule.`, `play.style.` (hábitos y reglas del coach) | `style.json` |
| el resto de `play.` (coach de partidas) | `play.json` |
| `tests.` (pruebas con el lápiz) | `tests.json` |
| `otd.` | `otd.json` |

Cambia el texto en los dos idiomas a la vez. Frases cortas y llanas, sin emoji ni símbolos usados como iconos
(otro test lo vigila).

### Añadir un filtro

1. `osu_tablet_assistant/knowledge/filters.json`: la entrada con su `path` exacto de OTD, sus parámetros y
   rangos (el `path` y los nombres de las propiedades salen del código fuente del plugin).
2. `osu_tablet_assistant/otd/plugins.py`: el plugin en `KNOWN_PLUGINS`, para reconocerlo instalado.
3. `osu_tablet_assistant/engine/recommend.py`: su constante y su sitio en `RESEARCH_ORDER`.
4. `osu_tablet_assistant/locales/es/engine.json` y `en/engine.json`: el resumen que se muestra.
5. `python tools/refresh_data.py` para regenerar `data/`, y una línea en [docs/filtros.md](docs/filtros.md).

`tests/test_filters_catalog.py` comprueba que el catálogo sea coherente.

### Enviar un cambio

1. Crea una rama desde `main`.
2. Cada cambio de comportamiento lleva su test; la lógica va en módulos sin Tk para poder probarla.
3. Pasa los tests.
4. Mensajes de commit en español, cortos, diciendo qué cambia.

Normas que no se negocian: nada se aplica a OpenTabletDriver sin copia de seguridad y confirmación, nunca se
instalan plugins ni se activa la predicción por cuenta propia, y lo que no se ha probado con un lápiz y osu!
reales se dice como no verificado.

## Moverse por las versiones

Cada versión tiene una etiqueta (`git tag` las lista). `git log v0.8.1..v0.9.0` muestra qué entró en una
versión y `git checkout v0.5.0` deja el proyecto como estaba. Las etiquetas 0.2 a 0.7 se pusieron después,
en el commit más cercano a cada ejecutable que se fue probando, así que son aproximadas.

## Compilar el .exe

Necesitas PyInstaller (`py -3 -m pip install pyinstaller`; es solo para compilar, el programa no lo usa):

```
powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1
```

Deja `dist\Osu Open Tablet.exe`. Publicar versiones es cosa del mantenedor: ver [docs/RELEASING.md](docs/RELEASING.md).
