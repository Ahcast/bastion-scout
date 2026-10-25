# bastion-scout

Extractor secuencial de HonduCompras para alimentar Bastion. Mantiene los filtros del
crawler anterior: Compra Menor (`2`) y Suministro de Bienes y/o Servicios (`1`).
El crawler anterior permanece independiente.

## Ejecución diaria

Programa `main.py` cada hora. El programa interpreta siempre la hora de Honduras,
independientemente de la zona horaria del servidor:

- A las **19:00** comienza un ciclo diario nuevo.
- Si el ciclo ya terminó, las siguientes invocaciones no vuelven a extraer ni analizar.
- Si una etapa falla, la siguiente invocación reintenta únicamente lo pendiente.
- Los reintentos terminan a las **13:00 del día siguiente**, sin incluir esa hora.
- Entre las 13:00 y las 19:00 no se inicia trabajo nuevo.
- Una etapa iniciada comprueba el cierre antes de cada solicitud y durante la lectura.
  Las observaciones ya recibidas se consolidan localmente aunque se alcance el cierre.

No se instala un cron ni se despliega nada automáticamente.

## Instalación

Python **3.10 o posterior**. En Linux, dentro de esta carpeta:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.example.json config.json
.venv/bin/python main.py --check
```

Los caminos relativos de `config.json` se resuelven respecto a ese archivo.
El directorio `data/` se crea automáticamente. En Windows se puede usar
`.venv\Scripts\python.exe`.

Ejemplo de cron, ajustando la ruta de instalación:

```cron
0 * * * * /opt/bastion-scout/.venv/bin/python /opt/bastion-scout/main.py --config /opt/bastion-scout/config.json
```

El proceso debe recibir `BASTION_AI_API_KEY` mediante el entorno del servicio o cron.
La configuración no contiene la clave; tampoco se imprime ni se registra en los logs.
El programa no carga archivos `.env` automáticamente.

## Configurar una skill privada

Los archivos de la skill, sus criterios y referencias se mantienen fuera del
repositorio público. Las carpetas `private/` y `skills/` están excluidas de Git.
Para habilitar el análisis:

1. Copia `config.example.json` a `config.json` si es una instalación nueva.
2. Coloca la skill y sus referencias directamente en una carpeta privada del servidor,
   accesible al usuario que ejecuta cron. Configura `ai.skill_path` con el archivo de
   instrucciones para la API y `ai.reference_paths` con sus referencias necesarias.
   Se aceptan rutas absolutas o relativas a `config.json`.
   Por ejemplo, dentro del bloque `ai`:

   ```json
   "skill_path": "/etc/bastion-scout/rfq-clasificacion/api-instructions.md",
   "reference_paths": [
     "/etc/bastion-scout/rfq-clasificacion/references/criterios.md",
     "/etc/bastion-scout/rfq-clasificacion/references/rubros.md"
   ]
   ```

   No se resuelven enlaces Markdown automáticamente. Los ejemplos de calibración
   pueden mantenerse aparte para revisión, sin repetirlos en cada solicitud.
3. Configura opcionalmente `ai.context_paths` con contexto seleccionado. No cargues
   catálogos completos: el límite predeterminado de instrucciones más contexto es
   128 KiB. Si se supera, se informa el error y no se trunca silenciosamente.
4. Configura la variable de entorno `BASTION_AI_API_KEY`.

El modelo configurado es `gpt-6-luna`, mediante Responses API y JSON Schema estricto.
La integración sigue la [documentación del modelo](https://developers.openai.com/api/docs/models/gpt-6-luna)
y la de [salidas estructuradas](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).
No se ha efectuado ninguna llamada de pago durante el desarrollo.

La API devuelve solo `puntaje` (entero 0–100), `rubro`, `resumen` e `items` (array de
textos), sin repetir los datos de entrada. Python escribe `items` como una celda CSV
de texto con numeración y saltos de línea reales, no como JSON. El tratamiento del
contenido y la puntuación se definen exclusivamente en la skill privada.

Si la skill original describe otro formato de respuesta, utiliza una adaptación
privada que conserve sus criterios y pida únicamente los cuatro campos JSON. El
clasificador conserva los procesos independientemente de su rubro o puntaje.

Sin la skill o la clave, el maestro se conserva y la clasificación queda pendiente. Los maestros
anteriores con `resumen_corto` y listas JSON se convierten a `resumen` e items numerados
sin analizar otra vez. También se reutilizan las respuestas antiguas de la caché.

## Archivos y responsabilidades

| Archivo | Contenido |
| --- | --- |
| `main.py` / `data/main.json` | Horario, estado global, etapas pendientes y manifiesto de entrega. |
| `scout.py` / `data/scout.json` | Extracción, páginas, corte, errores y detalles pendientes. |
| `scout_engine.py` / `scout_store.py` | Fases independientes, cola durable y recuperación de paginación. |
| `data/raw.csv` | Observaciones del ciclo, incluidos detalles incompletos. |
| `combiner.py` / `data/combiner.json` | Altas, cambios, conservación y depuración. |
| `clasificador.py` / `data/clasificador.json` | Análisis nuevos, reutilizados, errores y uso de tokens. |
| `data/maestro.csv` | CSV compartido por combiner y clasificador. |
| `data/control.sqlite3` | Checkpoints de extracción, caché de IA y registro de retirados. |
| `data/pipeline.lock` | Bloqueo del sistema operativo para excluir escritores simultáneos. |
| `data/bastion.log*` | Logs rotativos: hasta cuatro archivos de aproximadamente 2 MB. |

Los scripts también se pueden ejecutar individualmente con `--config`. Esas ejecuciones
manuales utilizan el mismo bloqueo; la ventana diaria la aplica `main.py`.
Los códigos de salida son `0` para éxito o fuera de horario, `1` para trabajo pendiente
o error y `75` cuando otra ejecución mantiene el bloqueo.

## Integridad y recuperación

- Primero descubre enlaces usando **Fecha Cierre del listado**, por día de Honduras.
  Cada página y sus enlaces vigentes o de fecha desconocida se confirman juntos en
  SQLite. Diez procesos vencidos consecutivos proponen un corte para evitar recorrer
  páginas que probablemente solo contienen procesos fuera de alcance. Los vencidos quedan
  registrados, pero no se descargan como detalles. En el sitio, la celda «Perido de
  Vigencia» contiene inicio y cierre: se lee el cierre.
- Después procesa la cola guardada. Un fallo de paginación permite continuar esa cola.
  Cada respuesta de detalle se confirma antes de continuar. Al reintentar el ciclo no
  vuelve a solicitar los detalles completos que ya obtuvo.
- `scout_round` guarda la ronda y su avance; `scout_page` conserva páginas, fechas y
  firmas; `scout_queue` guarda estado (`pendiente`, `completo`, `incompleto`, `error`),
  intentos, error y próximo reintento. Se utiliza el mismo `control.sqlite3` existente.
  No se guarda ViewState: cada recuperación crea una búsqueda con los mismos filtros
  y un formulario reciente. También se conserva la acción real del paginador
  (`__EVENTTARGET` y `Page$N`) junto al checkpoint. Al recuperarse, Scout reconstruye
  la búsqueda y vuelve a avanzar página por página hasta el checkpoint, evitando saltos
  que el sitio no acepte. La búsqueda y los cambios de página esperan al menos cinco
  segundos entre solicitudes; los detalles esperan dos. Ante un fallo de listado intenta
  recuperarse hasta tres veces, con pausas crecientes de tres, seis y hasta diez minutos,
  respetando `Retry-After` hasta 30 minutos. Los detalles mantienen su ritmo independiente.
- «Compra Menor» y otros valores genéricos no validan una descripción. La grilla de
  productos se registra como presente, vacía o ausente; una grilla ausente o un detalle
  sin contenido útil queda incompleto. Los campos obligatorios siguen siendo necesarios.
  La migración reabre los falsos completos antiguos sin borrar datos ni caché de IA.
- Los procesos conocidos con fecha vigente se vuelven a comprobar aunque falle el
  listado. Los demás pendientes heredados tienen un presupuesto de 60 segundos y dos
  intentos por invocación. `ronda_actual_completa` y `pendientes_historicos` se informan
  por separado; los heredados no impiden cerrar la ronda actual y permanecen visibles.
- Si un ciclo anterior quedó sin combinar, se recupera su journal y se consolida antes
  de reemplazar el raw con el ciclo nuevo.
- No se elimina un proceso por faltar en el raw de una extracción parcial.
- Las escrituras públicas se realizan en un temporal del mismo directorio y se
  publican mediante reemplazo atómico. Un error de CSV conserva el maestro anterior.
- Cada respuesta de IA validada se confirma en la caché antes de publicar el CSV.
  Una interrupción durante la escritura permite reutilizarla sin otra llamada de IA.
- Un timeout de la API puede haber consumido tokens sin devolver respuesta. No se
  reintenta inmediatamente esa solicitud; se deja pendiente. No se puede garantizar
  evitar ese coste si el servidor procesó una solicitud cuya respuesta se perdió.

Conserva `control.sqlite3` al mover el proyecto o hacer copias de seguridad. Borrarlo
elimina checkpoints, caché y protección contra el reingreso de versiones retiradas.
El historial exitoso de extracción se depura después de siete días; los pendientes
se conservan hasta recuperarse. La caché y el registro de retirados crecen en disco,
sin cargarse completos en memoria.

## Fechas, identidad y retención

La vigencia se compara **por día**. Si hoy es 15, solo el 14 y las fechas anteriores
cuentan como vencidas. Una fecha de hoy se conserva aunque su hora ya haya pasado;
Bastion filtra por hora. Diez vencidos consecutivos proponen el corte; Scout revisa tres
páginas adicionales (`audit_pages`) para detectar procesos vigentes que aparezcan después.

Scout verifica el orden de las fechas de cierre y registra fechas desconocidas e
inversiones. Una fecha desconocida, una inversión o un proceso vigente después del corte
desactiva el corte para esa ronda y Scout sigue hasta el final. `fin_resultados` indica
que llegó a la última página indicada por el sitio; `heuristica_no_garantizada` indica
que terminó después de la auditoría finita. Si una página falla, conserva el checkpoint
y deja la fase pendiente para reintentarla.

`proceso_id` se obtiene del enlace de detalle normalizado. `contenido_hash` usa la fecha
de recepción completa, incluida su hora, y la descripción normalizada. La descripción
incluye las columnas extraídas de los renglones para conservar cantidades y referencias.
El texto no se recorta. Un hash igual conserva la clasificación; un hash distinto la
deja pendiente. Cambiar la skill o el modelo no invalida un análisis ya hecho.

El maestro retira los vencidos por día y los registros con **30 días desde la primera
detección**. Volver a observarlos no reinicia el plazo. Una versión retirada no reingresa
sin cambios. Si cambia su hash, puede reingresar con una nueva ventana de retención.
Una versión que vuelve a un hash previamente analizado recupera el resultado en caché.

## Entrega a Bastion

La lógica de recepción e incorporación pertenece al backend. Este proyecto no sube
archivos ni llama al backend.

`main.json` contiene `output_ready: true` cuando las tres etapas de la ronda actual
terminaron exitosamente. Incluye pendientes históricos, cobertura, `run_id`,
`maestro_sha256` y el total de filas. Los registros incompletos históricos conservan
su estado en el CSV y no se envían a la IA. Para consumir una
versión consistente, el receptor puede comprobar que el SHA-256 del CSV coincide con
ese manifiesto. `items` contiene texto numerado con un item por línea en una celda
CSV entrecomillada. `items_fuente` conserva el JSON de los renglones extraídos.
La codificación del archivo es UTF-8 con BOM.

`main.json` también incluye `daily_counts`, un resumen por fecha de Honduras con los
procesos únicos extraídos ese día, cuántos tienen detalles completos o incompletos y
cuántos procesos recibieron un análisis exitoso de IA ese día. El mismo resumen queda
en el log al cerrar cada invocación de `main.py`; los reintentos del ciclo no duplican
los conteos.

## Carga y ritmo de consulta

Se usa una sola sesión HTTP y no hay consultas paralelas. Por defecto hay una pausa
mínima de un segundo después de cada respuesta, ajustada hasta cuatro segundos según
la latencia. Los errores aumentan la pausa y aplican retroceso exponencial; también se
respeta `Retry-After`. Tres fallos de detalle consecutivos abren el circuito y dejan
el trabajo para la próxima invocación horaria.

Las pausas de fase y los próximos reintentos de cada detalle se guardan en SQLite,
con espera creciente. Cada invocación de Scout tiene 45 minutos por defecto; el
descubrimiento usa como máximo 15 minutos y el 40 % del tiempo restante, dejando
tiempo para la extracción. Estos presupuestos no descartan enlaces: se retoman
desde sus checkpoints en otra invocación. `main.py` conserva el cierre de las 13:00.

Los timeouts de conexión y lectura son 10 y 90 segundos. No hay un límite de 600
procesos: ese número es la carga esperada, no un motivo para detenerse. El límite de
16 MB por respuesta HTTP es configurable; excederlo genera un error visible.

El procesamiento usa una página HTML o fila a la vez y tablas de trabajo en disco.
No requiere navegador, pandas, modelos locales ni cargar el maestro completo en RAM.

## Verificaciones

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tests/benchmark.py
```

El benchmark procesa 600 procesos de 20 items cada uno, sin consultar HonduCompras ni
consumir API. Informa memoria máxima y comprueba que una segunda pasada no repita análisis.
No representa el tiempo real del sitio ni una medición en el servidor de producción.

Para una prueba manual breve contra el sitio, que no genera raw ni usa IA:

```bash
.venv/bin/python tools/check_site.py --config config.json --second-page
```

Esa prueba realiza un GET inicial, la búsqueda filtrada, un detalle y, con la opción
indicada, una segunda página, manteniendo las mismas pausas del extractor.


## Inicio manual desde Bastion

`python main.py --manual --config /ruta/config.json` inicia un ciclo nuevo de extracción, combinación y clasificación a cualquier hora. Mantiene el bloqueo compartido con cron, los checkpoints y la caché IA; no fuerza un nuevo análisis de contenido sin cambios. Su límite es 18 horas desde el inicio y no modifica la programación normal. Cada ciclo tiene un run_id manual distinto y el manifiesto indica trigger=manual. La interfaz debe usar un controlador local autorizado, sin comandos ni rutas libres.
