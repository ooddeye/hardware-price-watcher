# Buscador de precios de hardware Argentina

Script Python para buscar precios de componentes de PC en fuentes argentinas y dejar un reporte listo en GitHub Actions.

Fuentes incluidas:

- CompraGamer, usando su catalogo JSON publico.
- HardGamers, usando resultados de busqueda HTML.

## Uso local

```bash
pip install -r requirements.txt
python price_watch.py --config components.yml --out reports/precios.md --json reports/precios.json
```

El reporte queda en:

- `reports/precios.md`
- `reports/precios.json`

## Uso en GitHub

1. Crea un repositorio y sube estos archivos.
2. En GitHub, entra a `Actions`.
3. Ejecuta el workflow `Buscar precios de hardware` con `Run workflow`.
4. Tambien correra cada 4 horas.

El workflow sube el reporte como artifact, manda un resumen a Telegram si configuraste los secretos, y tambien intenta commitear `reports/precios.md` y `reports/precios.json`. Si tu repositorio bloquea commits de Actions, activa `Settings > Actions > General > Workflow permissions > Read and write permissions`.

## Avisos por Telegram

1. Crea un bot hablando con `@BotFather` en Telegram y guarda el token.
2. Abre un chat con el bot y mandale cualquier mensaje.
3. Entra a `https://api.telegram.org/botTU_TOKEN/getUpdates` y copia el `chat.id`.
4. En GitHub, entra a `Settings > Secrets and variables > Actions > New repository secret`.
5. Crea estos secretos:

- `TELEGRAM_BOT_TOKEN`: token del bot.
- `TELEGRAM_CHAT_ID`: id del chat, grupo o canal.
- `TELEGRAM_MESSAGE_THREAD_ID`: opcional, solo si usas temas en un grupo.

Para probarlo localmente:

```bash
$env:TELEGRAM_BOT_TOKEN="123456:ABC..."
$env:TELEGRAM_CHAT_ID="123456789"
python price_watch.py --config components.yml --out reports/precios.md --json reports/precios.json --telegram
```

## Editar componentes

Modifica `components.yml`.

Campos utiles:

- `target_price_ars`: precio objetivo unitario.
- `quantity`: cantidad esperada.
- `queries`: busquedas a ejecutar en HardGamers.
- `compragamer_ids`: IDs exactos de CompraGamer, si los conoces.
- `required_terms`: palabras que deben estar en el nombre.
- `exclude_terms`: palabras que descartan falsos positivos.

## Notas

Los sitios pueden cambiar su HTML o bloquear requests. El script tolera fallos por fuente: si una tienda falla, sigue con las demas y agrega el aviso al reporte.
