# Filtros

El catálogo está en `osu_tablet_assistant/knowledge/filters.json` y se contrastó con el código fuente de los plugins el 2026-10-30 (detalle en [research/filters.md](research/filters.md)).

## Qué recomienda el asistente

| Filtro | Recomendación automática |
|---|---|
| Radial Follow (espacio de tableta y de pantalla), Hawku Smoothing, Devocub Antichatter | Sí |
| Temporal Resampler | Sí, cuando la pantalla refresca bastante más rápido de lo que reporta la tableta |
| Hawku Noise Reduction, Kuuube's Chatter Exterminator | Sí, si hay temblor |
| Reconstructor, Spectral Reconstructor, Saturn Multifilter / HRRNC, Bezier Interpolator | No en las opciones estándar (avanzados). Se usan desde configuraciones de la comunidad o desde tu propio perfil |
| Frenet-Serret predictor, ScuffedInter/Extrapolator, Hover Distance Limiter, Saturn Area Rounding | Nunca. Solo decides tú |

Sobre el orden: en OTD 0.6 el orden de la lista `Filters` es el orden de ejecución, así que el asistente nunca reordena la cadena que ya tienes. En una cadena nueva se sigue la investigación: reducción de ruido primero, luego suavizado, luego remuestreo.

Cambios de la última revisión del catálogo:

- `SmoothingCoefficient` admite 0,995.
- Se quitaron las propiedades eliminadas de Saturn (`dacOuter`, `msOverride`).
- La `Frequency` de Saturn ya no se fuerza a la tasa de reporte, porque no está claro que corresponda.

## Normas de osu! sobre predicción

El 2026-10-14 osu! dijo que OTD y los plugins de suavizado y antitemblor están bien, pero que los plugins que asisten la entrada de una forma que "cualquier persona normal consideraría trampa" llevarán a sanciones. No nombró ningún plugin ([tema del foro](https://osu.ppy.sh/community/forums/topics/2254092)).

Por eso:

- Los plugins de predicción (Frenet-Serret predictor, ScuffedInter/Extrapolator) y los filtros que no son de suavizado (Hover Distance Limiter, Saturn Area Rounding) no se eligen nunca automáticamente. Solo los eliges tú, con una nota.
- La predicción de Devocub no se activa.
- No se sabe si cuenta el ajuste de predicción de Temporal Resampler o de Saturn (`frameShift`). Cuando una opción lo usa, los motivos lo dicen.
