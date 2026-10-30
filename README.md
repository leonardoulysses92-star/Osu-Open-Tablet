**[Read this in English](README.en.md)**

# osu! Tablet Assistant (osu-open-tablet)

Asistente guiado para jugadores de osu! que te ayuda a configurar [OpenTabletDriver](https://opentabletdriver.net) (OTD): el tamaño y la proporción del área de la tableta, y los filtros de suavizado. Funciona con cualquier tableta que OTD soporte. Mide tu tableta con pruebas cortas en vivo y explica cada recomendación, para que entiendas por qué se sugiere cada cosa.

## Qué hace

El asistente te lleva por ocho pasos, en este orden:

| # | Paso (nombre en la app) | Qué pasa |
|---|---|---|
| 1 | Bienvenida | Lees el aviso y lo aceptas con "Lo entiendo". |
| 2 | OpenTabletDriver | Busca la carpeta de configuración y de programa de OTD, y lee tus perfiles y plugins. |
| 3 | Tableta | Eliges tu perfil de OTD, una tableta de la lista o escribes los datos de la tuya. |
| 4 | Pantalla | Detecta el tamaño del monitor principal. Tú confirmas o escribes los valores. |
| 5 | Sobre ti | Preguntas cortas: estilo de juego, tipo de mapas, prioridad (poca latencia o más suavidad), nivel, y si notas temblor o retraso. |
| 6 | Punto de partida | Dos pruebas (tasa de informes y estabilidad) con tu configuración actual. |
| 7 | Probar ajustes | Aplica cada opción a OTD, la mide con las pruebas de quietud, seguimiento y puntería, y te deja calificarla del 1 al 5. Puedes refinar la mejor. |
| 8 | Resultado | Muestra la configuración recomendada, sus razones y las mediciones. Puedes guardarla como preset, copiar el informe o aplicarla en OTD. |

**Lo que no hace:**

- No garantiza el mejor ajuste posible. Da puntos de partida medidos y explicables. Cada mano y cada tableta son distintas, así que el ajuste final es tuyo.
- Los rangos de tamaño de área y los filtros vienen de valores que usa la comunidad de osu!. Son heurísticos y el código los marca así.
- No cambia nada sin tu confirmación.
- No instala OTD ni plugins, y no recomienda filtros que no tengas instalados.

## Requisitos

| Requisito | Detalle |
|---|---|
| Sistema | Windows 10 u 11 (plataforma principal). Linux y macOS son de soporte *best-effort*: el código tiene rutas para ellos, pero el proyecto está pensado para Windows. |
| Python | 3.10 o superior, con `tkinter` (viene con la biblioteca estándar). No hay que instalar nada con `pip`. Si falta `tkinter`, el programa lo indica y termina. |
| OpenTabletDriver | 0.6.x instalado. El código se revisó contra el código fuente de OTD 0.6.7. |

## Cómo ejecutarlo

Opción 1: haz doble clic en `run.bat`.

Opción 2: desde una terminal, en la carpeta del proyecto:

```
py -3 -m osu_tablet_assistant
```

Las opciones de línea de comandos son:

| Opción | Qué hace |
|---|---|
| `--lang es` o `--lang en` | Idioma de la interfaz. Por defecto `es`. También se puede cambiar dentro de la app. |
| `--config-dir RUTA` | Carpeta de configuración de OTD (la que contiene `settings.json`). Si no la indicas, el asistente la busca. |
| `--install-dir RUTA` | Carpeta del programa de OTD (la que contiene el ejecutable Console o Daemon). Si no la indicas, el asistente la busca. |
| `--dry-run` | Modo simulación: nunca aplica nada a OTD. Solo puedes guardar un preset y el informe. |

`run.bat` pasa las opciones al programa. Por ejemplo: `run.bat --lang en --dry-run`.

## Seguridad

- **Copia de seguridad antes de cambiar algo.** Antes del primer cambio, el asistente guarda una copia de `settings.json` en la carpeta `Backup`. Solo lo hace una vez por sesión.
- **Confirmación explícita.** El asistente te pregunta antes de la primera prueba que aplica ajustes a OTD, y antes de guardar un preset, aplicar el resultado final o restaurar tus ajustes.
- **Modo simulación (`--dry-run`).** No aplica nada a OTD y no hace pruebas por opción. Sirve para revisar la recomendación sin tocar nada.

Qué se escribe y dónde (`<config>` es la carpeta de configuración de OTD):

| Qué | Dónde | Cuándo |
|---|---|---|
| Copia de seguridad de `settings.json` | `<config>\Backup\osu-assistant-settings-AAAAMMDD-HHMMSS.json` | Antes del primer cambio |
| Preset del resultado | `<config>\Presets\osu-assistant <tableta> AAAAMMDD-HHMM.json` | Al pulsar "Guardar como preset" |
| Preset de respaldo | `<config>\Presets\osu-tablet-assistant.json` | Solo si no se pudo aplicar en vivo. Se sobrescribe |
| `settings.json` | `<config>\settings.json` | Solo al pulsar "Aplicar en OpenTabletDriver" |
| Ajustes de prueba | Solo en la memoria del daemon de OTD | Durante las pruebas. No se escriben en el archivo |

Por defecto, `<config>` es `%LOCALAPPDATA%\OpenTabletDriver`. Si OTD está en modo portable, es la carpeta `userdata` junto al programa.

**Cómo deshacer un cambio:**

1. Dentro de la app: "Restaurar mis ajustes" (en Probar ajustes) o "Restaurar mis ajustes originales" (en Resultado). Vuelve a enviar a OTD los ajustes que había al empezar.
2. Cargar un preset: en OpenTabletDriver, abre el menú **File > Presets** y selecciona el preset guardado.
3. Restaurar el archivo a mano: con OTD cerrado, copia la copia de seguridad de `Backup` sobre `settings.json`. La app no tiene una función para esto.

Si cierras la ventana con un ajuste de prueba todavía aplicado en OTD, el asistente te pregunta si quieres restaurarlo antes de salir.

## Tabletas

- La lista integrada tiene **48 tabletas** (`osu_tablet_assistant/knowledge/tablets_builtin.json`). Para cada una están el nombre exacto del perfil de OTD, el área activa en mm, la tasa de informes, la presión máxima y la resolución. Todas están marcadas como verificadas en área, presión y resolución contra los archivos de configuración de OTD. En 26 de ellas, la tasa de informes es una estimación basada en especificaciones del fabricante.
- El nombre de tu perfil de OTD se compara con la lista sin importar mayúsculas, espacios ni signos de puntuación. Si la carpeta de OTD tiene una carpeta `Configurations`, sus definiciones también se leen y reemplazan a las integradas con el mismo nombre.
- Si tu tableta no está en la lista, usa la **entrada manual**: nombre, ancho y alto del área activa en mm, tasa de informes en Hz (el campo empieza en 133 Hz) y presión máxima (opcional).
- Si tu perfil de OTD no aparece en la lista, se muestra con el tamaño desconocido. Escribe el tamaño a mano.

## Filtros que conoce

El catálogo está en `osu_tablet_assistant/knowledge/filters.json`.

| Filtro | Para qué sirve | Se recomienda solo |
|---|---|---|
| Hawku Smoothing | Suavizado con latencia en ms | Sí |
| Devocub Antichatter | Suavizado que actúa más en movimientos lentos | Sí |
| Radial Follow (espacio de tableta) | Suavizado con radio. El cursor no se retrasa más que ese radio | Sí |
| Radial Follow (espacio de pantalla) | Lo mismo, con el radio en píxeles | Sí |
| Temporal Resampler | Interpola la posición para tabletas de tasa baja en monitores rápidos | Sí, según la condición |
| Noise Reduction | Reduce el ruido al tener el lápiz casi quieto | Sí, si hay ruido |
| Kuuube's Chatter Exterminator (smooth y raw) | Quita el chatter (temblor) | Sí, uno de ellos |
| Saturn Multifilter | Antichatter, predicción y otras opciones | No (avanzado) |
| Saturn Area Rounding | Remapea el área alrededor del centro de la pantalla | No (avanzado) |

Los nombres de los plugins que traen cada filtro están en el catálogo `filters.json` y en `otd/plugins.py`.

Reglas:

- **Solo se recomiendan filtros instalados.** El asistente busca los plugins en `<config>\Plugins\` (por nombre de carpeta, nombre del `metadata.json` o nombre de la DLL) y también cuenta los filtros que ya aparecen en tu `settings.json`. Si un filtro no aparece, no se recomienda.
- **"Avanzado" significa que nunca se añade solo.** Solo aparece si ya lo tienes en tu perfil, y entonces se conserva en la opción "Tu configuración actual".
- **La latencia de cada filtro es una estimación** calculada a partir de su algoritmo. No es una medición.

## Cómo funcionan las pruebas en vivo

Cada prueba abre una ventana sin bordes que cubre el monitor elegido. Antes de empezar hay una cuenta regresiva de 3 segundos. Para empezar, haz clic (o toca con el lápiz). Esc cancela.

| Prueba | Duración | Qué pides al jugador | Qué mide |
|---|---|---|---|
| Tasa de informes | 5 s | Dibujar círculos grandes y continuos a velocidad normal de juego | Cuántas posiciones por segundo recibe el cursor |
| Estabilidad | 5 s | Apoyar el lápiz sin moverlo (o mantenerlo cerca, en el aire) | Cuánto tiembla el cursor |
| Seguimiento | 8 s | Seguir un punto que dibuja un ocho a velocidad constante | Retraso, error de trayectoria y suavidad |
| Puntería | 20 círculos | Hacer clic en el centro de cada uno, de uno en uno | Distancia del clic al centro y tiempo hasta el clic |

**Límites de las pruebas:**

- Solo miden el **cursor del sistema**. No ven presión, inclinación ni botones del lápiz.
- El retraso incluye lo que tarda en dibujarse la ventana de prueba, no solo OTD.
- En Windows, el cursor se consulta unas 1500 veces por segundo. En otros sistemas se usan eventos de la ventana, como alternativa de respaldo.
- Si mueves el lápiz muy despacio, la tasa de informes puede salir más baja que la real.
- La puntuación es una heurística. Cada métrica se convierte en una penalización entre 0 y 1, y la prioridad que elegiste decide cuánto pesa cada una. La calificación del 1 al 5 suma o resta hasta 10 puntos.

**El tamaño del área** sale de un rango por estilo de juego, en milímetros de ancho: dedos 40 a 70, muñeca 60 a 100, mixto 75 a 120 y brazo 100 a 160. Luego se ajusta un poco según el tipo de mapas y el nivel, se recorta a lo que cabe en tu tableta y se respeta la proporción de tu pantalla. Son valores heurísticos con confianza baja, pensados como punto de partida. Si prefieres conservar tu tamaño actual, el asistente lo respeta.

**Las opciones que se prueban** son:

| Opción | Qué busca |
|---|---|
| Sin filtros (referencia) | Tu tableta tal cual. Sirve para comparar el resto |
| Baja latencia | El menor retraso añadido posible |
| Equilibrado | Suavizado moderado con poco retraso |
| Suave | El cursor más estable, aceptando algo más de retraso |
| Tu configuración actual | Solo si ya tienes un perfil en OTD, para compararlo |

"Refinar la mejor" hace un ajuste pequeño del parámetro más influyente de la mejor opción. Se detiene cuando no quedan valores nuevos por probar, o tras cuatro pasos por opción.

## Estructura del proyecto

```
osu-open-tablet/
├── run.bat                     lanzador para Windows
├── pyproject.toml
├── docs/ARCHITECTURE.md        diseño y contratos entre módulos
└── osu_tablet_assistant/
    ├── __main__.py             punto de entrada y opciones
    ├── models.py               tipos compartidos
    ├── otd/                    todo lo que toca OTD (configuración, plugins, aplicación)
    ├── knowledge/              tabletas y catálogo de filtros (JSON)
    ├── engine/                 áreas, recomendaciones y métricas
    ├── gui/                    asistente y pruebas en vivo
    └── locales/{es,en}/        textos de la interfaz
tests/                          pruebas (unittest)
```

## Ejecutar las pruebas

Desde la carpeta del proyecto:

```
py -3 -m unittest discover -s tests -t .
```

## Contribuir

- **Agregar una tableta:** añade una entrada en `osu_tablet_assistant/knowledge/tablets_builtin.json`. El campo `name` debe ser el nombre exacto del perfil de OTD. Pon `verified: true` solo si el área, la presión y la resolución se comprobaron contra los archivos de configuración de OTD. Pon `rate_estimated: true` si la tasa de informes viene de especificaciones del fabricante.
- **Agregar un filtro:** el catálogo está en `osu_tablet_assistant/knowledge/filters.json` (ruta de OTD, parámetros con sus límites, fuentes y nivel de confianza). Agregarlo al catálogo no basta para recomendarlo. Hay que incluirlo en `engine/recommend.py`, y si es un plugin conocido, en `KNOWN_PLUGINS` de `otd/plugins.py` para que se detecte como instalado. Sus textos deben existir en `locales/es/engine.json` y `locales/en/engine.json`.
- Lee `docs/ARCHITECTURE.md` antes de tocar el código.

## Estado y limitaciones

El proyecto está en la **versión 0.1.0**, en una etapa temprana.

- Aplicar ajustes a un OTD en ejecución (con `OpenTabletDriver.Console loadsettings` y con la tubería del daemon) está implementado a partir del código fuente de OTD, pero **todavía no está verificado en hardware real**. Si no se puede aplicar, el asistente guarda un preset y te indica cómo cargarlo.
- La tasa de informes de tu tableta la escribes tú. El asistente no la detecta.
- Solo se detecta el monitor principal. Para otro monitor, escribe el tamaño y la posición de su esquina superior izquierda, en píxeles del escritorio.
- El proyecto está pensado para Windows. La captura del cursor de alta frecuencia es específica de Windows.
- Solo se mide el cursor del sistema: sin presión ni inclinación.
- Los rangos de área, las latencias y las puntuaciones son estimaciones y heurísticas.
- La interfaz está en español e inglés.

## Licencia

Este proyecto usa la licencia [MIT](LICENSE).
