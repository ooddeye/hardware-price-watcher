from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
import yaml
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
}

ARS = "ARS"
HARDGAMERS_URL = "https://www.hardgamers.com.ar/search"
COMPRAGAMER_PRODUCTS_URL = "https://static.compragamer.com/productos"
TELEGRAM_MESSAGE_LIMIT = 3900
# Ofertas por debajo de este % del precio objetivo se descartan (cables, errores de carga, etc.)
DEFAULT_MIN_PRICE_RATIO = 0.40


def local_timezone() -> timezone | ZoneInfo:
    try:
        return ZoneInfo("America/Argentina/Buenos_Aires")
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=-3), "ART")


LOCAL_TZ = local_timezone()


@dataclass(slots=True)
class Offer:
    component: str
    source: str
    store: str
    name: str
    price_ars: float
    url: str
    score: int
    matched_by: str
    available: bool | None = None
    stock: int | None = None
    list_price_ars: float | None = None
    collected_at: str | None = None


# ---------------------------------------------------------------------------
# Normalizacion y matching
# ---------------------------------------------------------------------------
def normalize(value: Any) -> str:
    """Minusculas, sin acentos, solo [a-z0-9] y con letras/numeros separados.

    '8GB' y '8 GB' -> '8 gb'; 'DDR4' -> 'ddr 4'; '2x16' y '2 x 16' -> '2 x 16'.
    Se aplica igual a nombres y a terminos, asi que el matching es consistente.
    """
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def term_present(haystack_normalized: str, term: str) -> bool:
    """Coincidencia por limite de palabra (sin fallback a substring)."""
    needle = normalize(term)
    if not needle:
        return True
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack_normalized) is not None


def slugify_compragamer(name: str) -> str:
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^A-Za-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


def to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Precios (soporta 250000, 250000.00, 250.000, 250.000,50, $ 1.234.567, 1,234.56)
# ---------------------------------------------------------------------------
def parse_price(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None

    text = re.sub(r"[^\d.,]", "", str(value))
    if not text:
        return None

    if "," in text and "." in text:
        # el ultimo separador es el decimal
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".") if re.search(r",\d{1,2}$", text) else text.replace(",", "")
    elif text.count(".") > 1 or re.search(r"\.\d{3}$", text):
        text = text.replace(".", "")  # 250.000 -> 250000

    try:
        price = float(text)
    except ValueError:
        return None
    return price if price > 0 else None


def format_ars(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"$ {int(round(float(value))):,}".replace(",", ".")


def markdown_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def make_session(extra_headers: dict[str, str] | None = None) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(DEFAULT_HEADERS | (extra_headers or {}))
    return session


def request_text(session: requests.Session, url: str, **kwargs: Any) -> str:
    response = session.get(url, timeout=(5, 30), **kwargs)
    response.raise_for_status()
    return response.text


def component_match_score(component: dict[str, Any], product_name: str, exact_id_match: bool = False) -> tuple[bool, int, str]:
    normalized_name = normalize(product_name)

    excluded_terms = component.get("exclude_terms", [])
    exclusions = [term for term in excluded_terms if term_present(normalized_name, term)]
    if exclusions:
        return False, 0, f"excluido: {', '.join(exclusions)}"

    required_terms = component.get("required_terms", [])
    missing_terms = [term for term in required_terms if not term_present(normalized_name, term)]
    # El ID es una pista, no un pase libre: solo saltea required_terms si el componente
    # declara trust_compragamer_ids: true (ej. cuando CompraGamer cambia el nombre del producto).
    trust_ids = bool(component.get("trust_compragamer_ids", False))
    if missing_terms and not (exact_id_match and trust_ids):
        return False, 0, f"faltan: {', '.join(missing_terms)}"

    queries = component.get("queries", [])
    query_text = " ".join(str(query) for query in queries)
    query_tokens = {
        token
        for token in normalize(query_text).split()
        if len(token) > 2 and token not in {"con", "para", "the", "and", "ddr", "amd"}
    }
    matched_query_tokens = sum(1 for token in query_tokens if term_present(normalized_name, token))
    score = matched_query_tokens + len(required_terms) * 5
    if exact_id_match:
        score += 100

    return True, score, "id exacto" if exact_id_match else "terminos"


# ---------------------------------------------------------------------------
# HardGamers
# ---------------------------------------------------------------------------
def parse_hardgamers_articles(html: str, component: dict[str, Any], now_iso: str) -> tuple[list[Offer], int]:
    """Devuelve (ofertas que matchean, total de articulos encontrados en la pagina)."""
    soup = BeautifulSoup(html, "html.parser")
    offers: list[Offer] = []
    articles = soup.select("article[itemtype='http://schema.org/Product'], article.One-Bit-Product")

    for article in articles:
        name_node = article.select_one("[itemprop='name'], .product-name")
        price_node = article.select_one("[itemprop='price']")
        store_node = article.select_one(".store")
        link_node = article.select_one("a[href^='/product/'], a[href*='/product/']")

        if not name_node or not price_node:
            continue

        name = name_node.get_text(" ", strip=True)
        price = parse_price(price_node.get("content") or price_node.get_text(" ", strip=True))
        if price is None:
            continue

        matched, score, matched_by = component_match_score(component, name)
        if not matched:
            continue

        link = urljoin("https://www.hardgamers.com.ar/", link_node["href"]) if link_node and link_node.get("href") else ""

        offers.append(
            Offer(
                component=component["name"],
                source="hardgamers",
                store=store_node.get_text(" ", strip=True) if store_node else "HardGamers",
                name=name,
                price_ars=price,
                url=link,
                score=score,
                matched_by=matched_by,
                available=True,
                collected_at=now_iso,
            )
        )

    return offers, len(articles)


def search_hardgamers(
    session: requests.Session,
    component: dict[str, Any],
    source_cfg: dict[str, Any],
    now_iso: str,
) -> tuple[list[Offer], list[str]]:
    warnings: list[str] = []
    offers: list[Offer] = []
    max_pages = int(component.get("hardgamers_pages", source_cfg.get("max_pages", 1)))
    delay = float(source_cfg.get("delay_seconds", 0.7))
    queries = component.get("queries") or [component["name"]]
    total_articles = 0
    requests_ok = 0

    for query in queries:
        for page in range(1, max_pages + 1):
            params: dict[str, Any] = {"text": query}
            if page > 1:
                params["page"] = page
            try:
                html = request_text(session, HARDGAMERS_URL, params=params)
            except requests.RequestException as exc:
                warnings.append(f"HardGamers fallo para '{query}' pagina {page}: {exc}")
                break
            finally:
                time.sleep(delay)  # rate limit tambien ante errores

            requests_ok += 1
            page_offers, n_articles = parse_hardgamers_articles(html, component, now_iso)
            total_articles += n_articles
            offers.extend(page_offers)
            if n_articles == 0:
                break  # no hay mas paginas

    if requests_ok and total_articles == 0:
        warnings.append(
            f"HardGamers: 0 articulos parseados para '{component['name']}'. "
            "Posible cambio de HTML o bloqueo; revisar selectores."
        )

    return dedupe_offers(offers), warnings


# ---------------------------------------------------------------------------
# CompraGamer
# ---------------------------------------------------------------------------
def fetch_compragamer_products(session: requests.Session, source_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    url = source_cfg.get("url", COMPRAGAMER_PRODUCTS_URL)
    text = request_text(session, url)
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("El catalogo de CompraGamer no devolvio una lista JSON")
    products = [item for item in data if isinstance(item, dict)]
    if not products:
        raise ValueError("El catalogo de CompraGamer vino vacio")
    return products


def compragamer_offer_from_product(component: dict[str, Any], product: dict[str, Any], now_iso: str, score: int, matched_by: str) -> Offer | None:
    product_id = product.get("id_producto")
    price = parse_price(product.get("precioEspecial") or product.get("precioEspecialCombo") or product.get("precioLista"))
    if price is None or not product_id:
        return None

    name = str(product.get("nombre") or f"Producto {product_id}")
    url = f"https://compragamer.com/producto/{slugify_compragamer(name)}_{product_id}"
    try:
        stock = int(product.get("stock") or 0)
    except (TypeError, ValueError):
        stock = 0
    available = bool(product.get("vendible")) and stock > 0

    return Offer(
        component=component["name"],
        source="compragamer",
        store="CompraGamer",
        name=name,
        price_ars=price,
        list_price_ars=parse_price(product.get("precioLista") or product.get("precioListaCombo")),
        url=url,
        score=score,
        matched_by=matched_by,
        available=available,
        stock=stock,
        collected_at=now_iso,
    )


def search_compragamer(
    products: list[dict[str, Any]],
    component: dict[str, Any],
    source_cfg: dict[str, Any],
    now_iso: str,
) -> tuple[list[Offer], list[str]]:
    include_out_of_stock = bool(source_cfg.get("include_out_of_stock", False))
    expected_ids = {i for i in (to_int(pid) for pid in component.get("compragamer_ids", [])) if i is not None}
    seen_ids: set[int] = set()
    id_warnings: list[str] = []
    offers: list[Offer] = []

    for product in products:
        product_id = to_int(product.get("id_producto"))
        exact_id_match = product_id in expected_ids
        if exact_id_match:
            seen_ids.add(product_id)  # type: ignore[arg-type]
        name = str(product.get("nombre") or "")
        matched, score, matched_by = component_match_score(component, name, exact_id_match=exact_id_match)
        if not matched:
            if exact_id_match and matched_by.startswith("faltan"):
                id_warnings.append(
                    f"CompraGamer: el ID {product_id} de '{component['name']}' apunta a "
                    f"'{name}', que no cumple required_terms ({matched_by}). "
                    "Revisar el ID en components.yml."
                )
            continue

        offer = compragamer_offer_from_product(component, product, now_iso, score, matched_by)
        if offer is None:
            continue
        if not include_out_of_stock and offer.available is False:
            continue
        offers.append(offer)

    warnings = id_warnings + [
        f"CompraGamer: el ID {pid} de '{component['name']}' no existe en el catalogo (descontinuado o cambio de ID)."
        for pid in sorted(expected_ids - seen_ids)
    ]
    return dedupe_offers(offers), warnings


# ---------------------------------------------------------------------------
# Consolidacion
# ---------------------------------------------------------------------------
def dedupe_offers(offers: list[Offer]) -> list[Offer]:
    by_key: dict[tuple[str, str, str, int], Offer] = {}
    for offer in offers:
        key = (
            offer.source,
            normalize(offer.store),
            normalize(offer.name),
            int(round(offer.price_ars)),
        )
        existing = by_key.get(key)
        if existing is None or offer.score > existing.score:
            by_key[key] = offer

    return sorted(by_key.values(), key=lambda item: (item.price_ars, -item.score, item.source, item.store))


def drop_suspicious(offers: list[Offer], component: dict[str, Any], config: dict[str, Any]) -> tuple[list[Offer], list[str]]:
    target = parse_price(component.get("target_price_ars"))
    if target is None:
        return offers, []
    ratio = float(component.get("min_price_ratio", config.get("min_price_ratio", DEFAULT_MIN_PRICE_RATIO)))
    floor = target * ratio
    kept = [o for o in offers if o.price_ars >= floor]
    dropped = [o for o in offers if o.price_ars < floor]
    warnings = [
        f"Descartado por precio sospechoso ({format_ars(o.price_ars)} < {format_ars(floor)}): "
        f"{o.name[:60]} [{o.store}]"
        for o in dropped
    ]
    return kept, warnings


def validate_config(config: dict[str, Any]) -> None:
    components = config.get("components")
    if not isinstance(components, list) or not components:
        raise ValueError("Config invalida: falta la lista 'components'")
    for index, component in enumerate(components):
        for field in ("name", "target_price_ars", "required_terms"):
            if field not in component:
                raise ValueError(f"components[{index}] sin campo obligatorio '{field}'")
        if parse_price(component["target_price_ars"]) is None:
            raise ValueError(f"components[{index}] ('{component['name']}'): target_price_ars invalido")


def collect_offers(config: dict[str, Any]) -> tuple[dict[str, list[Offer]], list[str]]:
    session = make_session(config.get("headers"))
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    warnings: list[str] = []

    sources = config.get("sources", {})
    cg_cfg = sources.get("compragamer", {})
    hg_cfg = sources.get("hardgamers", {})

    compragamer_products: list[dict[str, Any]] | None = None
    if cg_cfg.get("enabled", True):
        try:
            compragamer_products = fetch_compragamer_products(session, cg_cfg)
        except Exception as exc:  # noqa: BLE001 - una fuente caida no frena el reporte.
            warnings.append(f"CompraGamer no se pudo consultar: {exc}")

    results: dict[str, list[Offer]] = {}

    for component in config.get("components", []):
        all_offers: list[Offer] = []

        if cg_cfg.get("enabled", True) and compragamer_products is not None:
            cg_offers, cg_warnings = search_compragamer(compragamer_products, component, cg_cfg, now_iso)
            all_offers.extend(cg_offers)
            warnings.extend(cg_warnings)

        if hg_cfg.get("enabled", True):
            try:
                hg_offers, hg_warnings = search_hardgamers(session, component, hg_cfg, now_iso)
                all_offers.extend(hg_offers)
                warnings.extend(hg_warnings)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"HardGamers fallo inesperadamente para '{component['name']}': {exc}")

        offers, suspicious_warnings = drop_suspicious(dedupe_offers(all_offers), component, config)
        warnings.extend(suspicious_warnings)

        max_results = int(component.get("max_results", config.get("max_results_per_component", 8)))
        results[component["name"]] = offers[:max_results]

    return results, warnings


# ---------------------------------------------------------------------------
# Reportes
# ---------------------------------------------------------------------------
def build_markdown_report(config: dict[str, Any], results: dict[str, list[Offer]], warnings: list[str]) -> str:
    generated_at = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    lines: list[str] = [
        "# Reporte de precios de hardware",
        "",
        f"Generado: {generated_at}",
        "",
    ]

    for component in config.get("components", []):
        name = component["name"]
        target = parse_price(component.get("target_price_ars"))
        quantity = int(component.get("quantity", 1))
        offers = results.get(name, [])

        lines.extend([f"## {markdown_escape(name)}", ""])
        if target is not None:
            lines.append(f"Precio objetivo unitario: {format_ars(target)}")
            if quantity > 1:
                lines.append(f"Cantidad configurada: {quantity} | Objetivo total: {format_ars(target * quantity)}")
            lines.append("")

        if not offers:
            lines.extend(["> Sin resultados para los filtros actuales.", ""])
            continue

        best = offers[0]
        if target is not None:
            diff = best.price_ars - target
            status = "OK, dentro del objetivo" if diff <= 0 else f"{format_ars(diff)} sobre el objetivo"
            lines.append(f"Mejor precio: **{format_ars(best.price_ars)}** en **{markdown_escape(best.store)}** ({status}).")
        else:
            lines.append(f"Mejor precio: **{format_ars(best.price_ars)}** en **{markdown_escape(best.store)}**.")
        if quantity > 1:
            lines.append(f"Total por {quantity}: **{format_ars(best.price_ars * quantity)}**.")
        lines.append("")

        lines.extend(
            [
                "| Precio | Lista | Tienda | Fuente | Producto | Stock | Link |",
                "|---:|---:|---|---|---|---:|---|",
            ]
        )
        for offer in offers:
            stock = "-" if offer.stock is None else str(offer.stock)
            url = offer.url or "#"
            lines.append(
                "| "
                f"{format_ars(offer.price_ars)} | "
                f"{format_ars(offer.list_price_ars)} | "
                f"{markdown_escape(offer.store)} | "
                f"{markdown_escape(offer.source)} | "
                f"{markdown_escape(offer.name)} | "
                f"{stock} | "
                f"[ver]({url}) |"
            )
        lines.append("")

    if warnings:
        lines.extend(["## Avisos", ""])
        for warning in warnings:
            lines.append(f"- {markdown_escape(warning)}")
        lines.append("")

    return "\n".join(lines)


def short_component_name(component: dict[str, Any]) -> str:
    return str(component.get("telegram_name") or component["name"])


def build_telegram_message(config: dict[str, Any], results: dict[str, list[Offer]], warnings: list[str]) -> str:
    generated_at = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M ART")
    lines = [
        "Reporte de precios de hardware",
        f"Generado: {generated_at}",
        "",
    ]

    for component in config.get("components", []):
        name = component["name"]
        target = parse_price(component.get("target_price_ars"))
        quantity = int(component.get("quantity", 1))
        offers = results.get(name, [])
        title = short_component_name(component)

        if not offers:
            lines.append(f"Sin resultados: {title}")
            lines.append("")
            continue

        best = offers[0]
        status = ""
        if target is not None:
            diff = best.price_ars - target
            status = "OK" if diff <= 0 else f"+{format_ars(diff)} vs objetivo"

        lines.append(title)
        lines.append(f"Mejor: {format_ars(best.price_ars)} - {best.store}")
        lines.append(f"Producto: {best.name}")
        if target is not None:
            lines.append(f"Objetivo: {format_ars(target)} ({status})")
        if quantity > 1:
            lines.append(f"Total x{quantity}: {format_ars(best.price_ars * quantity)}")
        if best.url:
            lines.append(best.url)
        lines.append("")

    if warnings:
        lines.append(f"Avisos: {len(warnings)}. Ver reporte completo en GitHub.")

    return "\n".join(lines).strip()


def split_telegram_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append("\n".join(current))
            current, current_len = [], 0

    for line in text.splitlines():
        if len(line) >= limit:  # linea gigante: respetar el orden y partirla
            flush()
            chunks.extend(line[i : i + limit] for i in range(0, len(line), limit))
            continue
        line_len = len(line) + 1
        if current and current_len + line_len > limit:
            flush()
        current.append(line)
        current_len += line_len

    flush()
    return chunks


def send_telegram_message(text: str) -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    thread_id = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "").strip()

    if not token or not chat_id:
        return "omitido: faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID"

    session = make_session()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = split_telegram_message(text)

    for chunk in chunks:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if thread_id:
            payload["message_thread_id"] = int(thread_id)

        response = session.post(url, json=payload, timeout=30)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            description = body.get("description", "respuesta no OK de Telegram")
            raise RuntimeError(description)

    return f"enviado ({len(chunks)} mensaje(s))"


def write_json_report(config: dict[str, Any], results: dict[str, list[Offer]], warnings: list[str], path: Path) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "currency": ARS,
        "components": [
            {
                "name": component["name"],
                "target_price_ars": component.get("target_price_ars"),
                "quantity": component.get("quantity", 1),
                "offers": [asdict(offer) for offer in results.get(component["name"], [])],
            }
            for component in config.get("components", [])
        ],
        "warnings": warnings,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file)
    if not isinstance(data, dict):
        raise ValueError(f"Config invalida: {path}")
    validate_config(data)
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Busca precios de componentes de PC en tiendas argentinas.")
    parser.add_argument("--config", default="components.yml", type=Path, help="Ruta al archivo YAML de componentes.")
    parser.add_argument("--out", default="reports/precios.md", type=Path, help="Ruta del reporte Markdown.")
    parser.add_argument("--json", default="reports/precios.json", type=Path, help="Ruta del reporte JSON.")
    parser.add_argument("--telegram", action="store_true", help="Envia un resumen a Telegram usando secretos de entorno.")
    args = parser.parse_args()

    config = load_config(args.config)
    results, warnings = collect_offers(config)

    markdown = build_markdown_report(config, results, warnings)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(markdown + "\n", encoding="utf-8")
    write_json_report(config, results, warnings, args.json)

    # Primero mostrar resultados, despues notificar (un fallo de Telegram no oculta nada).
    for component in config.get("components", []):
        offers = results.get(component["name"], [])
        if offers:
            print(f"{component['name']}: {format_ars(offers[0].price_ars)} en {offers[0].store}")
        else:
            print(f"{component['name']}: sin resultados")

    if warnings:
        print("\nAvisos:")
        for warning in warnings:
            print(f"- {warning}")

    exit_code = 0

    if args.telegram:
        try:
            telegram_status = send_telegram_message(build_telegram_message(config, results, warnings))
            print(f"\nTelegram: {telegram_status}")
        except Exception as exc:  # noqa: BLE001
            print(f"\nTelegram fallo: {exc}", file=sys.stderr)
            exit_code = 1

    # Si no hubo NINGUN resultado y hubo avisos, las fuentes probablemente fallaron: que Actions lo marque rojo.
    if not any(results.values()) and warnings:
        print("\nNingun componente tuvo resultados y hubo avisos: posible caida de fuentes.", file=sys.stderr)
        exit_code = exit_code or 2

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
