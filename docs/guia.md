# Guía de uso

## Pasos del asistente

| # | Paso | Qué hace |
|---|---|---|
| 1 | Bienvenida | Aviso inicial, estado de los datos de la comunidad y botón para actualizarlos. |
| 2 | OpenTabletDriver | Busca las carpetas de configuración y de programa de OTD (instalado o portable), lee perfiles y plugins, y avisa si OTD es anterior a la 0.6.8.1. |
| 3 | Tableta | Eliges un perfil de OTD, una tableta de la lista o escribes los datos de la tuya. |
| 4 | Pantalla | Detecta el monitor principal. Confirmas o escribes los valores. |
| 5 | Sobre ti | Estilo de juego, tipo de mapas, prioridad, nivel, si notas temblor o retraso, y la [política de plugins](#política-de-plugins). |
| 5b | Preset de partida | Solo si hay configuraciones de la comunidad utilizables: eliges una como punto de partida (opcional). |
| 6 | Punto de partida | Dos pruebas cortas (tasa de reporte y estabilidad) con tu configuración actual. |
| 7 | Probar ajustes | Aplica cada opción a OTD, la mide y te dice cómo salió en palabras (Excelente, Buena, Normal o Floja). Repetir una prueba acumula las ejecuciones y la hace más fiable; si dos opciones salen casi igual, se marcan como empate. Tu valoración de 1 a 5 es opcional. Puedes refinar la mejor. |
| 8 | Resultado | Configuración recomendada, motivos, mediciones, comparación con la comunidad y, si lo permites, plugins que podrías probar. La guardas, copias el informe o la aplicas en OTD. |

Los valores de la comunidad no se toman como "los mejores": son un candidato más, que se mide como los demás y solo gana si te mide mejor a ti.

## Política de plugins

Se elige en "Sobre ti" y en Ajustes.

| Opción | Efecto |
|---|---|
| Solo ajustar mis filtros actuales | Se conservan tus filtros y su orden. Solo cambian sus valores, de uno en uno, mostrando valor actual → propuesto y el motivo. No existe la opción "sin filtros". |
| Usar cualquier plugin instalado (por defecto) | Elige entre los plugins de la carpeta `Plugins` de OTD. |
| Sugerir también plugins que no tengo | Igual que la anterior, y el Resultado lista los plugins que faltan, con el motivo y dónde conseguirlos. |

Las candidatas, los refinamientos y las configuraciones de la comunidad respetan la política. Una configuración que necesita un plugin que no tienes (o, con "solo ajustar", un filtro que no usas) no se prueba.

## Pruebas en vivo

Cada prueba abre una ventana sin bordes sobre el monitor elegido. Se empieza con clic, toque del lápiz o Espacio/Enter, y sigue una cuenta atrás corta. Esc cancela.

| Prueba | Qué haces | Qué mide |
|---|---|---|
| Tasa de reporte | Dibujas círculos grandes a tu velocidad normal. | Posiciones por segundo que llegan al cursor. |
| Estabilidad | Mantienes el lápiz quieto, apoyado o en el aire. | Cuánto tiembla el cursor. |
| Seguimiento | Sigues un punto que se mueve. | Retraso, error de trayectoria y suavidad. |
| Puntería | Pasas el cursor por cada círculo; cuenta al entrar, sin clic. | Tiempo a cada objetivo, cercanía al centro y pasadas de largo. |

Límites:

- Solo se mide el cursor del sistema, sin presión ni inclinación.
- El retraso incluye el tiempo de dibujo de la ventana de prueba.
- En Windows el cursor se lee unas 1500 veces por segundo.
- La puntuación es una heurística.

## Dónde se guarda cada cosa

En Ajustes eliges tu carpeta de salida (por ejemplo la de un OTD portable). Lo que se guarda es un archivo de configuración completo de OTD, con el mismo formato que un `Osu.json` (`Revision`, `Profiles`, `LockUsableAreaDisplay`, `LockUsableAreaTablet`, `Tools`), que cargas con "Load settings" en OTD.

- El nombre se puede editar al guardar. Por defecto es `<tableta> <fecha hora>`; la plantilla se cambia en Ajustes.
- Si el archivo ya existe, el asistente pregunta y guarda antes una copia con fecha en `<carpeta>\Backup\`.
- Sin carpeta elegida, guarda en la carpeta `Presets` de OTD.

| Qué | Dónde | Cuándo |
|---|---|---|
| Copia de `settings.json` | `<config>\Backup\osu-assistant-settings-AAAAMMDD-HHMMSS.json` | Antes del primer cambio de la sesión |
| Tu configuración guardada | Tu carpeta, o `<config>\Presets\` | Al pulsar "Guardar como preset" |
| Copia de un archivo reemplazado | `<tu carpeta>\Backup\<nombre>-AAAAMMDD-HHMMSS.json` | Antes de reemplazarlo |
| Preset de respaldo | `<config>\Presets\osu-tablet-assistant.json` | Solo si no se pudo aplicar en vivo |
| `settings.json` | `<config>\settings.json` | Solo con "Aplicar a OpenTabletDriver" |
| Ajustes de prueba | Memoria del daemon de OTD | Durante las pruebas; no se escriben a disco |
| Ajustes del asistente y datos descargados | `%APPDATA%\OsuOpenTablet\` | Al cambiar ajustes o actualizar datos |

`%APPDATA%\OsuOpenTablet\config.json` guarda idioma, carpeta, nombre de archivo, política de plugins y actualizaciones. Si se daña, se usan los valores por defecto y la copia dañada queda como `config.corrupt.json`.

OTD portable: además de `%LOCALAPPDATA%\OpenTabletDriver`, se reconocen carpetas con `OpenTabletDriver.Daemon.exe` o `convert_to_portable.bat` junto a un archivo de configuración o una carpeta `userdata`. Si hay varias, el paso 2 deja elegir.

## Confirmaciones y cómo deshacer

Hay que confirmar antes de la primera prueba que aplica ajustes, antes de aplicar el resultado, antes de restaurar y antes de reemplazar un archivo. `--dry-run` nunca aplica nada. El asistente no cierra ni reinicia OTD.

Para deshacer:

1. "Restaurar mi configuración" en la app, o
2. cargar un preset o archivo guardado en OTD, o
3. con OTD cerrado, copiar la copia de `Backup\` encima de `settings.json`.
