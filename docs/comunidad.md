# Datos de la comunidad

`osu_tablet_assistant/knowledge/community_presets.json` reúne 31 configuraciones (datos del 2026-10-30) de jugadores del top y miembros conocidos de la comunidad: perfiles de osu!, entradas de setup.moe escritas por los propios jugadores, Liquipedia y el foro de osu!. El método de recolección está en [research/players.md](research/players.md).

## Qué guarda cada entrada

- Enlace a la fuente.
- Fecha de la fuente, o "sin fecha".
- Fecha de consulta.
- Nivel de confianza:
  - alta: perfil propio o entrada escrita por el jugador;
  - media: wiki, fuente secundaria o entradas con avisos;
  - baja: de oídas o sin verificar.

## Qué se descartó o se marcó

- Se descartaron las entradas que parecen erratas o valores sin tocar: ChocoPafe (radio interior mayor que el exterior) y Exxotl (valores por defecto con una zona muerta de 1 mm).
- Se marcaron, y se muestran como avisos, las que dejan dudas: "posible errata", "valores del formulario", "antigua", "depende de la tableta".
- Los valores aportados por el autor del proyecto (Radial Follow 0,71 / 0,37 / 0,37 / 0,38 / 0,26, área desconocida) están como "valores propios", sin verificar, con confianza baja y sin atribuirse a ningún jugador: la investigación no encontró ninguna fuente pública que los relacione con mrekk.

## Adaptación al área del jugador

Los radios de Radial Follow son milímetros sobre la tableta, así que un valor de otra persona depende de su área. El asistente lo adapta con una heurística:

- `OuterRadius` se escala por la proporción de anchos entre tu área y la de la fuente.
- `InnerRadius` se escala por la proporción de altos. Es lo que hace la calculadora de Mixmg, que usa la mayoría del top.
- `SmoothingCoefficient` solo se escala si la entrada tiene la firma de esa calculadora (interior = coeficiente).
- Knee y leak no se tocan.
- Los valores en píxeles (espacio de pantalla) y en unidades internas de la tableta no se escalan.
- Si no se conoce el área de la fuente, se usan los valores publicados.

Cada ajuste aparece en los motivos. Además, la predicción siempre se desactiva, un valor interno de `Frequency` vuelve a 1000 Hz y los valores fuera de rango se limitan al catálogo, y también se indica.

## Cómo entra en el asistente

- Las 3 configuraciones más parecidas (por tableta, área, confianza y filtros que ya usas) se convierten en candidatas. La que elijas como preset de partida se mide primero.
- El Resultado compara tu configuración con las más parecidas: área, tableta y cada valor que difiere, con fuente, fecha y confianza.

## Actualizar los datos

El botón "Actualizar datos de la comunidad" (Bienvenida y Ajustes) descarga un paquete pequeño de la carpeta `data/` de este repositorio, rama `main`. La dirección se puede cambiar en Ajustes.

- Solo `https`, con tiempo de espera y tamaño máximo por archivo.
- `manifest.json` trae el SHA-256 y el tamaño de cada archivo. Ambos deben coincidir y cada archivo pasa la misma validación estricta que los datos incluidos.
- Un catálogo descargado no puede añadir plugins que esta versión no conoce. Son solo datos, nunca código.
- Un paquete válido se guarda en `%APPDATA%\OsuOpenTablet\data` y se usa desde entonces. Si algo falla, se siguen usando los datos anteriores y, como último recurso, los incluidos.
- La pantalla muestra "Última actualización <fecha> · N configuraciones · M fuentes" y avisa si los datos tienen más de 90 días.
- La comprobación al iniciar está desactivada por defecto (se activa en Ajustes). No hay telemetría: solo se piden esos tres archivos, sin identificadores.

## Mantenimiento

Después de cambiar `knowledge/community_presets.json` (subiendo su `version` y `generated`) o `knowledge/filters.json`:

```
python tools/refresh_data.py
```

y se sube `data/`. Los tests fallan si `data/` no está al día.

## Limitaciones de los datos

- Las entradas de setup.moe las escriben los propios jugadores y no tienen fecha.
- Los rankings cambian cada día y algunas configuraciones pueden ser antiguas.
- La mayoría de las fuentes no registra el orden de los filtros.
- La regla de escalado por área es una heurística, no una regla de los autores de los plugins.
