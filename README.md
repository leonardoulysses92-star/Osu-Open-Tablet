**[Read this in English](README.en.md)**

# osu! Tablet Assistant (osu-open-tablet)

Asistente guiado para jugadores de osu! que te ayuda a configurar [OpenTabletDriver](https://opentabletdriver.net) (OTD): el tamaño y la proporción del área de la tableta, y los filtros de suavizado. Funciona con cualquier tableta que OTD soporte. Mide tu tableta con pruebas cortas en vivo, compara el resultado con configuraciones publicadas por jugadores top y la comunidad (con sus fuentes) y explica cada recomendación.

## Qué hace

El asistente te lleva por estos pasos:

| # | Paso (nombre en la app) | Qué pasa |
|---|---|---|
| 1 | Bienvenida | Lees el aviso y lo aceptas. Aquí también ves el estado de los datos de la comunidad y el botón "Actualizar datos de la comunidad". |
| 2 | OpenTabletDriver | Busca las carpetas de configuración y del programa de OTD (instalado o **portable**), lee tus perfiles y plugins y te avisa si tu OTD es anterior a la 0.6.8.1. |
| 3 | Tableta | Eliges tu perfil de OTD, una tableta de la lista o escribes los datos de la tuya. |
| 4 | Pantalla | Detecta el tamaño del monitor principal. Lo confirmas o escribes los valores. |
| 5 | Sobre ti | Estilo de juego, tipo de mapas, prioridad, nivel, temblor o retraso, y **qué plugins puede usar el asistente** (ver "Política de plugins"). |
| 5b | Preset de partida | Solo si hay configuraciones de la comunidad utilizables: eliges una como punto de partida (opcional). |
| 6 | Punto de partida | Dos pruebas cortas (tasa de reporte y estabilidad) con tu configuración actual. |
| 7 | Probar ajustes | Aplica cada opción a OTD, la mide (estabilidad, seguimiento, puntería) y te deja puntuarla de 1 a 5. Puedes refinar la mejor. |
| 8 | Resultado | La configuración recomendada, los motivos, las mediciones, la **comparación con configuraciones de la comunidad** y, si lo permites, **plugins que podrías probar**. La guardas en tu carpeta, copias el informe o la aplicas en OTD. |

**Lo que no hace:**

- No garantiza el mejor ajuste posible. Da puntos de partida medidos y explicables.
- Los valores de la comunidad no son "lo mejor": son un candidato más, que se mide como los demás y solo gana si te mide mejor a ti.
- No cambia nada sin tu confirmación y **nunca instala plugins** (solo te dice cuál, por qué, dónde encontrarlo y cómo instalarlo tú).
- Nunca activa la predicción por su cuenta (ver "Normas de osu!").

## Requisitos

| Requisito | Detalle |
|---|---|
| Sistema | Windows 10 u 11 (el objetivo principal). Linux y macOS, sin garantías. |
| Python | 3.10 o superior con `tkinter` (biblioteca estándar). No hace falta instalar nada con `pip`. |
| OpenTabletDriver | 0.6.x, instalado o portable. Se recomienda la **0.6.8.1** o posterior si usas interpoladores en Windows (la 0.6.8.1 corrige un cierre del daemon de la 0.6.8 con filtros asíncronos). Con la 0.6.7 el asistente muestra un aviso; nunca te obliga a actualizar. |

## Cómo ejecutarlo

Doble clic en `run.bat`, o desde una terminal en la carpeta del proyecto:

```
py -3 -m osu_tablet_assistant
```

| Opción | Qué hace |
|---|---|
| `--lang es` o `--lang en` | Idioma de la interfaz. Sin ella se usa el último idioma que elegiste (español la primera vez). |
| `--config-dir RUTA` | Carpeta de configuración de OTD (la que tiene `settings.json`). |
| `--install-dir RUTA` | Carpeta del programa OTD (la que tiene el ejecutable Console o Daemon). |
| `--dry-run` | Nunca aplica nada a OTD. Solo puedes guardar el archivo y el informe. |

## Datos de la comunidad y fuentes

`osu_tablet_assistant/knowledge/community_presets.json` reúne **31 configuraciones** (datos del 2026-10-30) de jugadores del top y miembros conocidos de la comunidad: perfiles de osu!, entradas de setup.moe escritas por los propios jugadores, Liquipedia y el foro de osu!. Cada entrada guarda **el enlace a la fuente, la fecha de la fuente (o "sin fecha"), la fecha de consulta y un nivel de confianza** (alta = perfil propio o entrada escrita por el jugador; media = wiki/fuente secundaria o entradas con avisos; baja = de oídas o sin verificar). El método completo está en `docs/research/players.md`.

- Las entradas que parecen erratas o valores sin tocar se **descartaron** (ChocoPafe: radio interior mayor que el exterior; Exxotl: valores por defecto con una zona muerta de 1 mm) o se **marcaron** (por ejemplo "posible errata", "valores del formulario", "antigua", "depende de la tableta"). Las marcas se muestran como avisos.
- Los valores que aportaste (Radial Follow 0,71 / 0,37 / 0,37 / 0,38 / 0,26, área desconocida) están incluidos como **"tus propios valores", sin verificar, confianza baja y sin atribuirlos a ningún jugador**: la investigación no encontró ninguna fuente pública que los relacione con mrekk.
- **Adaptación a tu área (heurística):** los radios de Radial Follow son milímetros sobre la tableta. El asistente escala `OuterRadius` por la proporción de anchos y `InnerRadius` por la de altos entre tu área y la de la fuente (así los calcula la popular calculadora de Mixmg, que usa la mayoría del top). `SmoothingCoefficient` solo se escala cuando la entrada tiene la firma de la calculadora (interior = coeficiente). Knee y leak se mantienen. Los valores en píxeles (espacio de pantalla) y en unidades internas de la tableta no se escalan. Si no se conoce el área de la fuente, se usan los valores publicados. Cada ajuste aparece en los motivos.
- La predicción siempre se desactiva, un valor interno de `Frequency` vuelve a 1000 Hz y los valores fuera de rango se limitan al catálogo (y también se indica).
- Las 3 configuraciones de la comunidad más parecidas (por tableta, área, confianza y filtros que ya usas) se convierten en candidatas; la que elijas como preset de partida se mide primero.
- La página de Resultado compara tu resultado con las configuraciones más parecidas: área, tableta y cada valor que difiere, con fuente, fecha y confianza.

## Mantener los datos al día

El botón **"Actualizar datos de la comunidad"** (Bienvenida y Ajustes) descarga un pequeño paquete de la página de GitHub del proyecto (carpeta `data/` de `leonardoulysses92-star/Osu-Open-Tablet`, rama `main`; la dirección se puede cambiar en Ajustes):

- Solo direcciones `https`, con tiempo de espera y tamaño máximo por archivo.
- `manifest.json` indica el SHA-256 y el tamaño de cada archivo; ambos deben coincidir y los dos archivos deben pasar la misma validación estricta que los datos incluidos. Un catálogo descargado no puede añadir plugins que esta versión no conoce: **son solo datos, nunca código**.
- Un paquete válido se guarda en `%APPDATA%\OsuOpenTablet\data` y se usa a partir de entonces. Si algo falla, se siguen usando los datos que ya tenías (y como último recurso los incluidos).
- La pantalla muestra "Última actualización <fecha> · N configuraciones · M fuentes" y avisa cuando los datos tienen más de **90 días**.
- La comprobación al iniciar está **desactivada** por defecto; actívala en Ajustes si la quieres. No hay telemetría: solo se piden esos tres archivos, sin identificadores.

Para mantenedores: después de cambiar `knowledge/community_presets.json` (sube su `version` y `generated`) o `knowledge/filters.json`, ejecuta `python tools/refresh_data.py` y sube `data/`. Los tests fallan si `data/` no está al día.

## Política de plugins

En "Sobre ti" (y en Ajustes) eliges qué plugins puede usar el asistente:

| Opción | Qué significa |
|---|---|
| **Solo ajustar mis filtros actuales** | Tus filtros y su orden se mantienen. Solo cambian sus valores, **de uno en uno**, cada uno mostrado como valor actual -> valor propuesto con el motivo. No hay opción "sin filtros". |
| **Usar cualquier plugin que ya tenga instalado** (por defecto) | Elige entre los plugins que hay en la carpeta `Plugins` de OTD. |
| **Sugerir también plugins que aún no tengo** | Como la anterior, y la página de Resultado lista los plugins que te faltan con el motivo, dónde encontrarlos y cómo instalarlos tú. |

Las candidatas, los refinamientos y las configuraciones de la comunidad respetan la política: una configuración de la comunidad que necesita un plugin que no tienes (o, con "solo ajustar", un filtro que no usas) no se prueba.

## Normas de osu! (predicción y asistencia de entrada)

El 2026-10-14 osu! dijo que OTD y los plugins de suavizado / antitemblor están bien, pero que los plugins que asisten la entrada de una forma que "cualquier persona normal consideraría trampa" llevarán a sanciones. No nombró ningún plugin ([tema del foro](https://osu.ppy.sh/community/forums/topics/2254092)). Por eso:

- Los plugins de predicción (Frenet-Serret predictor, ScuffedInter/Extrapolator) y los filtros que no son de suavizado (Hover Distance Limiter, Saturn Area Rounding) **nunca se eligen automáticamente**: solo decides tú, con una nota explicativa.
- La predicción de Devocub nunca se activa.
- **No se sabe** si cuenta el ajuste de predicción de Temporal Resampler o Saturn (`frameShift`); cuando una opción lo usa, los motivos lo dicen.

## Dónde se guardan los archivos

En **Ajustes** eliges tu carpeta (por ejemplo `C:\Users\<tú>\Documents\opentablet`, donde vive un OTD portable). Lo que guarda el asistente es un **archivo de configuración completo de OTD**, el mismo formato que un `Osu.json` (`Revision`, `Profiles`, `LockUsableAreaDisplay`, `LockUsableAreaTablet`, `Tools`), que cargas con "Load settings" de OTD.

- Al guardar puedes editar el nombre; por defecto "`<tableta> <fecha hora>`" (la plantilla se cambia en Ajustes).
- Si el archivo ya existe (por ejemplo `Osu.json`), el asistente pregunta antes y, antes de reemplazarlo, guarda una copia con fecha en `<carpeta>\Backup\`.
- Si no eliges carpeta, guarda en la carpeta `Presets` de OTD, como antes.
- Los ajustes del propio asistente (idioma, carpeta, nombre de archivo, política de plugins, actualizaciones) están en `%APPDATA%\OsuOpenTablet\config.json`. Si ese archivo se daña, se usan los valores por defecto y la copia dañada se conserva como `config.corrupt.json`.

**OTD portable:** además de `%LOCALAPPDATA%\OpenTabletDriver`, el asistente reconoce carpetas portables (con `OpenTabletDriver.Daemon.exe` o `convert_to_portable.bat` junto a un archivo de configuración o una carpeta `userdata`). Si encuentra varias, el paso OpenTabletDriver te deja elegir.

## Seguridad

- **Copia de seguridad antes de cualquier cambio** en la configuración de OTD: `<config>\Backup\osu-assistant-settings-AAAAMMDD-HHMMSS.json`, una vez por sesión.
- **Confirmación explícita** antes de la primera prueba que aplica ajustes, antes de aplicar el resultado, antes de restaurar y antes de reemplazar un archivo.
- **Modo de prueba** (`--dry-run`): nunca aplica nada.
- El asistente nunca cierra ni reinicia OTD.

| Qué | Dónde | Cuándo |
|---|---|---|
| Copia de `settings.json` | `<config>\Backup\` | Antes del primer cambio |
| Tu configuración guardada | Tu carpeta (o `<config>\Presets\`) | Al pulsar "Guardar como preset" |
| Copia de un archivo reemplazado | `<tu carpeta>\Backup\<nombre>-AAAAMMDD-HHMMSS.json` | Antes de reemplazarlo |
| Preset de respaldo | `<config>\Presets\osu-tablet-assistant.json` | Solo si no se pudo aplicar en vivo |
| `settings.json` | `<config>\settings.json` | Solo con "Aplicar a OpenTabletDriver" |
| Ajustes de prueba | Solo en la memoria del daemon de OTD | Durante las pruebas |
| Ajustes del asistente y datos descargados | `%APPDATA%\OsuOpenTablet\` | Al cambiar ajustes o actualizar los datos |

**Cómo deshacer:** "Restaurar mi configuración" en la app; o cargar un preset/archivo en OTD; o, con OTD cerrado, copiar la copia de seguridad encima de `settings.json`.

## Filtros que conoce

El catálogo (`osu_tablet_assistant/knowledge/filters.json`) se revisó contra el código fuente de los plugins el 2026-10-30 (`docs/research/filters.md`).

| Filtro | Se recomienda automáticamente |
|---|---|
| Radial Follow (espacio de tableta y de pantalla), Hawku Smoothing, Devocub Antichatter | Sí |
| Temporal Resampler | Sí, cuando la pantalla refresca mucho más rápido de lo que reporta la tableta |
| Hawku Noise Reduction, Kuuube's Chatter Exterminator | Sí, si hay temblor |
| Reconstructor, Spectral Reconstructor, Saturn Multifilter / HRRNC, Bezier Interpolator | No en las opciones estándar (avanzados); se usan vía configuraciones de la comunidad o tu propio perfil |
| Frenet-Serret predictor, ScuffedInter/Extrapolator, Hover Distance Limiter, Saturn Area Rounding | **Nunca** (solo decides tú) |

Correcciones de la revisión: `SmoothingCoefficient` ya admite 0,995; se quitaron las propiedades eliminadas de Saturn (`dacOuter`, `msOverride`); la `Frequency` de Saturn ya no se fuerza a la tasa de reporte (dudoso); y el orden de una cadena nueva sigue la investigación (ruido primero, luego suavizado, luego remuestreo). En OTD 0.6 el orden de la lista `Filters` es el orden de ejecución, así que el asistente **nunca reordena tu cadena** por etapa.

## Cómo funcionan las pruebas en vivo

Cada prueba abre una ventana sin bordes sobre el monitor elegido. Para empezar: clic, toque con el lápiz o Espacio/Enter; sigue una cuenta atrás corta. Esc cancela.

| Prueba | Qué haces | Qué mide |
|---|---|---|
| Tasa de reporte | Dibuja círculos grandes a tu velocidad normal | Posiciones por segundo que llegan al cursor |
| Estabilidad | Mantén el lápiz quieto, apoyado o en el aire | Cuánto tiembla el cursor |
| Seguimiento | Sigue un punto que se mueve | Retraso, error de trayectoria y suavidad |
| Puntería | Muévete sobre cada círculo: **cuenta en cuanto el cursor entra, sin clic** (funciona en el aire) | Tiempo a cada objetivo, cercanía al centro, pasada de largo |

Límites: solo se mide el cursor del sistema (sin presión ni inclinación); el retraso incluye el tiempo de dibujo de la ventana; en Windows el cursor se lee unas 1500 veces por segundo; la puntuación es una heurística.

## Estructura del proyecto

```
osu-open-tablet/
├── run.bat, build_exe.ps1
├── data/                       paquete de datos de la comunidad para el botón "Actualizar" (generado)
├── tools/refresh_data.py       regenera data/ (manifest + sumas de verificación)
├── docs/                       ARCHITECTURE.md y research/
└── osu_tablet_assistant/
    ├── appconfig.py            ajustes del propio asistente (%APPDATA%\OsuOpenTablet)
    ├── otd/                    todo lo que toca OTD
    ├── knowledge/              tabletas, catálogo de filtros, presets de la comunidad, actualizador
    ├── engine/                 área, recomendaciones, presets de la comunidad, métricas
    ├── gui/                    el asistente y las pruebas en vivo
    └── locales/{es,en}/        textos de la interfaz
tests/                          tests unitarios (unittest)
```

## Tests y .exe

```
py -3 -m unittest discover -s tests
powershell -ExecutionPolicy Bypass -File build_exe.ps1
```

`build_exe.ps1` incluye automáticamente todos los módulos y todos los archivos de datos del paquete (los módulos y JSON nuevos entran sin editarlo).

## Estado y limitaciones (con honestidad)

- Versión 0.1.x, fase temprana. Aplicar en vivo (Console y pipe del daemon) está implementado a partir del código de OTD pero **no verificado con hardware real**.
- La regla de escalado por área de los valores de la comunidad es una heurística (basada en cómo funciona la calculadora de Mixmg), no una regla de los autores de los plugins.
- Las entradas de setup.moe las escriben los propios jugadores y no tienen fecha; los rankings cambian cada día; algunas configuraciones pueden ser antiguas. La mayoría de fuentes no registra el orden de los filtros.
- No está resuelto si los ajustes de predicción cuentan como asistencia de entrada para osu!.
- La tasa de reporte de tu tableta la escribes tú. Solo se detecta automáticamente el monitor principal.
- Interfaz en español e inglés.

## Licencia

[MIT](LICENSE).
