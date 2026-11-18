# Pantalla: foto del perro al iniciar el paseo (paseador)

Repo: `cantrack-frontend`. Trabaja en una rama y abre PR, no push directo a `main`.
Código, nombres y tests en inglés; los textos de la UI en español.

## Qué es y por qué

Al pulsar **Iniciar paseo**, el paseador puede tomar una foto del perro. El backend la compara con las fotos de referencia que subió el dueño (CLIP) y dice si es el mismo perro. **La IA solo aconseja: el paseo siempre empieza**, pase lo que pase con la foto. Esta pantalla es la parte visible de la IA del proyecto, así que tiene que verse bien en la demo.

## Contrato del backend (ya en `main`, PR cantrack-backend#20)

`POST /bidding/requests/{id}/check-in`, multipart opcional, campo `image` (máx. 5 MB).

- Sin foto: igual que antes, `{id, status: "in_walk", walkerId}`.
- Con foto: lo mismo más `ai: { match, similarity }`:

| `ai.match` | Significa | Qué mostrar |
|---|---|---|
| `match` | Es el perro (similitud ≥ 0.88) | Confirmación verde: "Es {dog} ✓" |
| `uncertain` | No estamos seguros | Aviso amarillo: "No estamos seguros de que sea {dog}. Confírmalo a ojo." + botón "Sí, es {dog}" (solo cierra el aviso; el paseo ya empezó) |
| `unknown` | El perro aún no tiene fotos de referencia | Nota neutra: "{dog} no tiene fotos de referencia todavía." |
| `unavailable` | Falló el modelo o la foto no se pudo leer | No mostrar error; seguir como si nada |

`similarity` es un número 0..1 o `null`. No lo muestres como porcentaje de "certeza" (es un coseno, no una probabilidad); como mucho un detalle pequeño.

## Qué cambiar

1. `src/lib/marketplace.ts` (~línea 844): `checkInWalk(requestId, photo?: Blob)`. Si hay foto, `FormData` con `image` (mismo patrón que `checkIn` en `src/lib/api.ts` ~335, sin poner `Content-Type` a mano). Extiende `WalkTransition` con `ai?: { match: "match" | "uncertain" | "unknown" | "unavailable"; similarity: number | null }`.
2. `src/walker/WalkerRoutesScreen.tsx`: `startWalk` (~línea 647) hoy llama directo a `checkInWalk`. Cambia a una hoja (sheet) con:
   - input de archivo con `accept="image/*"` y `capture="environment"` (patrón en `src/owner/OwnerDogsScreen.tsx` ~628);
   - reduce la foto antes de subirla con `resizeImage` de `src/lib/image.ts`;
   - botón principal "Iniciar paseo" (con foto) y botón secundario "Iniciar sin foto".
3. Estados: mientras sube, `DogLoader` y botones deshabilitados (como `busyId`). Si la petición falla por red, el mensaje de error existente (`wg__warn`); el paseo **no** debe quedar a medias en la UI.
4. Tras responder: muestra el resultado según la tabla y luego el aviso normal "Paseo con {dog} iniciado…" que ya existe. Si `ai` no viene, no muestres nada de IA.
5. Reusa los componentes y tokens de glass del propio paseador (`wg__primary`, `wg__ghost`, `wg__chip`, `Confirm`); no inventes valores nuevos de color o tipografía.

## Criterios de listo (binarios)

- [ ] Con foto y `match: "match"` se ve la confirmación verde y el paseo queda en curso.
- [ ] Con `uncertain`, el aviso amarillo aparece y el paseo está en curso igualmente.
- [ ] Con `unknown` y con `unavailable` el paseo empieza sin errores visibles de IA.
- [ ] "Iniciar sin foto" llama al endpoint sin `image` y funciona como antes.
- [ ] Un test vitest por cada fila de la tabla (mock de `checkInWalk`), más uno para "sin foto".
- [ ] `pnpm test` y `pnpm build` en verde.
- [ ] `node ~/.agents/skills/avoid-ai-design/scripts/detect.mjs src` con 0 P0 (perfil `inside-design-system`).
- [ ] Prueba a mano en el móvil con la demo: reinicia datos con `seed_demo --reset --apply`, sube 3 fotos de referencia a un perro como dueño, acepta un paseo como paseador y haz el check-in con una foto del mismo perro y otra de un perro distinto.

## Fuera de alcance

Reescribir pantallas, cambiar el flujo de ofertas, mostrar historial de coincidencias, tocar el backend.
