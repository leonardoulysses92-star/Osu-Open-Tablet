# Publicar una versión (solo el mantenedor)

`main` es siempre la versión actual; las anteriores quedan como etiquetas de Git y en `CHANGELOG.md`.

## Números de versión

Se escriben `0.G.M.S` y en la interfaz se muestran sin el 0 inicial. El número sube según el tamaño del
cambio:

| Cambio | Opción | Ejemplo | Se muestra |
|---|---|---|---|
| Arreglos y ajustes pequeños | `-Change small` | 0.8.0 → 0.8.0.1 | 8.0.1 |
| Una función o mejora notable | `-Change medium` | 0.8.0.1 → 0.8.1 | 8.1 |
| Un cambio grande | `-Change large` | 0.8.1 → 0.9.0 | 9 |

Con `-Version X.Y.Z` se fija el número a mano.

El proyecto se queda por debajo de 1.0 hasta que el dueño decida otra cosa: `tools/release.ps1` rechaza
cualquier versión 1.x o mayor.

## El script

Con el árbol limpio:

```
powershell -ExecutionPolicy Bypass -File tools\release.ps1 -Change small -Notes "Qué cambia, en una frase"
```

En orden:

1. comprueba que no hay cambios sin commitear, que la etiqueta no existe y que los tests pasan;
2. sube el número en `pyproject.toml`, `osu_tablet_assistant/__init__.py` y los dos README;
3. añade la entrada al `CHANGELOG.md` (debajo de la línea `<!-- versiones-nuevas-aqui -->`), hace el
   commit y crea la etiqueta `vX.Y.Z`;
4. compila el `.exe` con `tools\build_exe.ps1` y lo abre una vez con `--dry-run` para ver que arranca;
5. en la carpeta de destino (`-Dest`, por defecto "Osu Open Tablet" en el Escritorio) mueve el ejecutable
   anterior a "Versiones anteriores" y deja el nuevo como `Osu-Open-Tablet-vX.Y.Z.exe`.

No sube nada. Al final muestra el `git push` que hay que ejecutar y la huella SHA-256 del `.exe`. Para que
alguien pueda descargarlo hay que crear un Release de GitHub para la etiqueta, subir ese archivo y poner la
huella en las notas. El ejecutable no está firmado, así que Windows puede avisar al abrirlo.

`-DryRun` solo muestra lo que haría.
