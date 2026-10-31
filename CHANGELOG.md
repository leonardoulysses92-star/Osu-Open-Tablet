# Historial de versiones

La versión actual es la que está en `main`. Cada versión anterior queda **separada** como una etiqueta de Git
(`v0.1.0`, `v0.2.0`...): `git checkout v0.5.0` muestra el proyecto tal como estaba entonces, y
`git tag` las lista todas. El `main` no guarda copias de versiones viejas ni ejecutables.

Cómo se publica una versión nueva (el script archiva la anterior y actualiza el proyecto): ver
[CONTRIBUTING.md](CONTRIBUTING.md#publicar-una-versión).

Aviso: los números 0.2 a 0.7 se asignaron a los ejecutables que se fueron probando; cada etiqueta apunta al
commit más cercano del que salió ese ejecutable, así que son aproximadas. El número interno del paquete se
unificó en 0.8.0.

<!-- versiones-nuevas-aqui -->

## 0.8.0 (2026-10-31)

- Coach de partidas: la comparación entre rondas pasa a un resumen compacto (primera, mejor y última ronda con
  datos), un contador de rondas seguidas sin mejora y la lista completa en un desplegable.
- El número de versión del paquete y de los README queda unificado.

## 0.7.0 (2026-10-31)

- Coach: el área se ajusta de verdad. Si la evidencia no es clara, propone pruebas pequeñas y explicadas
  que se conservan o se devuelven según la ronda siguiente. Ajuste del área a mano y área actual en números.
- Suavizado opcional: el jugador decide qué filtros probar, ve qué hacen y los deja o los quita.
- Modo acompañante compacto con atajos configurables (botón "Atajos" con detección) y pasos Normal, Fino y
  Muy fino. Corrige un bloqueo de la ventana al abrir el editor de atajos.

## 0.6.0 (2026-10-31)

- Coach: reconoce el cambio de mapa al momento y lo muestra; barra de acciones fija en el informe;
  proporción libre del área (opcional).

## 0.5.0 (2026-10-31)

- Corrección: la tarjeta "Tus botones de clic" recupera su altura al plegarse.

## 0.4.0 (2026-10-31)

- Diseño "cristal oscuro": paneles redondeados y suavizados, sin iconos de emoji.
- Botones de clic configurables (por defecto Z y X; la punta del lápiz no cuenta salvo que se elija).
- Análisis por zonas más finas (6×8 por defecto) con detección de zonas pequeñas.

## 0.3.0 (2026-10-30)

- Coach de partidas: observa una partida real, alinea los clics con el mapa y propone un cambio por ronda.
- Tabla de pruebas con el resultado en palabras; las repeticiones se acumulan en vez de reemplazarse.
- Tres caminos de entrada, modo Simple/Avanzado, perfil guardado y lectura de repeticiones `.osr`.
- Documentación reorganizada en `docs/` y `build_exe.ps1` para compilar el `.exe`.

## 0.2.0 (2026-10-30)

- Prueba de puntería por "hover" (el objetivo se activa al tocarlo, sin clics), interfaz nueva,
  datos de la comunidad actualizables y modo acompañante.

## 0.1.0 (2026-10-30)

- Primer asistente guiado: recomendación de área y filtros, pruebas del lápiz, interfaz, README es/en y licencia MIT.
