# Historial de versiones

Lo que cambió en cada versión, de la más nueva a la más antigua. Cada una tiene su etiqueta en Git
(`git checkout v0.5.0` muestra el proyecto tal como estaba entonces).

<!-- versiones-nuevas-aqui -->

## 0.9.0 (2026-11-01)

- El coach aprende tus hábitos a lo largo de varias partidas (hacia dónde caen tus clics, si te quedas corto
  en los saltos, si el cursor tiembla con el lápiz quieto) y ajusta el área o el suavizado a tu forma de
  jugar, siguiendo reglas sacadas de la comunidad. Lo que aprende se queda en tu PC y se puede borrar.
- El mapa que juegas se reconoce en cuanto empieza, gracias a `osu!.db` (y al almacén de archivos de lazer).
- Después de cada ronda, el replay de osu! sirve para comprobar que el cursor que graba el programa coincide
  con el del juego.

## 0.8.1 (2026-11-01)

- El suavizado tiene cinco niveles; el del medio es el que usan los jugadores con una tableta y un área
  parecidas a las tuyas. Botones de "menos" y "más" suavizado.
- Aviso cuando ya llevas mucho suavizado, y a partir de cierto punto no deja subir más.

## 0.8.0 (2026-10-31)

- Coach: el resumen de rondas muestra solo la primera, la mejor y la última, y cuántas van seguidas sin
  mejorar. La lista completa sigue disponible, plegada.

## 0.7.0 (2026-10-31)

- Coach: si no está claro qué cambiar, prueba un cambio pequeño en el área y en la ronda siguiente decide si
  se queda o se deshace. El área también se puede ajustar a mano.
- Puedes probar los filtros de suavizado que tengas instalados: ves qué hace cada uno y tú decides si se queda.
- Ajuste en vivo más compacto, con atajos que puedes cambiar (con un botón para detectarlos) y pasos Normal,
  Fino y Muy fino. Arreglado un bloqueo de la ventana al abrir el editor de atajos.

## 0.6.0 (2026-10-31)

- Coach: se entera al momento de que cambias de mapa, la barra de botones del informe queda siempre a la
  vista y el área puede cambiar de proporción si lo permites.

## 0.5.0 (2026-10-31)

- Arreglo: la tarjeta "Tus botones de clic" vuelve a su altura al plegarla.
- Los errores muestran el detalle técnico. La documentación pasa a la carpeta `docs/`.

## 0.4.0 (2026-10-31)

- Interfaz nueva, oscura y con paneles redondeados, sin iconos de emoji.
- Puedes elegir con qué botones haces clic (por defecto Z y X; la punta del lápiz solo cuenta si la eliges).
- El mapa de fallos del coach se divide en zonas más pequeñas (6×8) y detecta problemas en zonas concretas.

## 0.3.0 (2026-10-30)

- Llega el coach de partidas: juegas un mapa de verdad, compara tus clics con el mapa y propone un cambio
  por ronda.
- La tabla de pruebas dice en palabras cómo salió cada opción; repetir una prueba suma, no reemplaza.
- Tres caminos al empezar, modo sencillo o avanzado, perfil guardado para no repetir todo, y lectura de
  replays `.osr`.

## 0.2.0 (2026-10-30)

- La prueba de puntería ya no pide clics: el objetivo cuenta al pasar el cursor por encima.
- Interfaz nueva, datos de la comunidad que se pueden actualizar y el primer modo de ajuste en vivo.

## 0.1.0 (2026-10-30)

- Primera versión: asistente guiado que recomienda área y filtros a partir de pruebas cortas con el lápiz.
