# Design: "calm dark glass"

**[Leer en español abajo](#en-español)**

The interface aims to be professional, simple and calm: one primary action per screen, rounded
glass-like panels on a deep graphite-blue ground, one accent colour used sparingly, no decorative
icons or filler text. Real screenshots (100 %, 125 % and 150 % scaling, Spanish and English) are in
[`docs/screenshots`](screenshots); regenerate them with `py -3 tools/screenshots.py es`
(or `en 1.25`, `es 1.5`).

## Direction

| Aspect | Choice |
|---|---|
| Ground | `#0e1219`, flat (see "Limits" for why there is no gradient) |
| Panels | frosted-glass emulation: fill = ground blended 5 % toward a cool white, 1 px hairline border (12 %), a brighter 1 px line along the top edge (21 %), 14 px corner radius |
| Controls | rounded (8 px buttons, 7 px fields, 9 px segmented tracks, pill chips), antialiased |
| Accent | osu! pink `#ff66aa`, only for the primary button, selected states, the focus ring, the current step and a few labels |
| Semantic colours | muted green / amber / red / blue, used for notices, chips and grades |
| Type | Segoe UI Variable (Display Semibold for titles, Text / Text Semibold for the rest); falls back to Segoe UI Semibold / Segoe UI, then to the Tk default font. Scale: display 21 pt, title 16, heading 12, body 10, small 9 |
| Spacing | 4/8 px grid (`SPACE`), 16 px between cards, 18 px card padding |
| Motion | none beyond Tk's own hover / pressed state changes (instant) |
| Icons | none as text glyphs; the few that carry meaning (notice kinds, check marks, chevrons, the "best option" dot, the logo) are drawn as small antialiased bitmaps |
| Title bar | dark on Windows 10 20H1+ / 11 (`DwmSetWindowAttribute`, attribute 20, falling back to 19; best effort, no-op elsewhere) |

## Tokens: where to tune the look

* **Colours** — `osu_tablet_assistant/gui/palette.py`. Change `GROUND`, `TINT` (the cool white the
  glass is blended toward), `ACCENT`, or the `GLASS` blend amounts and every derived colour follows.
  The semantic colours and text colours are plain fields of `Colors`.
* **Sizes and type** — the tokens block at the top of `osu_tablet_assistant/gui/widgets.py`:
  `SPACE`, `RADIUS`, `FONT_ROLES` (size and weight per role), `FONT_FACES` (preferred families per
  role) and `LABEL_ROLES`.
* `tests/test_design.py` checks the WCAG contrast of every text/background pair actually used
  (reading text >= 4.5:1, disabled text visible but distinct), so a palette change that hurts
  legibility fails the tests.

Current ratios (WCAG 2.x): body text 14.4:1 on a card, secondary text 7.2:1 on a card and 5.6:1 on
the hover colour, accent text 6.2:1 on a card, dark text on the pink button 6.9:1, chips and notices
>= 4.75:1. Disabled text is 3.6:1 on a card on purpose (visible, clearly not active).

## How the glass emulation works

Tk has **no real transparency and no backdrop blur**: every widget is an opaque rectangle. So the
"glass" is an emulation with blended opaque colours, not a blur of what is behind the window.

1. `gui/raster.py` (pure Python, no Tk) describes each shape with a signed distance function and
   gives every pixel the coverage `clamp(0.5 - distance, 0, 1)`. The shape's colours (focus ring,
   hairline with its top highlight, fill) are blended over the **known colour of the surface the
   shape sits on**. Results are memoised.
2. `gui/widgets.py` turns those pixels into `tk.PhotoImage`s and registers them as **ttk image
   elements with a nine-slice border**. Only a compact image (corners plus a 1 px centre) is computed
   in Python; Tk widens the centre with `photo copy -to` and Tk itself stretches the element when a
   widget is resized, so resizing a card or a button runs no Python code and cannot flicker.
3. Because the corner pixels must match what is under them, styles exist once per *surface* (the
   page, a card, an input field, each notice colour). Widgets find their surface by walking up their
   parents (`surface_prefix`), `button()` / `check()` / `radio()` / `label()` pick the right variant,
   and the Tk option database (`*GlassCard*TEntry.style`...) gives plain `ttk.Entry`, `Combobox`,
   `Scrollbar`... created inside a card the card variant automatically.
4. Canvas drawings (step indicator, before/after bars, area preview, tablet map) use the same
   bitmaps: `canvas_round_rect` places four antialiased corner images and fills the straight parts
   with canvas rectangles; `canvas_pill` does the same for bars.
5. Tooltips are rounded by making a colour key transparent on Windows (`-transparentcolor`); their
   antialiased edge is blended against the window background, so it looks right over this app and
   slightly hard over other windows.

The reusable primitive is `RoundedFrame` (alias `GlassCard`): an ordinary `ttk.Frame` you pack or
grid into, with `set_selected()` (2 px accent border) and `set_hover()`. `Card` adds the title row
and `body`. The public helpers other modules used before (`COLORS`, `Card`, `Chip`, `button`,
`label`, `WrapLabel`, `Expander`, `Notice`, `Tooltip`, `Columns`, `bullets`, `check`, `frame`, `px`,
`font`, `run_in_thread`, `apply_theme`...) keep their names and parameters.

## Limits (honest)

* No blur, no real translucency and no Mica/acrylic: Windows' system backdrop would not show
  through opaque Tk widgets, so it is not attempted.
* No background gradient: Tk labels and frames paint opaque rectangles, so a gradient behind the
  pages would show seams around every label. The ground is flat; depth comes from the glass layers.
* Shapes drawn with plain canvas polygons (the rotated active area, heat cells, dashed outlines and
  the arrows of the tablet map, the live-test targets) are still not antialiased; the live tests
  only took the palette (their geometry and timing are untouched).
* Treeview rows, `tk.Text` and native dialogs (file pickers, yes/no questions) are drawn by Tk or
  Windows and keep their own shapes.
* The companion overlay stays rectangular (it is click-through and shown over the game, where the
  colour under its corners is unknown); it got the glass colours and a thin accent hairline.
* Images are built per Tcl interpreter at start-up (about 25-70 ms) and cached; the pixel maths is
  cached across windows.
* Resizing the main window redraws more than before (bitmaps instead of flat fills). Measured on
  the development machine (full window resize + redraw, median of 30 steps): welcome page about
  130 ms (90 ms before), testing page about 150 ms (100 ms before); no flicker was seen. The
  whole test suite went from about 46 s to about 47-48 s.

## Mejoras pendientes / Pending improvements

In priority order; each item is one actionable step.

1. Resize cost: main-window resize is ~1.4-1.5x slower than the flat theme; profile Tk's image-element redraw per widget and try larger tiles for wide fields/buttons or flat fills for the centre of controls.
2. Coach report window: its content is taller than the 640x680 window and gets cut at the bottom (screenshot 18); put the coach body in a `ScrollFrame` (this was already so before the redesign).
3. Result page: the area-preview label "80.0 mm" sits close to the bezel when the area is near the tablet edge; give the size labels a little more room in `AreaPreview`.
4. Tablet map and live tests: rotated areas, heat cells, arrows and live-test targets are still hard-edged canvas polygons; draw them with antialiased bitmaps where it does not touch test geometry or timing.
5. Companion overlay and coach mini-map: still rectangular and not captured in screenshots (click-through windows); verify them in a real session and consider rounded corners via a colour key.
6. Tooltips over other applications: the antialiased edge is blended against this app's background, so it looks slightly hard over light windows; acceptable, but could use a per-pixel alpha layered window via ctypes.
7. Filter tree (result page): the expand indicator is still clam's triangle; replace with the chevron icons for consistency.
8. "Advanced mode" toggle: it is a one-segment control; a real switch (track + knob bitmap) would read more clearly as on/off.
9. Font check: Segoe UI Variable semibold faces were verified on Windows 11 only; check the fallback (Segoe UI / Tk default) on Windows 10 and Linux and take screenshots there.
10. Keyboard focus ring: drawn for buttons, segments, fields, checks and radios; not verified by screenshot (needs a Tab-driven capture in `tools/screenshots.py`).
11. High-contrast mode / Windows light theme are not handled (the app is dark only).

---

## En español

Interfaz tranquila y profesional: paneles redondeados tipo cristal sobre un fondo gris azulado
oscuro, un solo color de acento (rosa osu!) usado con moderación, sin iconos decorativos ni textos de
relleno. Las capturas reales están en [`docs/screenshots`](screenshots).

* **Dónde ajustar**: colores en `gui/palette.py` (`GROUND`, `TINT`, `ACCENT`, `GLASS`); tamaños,
  radios y tipografía al principio de `gui/widgets.py` (`SPACE`, `RADIUS`, `FONT_ROLES`,
  `FONT_FACES`). `tests/test_design.py` comprueba el contraste WCAG de cada combinación.
* **Cómo se emula el cristal**: Tk no tiene transparencia ni desenfoque. Las formas se calculan en
  Python puro (`gui/raster.py`) con bordes suavizados, mezcladas con el color conocido de la
  superficie que hay debajo, y Tk las estira como imágenes de nueve partes: redimensionar no ejecuta
  Python y no parpadea.
* **Límites**: no hay desenfoque real, ni Mica/acrílico, ni degradado de fondo (se verían cortes
  alrededor de cada etiqueta); los polígonos del mapa y las pruebas en vivo siguen sin suavizado;
  los diálogos nativos de Windows conservan su aspecto.
