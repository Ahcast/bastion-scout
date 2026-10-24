# Validación de desarrollo — 21 de octubre de 2026

La revisión posterior del 24 de octubre (47 pruebas y carga con el motor nuevo) está
registrada en [INCIDENT-2026-10-24.md](INCIDENT-2026-10-24.md).

- 31 pruebas locales aprobadas: fechas por día, corte de diez vencidos, errores de
  detalle, paginación repetida, acumulación parcial, recuperación entre ciclos,
  retención, bloqueo de escritores, validación de CSV, caché y contrato de Responses API.
  Incluyen carga de archivos privados sintéticos, items numerados, preservación del detalle original y
  migración de CSV y caché anteriores sin otra llamada de IA.
- Prueba de carga sin red: 600 procesos, 20 items por proceso. Pico de memoria del
  proceso: **49,59 MB** en Windows/Python 3.12; pico de asignaciones Python: 3,00 MB.
  Duración: 19,67 segundos, sin esperas HTTP ni llamadas reales de IA.
- Segunda pasada del clasificador: cero llamadas simuladas adicionales.
- `main.py --check` y compilación de los módulos aprobados.
- Consulta inicial real al formulario: HTTP 200. Una prueba breve posterior avanzó
  por búsqueda y lectura de un detalle, pero no completó la segunda página por
  errores HTTP y un timeout de lectura después de los reintentos configurados.
  La paginación real completa sigue pendiente de validación con el sitio estable.
- No se ejecutó una extracción completa, no se consumió la API de IA y no se instaló
  un cron ni se modificó el crawler anterior.

La memoria medida corresponde a una simulación concreta, no al servidor de producción
ni a todas las posibles respuestas HTML. El clasificador admite una skill privada,
JSON interno y CSV final con `resumen` e items numerados. El repositorio no contiene
la skill ni sus criterios. Falta instalar esos archivos y la clave de API en el
servidor para validar la clasificación real; las pruebas no miden calidad comercial.
