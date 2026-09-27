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


def normalize(value: Any) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def term_present(haystack_normalized: str, term: str) -> bool:
    needle = normalize(term)
    if not needle:
        return True
    if len(needle) <= 2:
        return re.search(rf"\b{re.escape(needle)}\b", haystack_normalized) is not None
    return re.search(rf"\b{re.escape(needle)}\b", haystack_normalized) is not None or needle in haystack_normalized


def slugify_compragamer(name: str) -> str:
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^A-Za-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


def parse_price(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("$", "").replace(".", "").replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def format_ars(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"$ {int(round(float(value))):,}".replace(",", ".")


def markdown_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def request_text(session: requests.Session, url: str, **kwargs: Any) -> str:
    response = session.get(url, timeout=30, **kwargs)
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
    if missing_terms and not exact_id_match:
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


def parse_hardgamers_articles(html: str, component: dict[str, Any], now_iso: str) -> list[Offer]:
    soup = BeautifulSoup(html, "html.parser")
    offers: list[Offer] = []

    for article in soup.select("article[itemtype='http://schema.org/Product'], article.One-Bit-Product"):
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

    return offers


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

            page_offers = parse_hardgamers_articles(html, component, now_iso)
            offers.extend(page_offers)

            if page < max_pages:
                time.sleep(delay)

    return dedupe_offers(offers), warnings


def fetch_compragamer_products(session: requests.Session, source_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    url = source_cfg.get("url", COMPRAGAMER_PRODUCTS_URL)
    text = request_text(session, url)
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("El catalogo de CompraGamer no devolvio una lista JSON")
    return [item for item in data if isinstance(item, dict)]


def compragamer_offer_from_product(component: dict[str, Any], product: dict[str, Any], now_iso: str, score: int, matched_by: str) -> Offer | None:
    product_id = product.get("id_producto")
    price = parse_price(product.get("precioEspecial") or product.get("precioEspecialCombo") or product.get("precioLista"))
    if price is None or not product_id:
        return None

    name = str(product.get("nombre") or f"Producto {product_id}")
    url = f"https://compragamer.com/producto/{slugify_compragamer(name)}_{product_id}"
    stock = int(product.get("stock") or 0)
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
) -> list[Offer]:
    include_out_of_stock = bool(source_cfg.get("include_out_of_stock", False))
    expected_ids = {int(pid) for pid in component.get("compragamer_ids", [])}
    offers: list[Offer] = []

    for product in products:
        product_id = product.get("id_producto")
        exact_id_match = product_id in expected_ids
        name = str(product.get("nombre") or "")
        matched, score, matched_by = component_match_score(component, name, exact_id_match=exact_id_match)
        if not matched:
            continue

        offer = compragamer_offer_from_product(component, product, now_iso, score, matched_by)
        if offer is None:
            continue
        if not include_out_of_stock and offer.available is False:
            continue
        offers.append(offer)

    return dedupe_offers(offers)


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


def collect_offers(config: dict[str, Any]) -> tuple[dict[str, list[Offer]], list[str]]:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS | config.get("headers", {}))
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    warnings: list[str] = []

    sources = config.get("sources", {})
    compragamer_products: list[dict[str, Any]] | None = None
    if sources.get("compragamer", {}).get("enabled", True):
        try:
            compragamer_products = fetch_compragamer_products(session, sources.get("compragamer", {}))
        except Exception as exc:  # noqa: BLE001 - one source failing should not stop the report.
            warnings.append(f"CompraGamer no se pudo consultar: {exc}")

    results: dict[str, list[Offer]] = {}

    for component in config.get("components", []):
        all_offers: list[Offer] = []

        if sources.get("compragamer", {}).get("enabled", True) and compragamer_products is not None:
            all_offers.extend(search_compragamer(compragamer_products, component, sources.get("compragamer", {}), now_iso))

        if sources.get("hardgamers", {}).get("enabled", True):
            hg_offers, hg_warnings = search_hardgamers(session, component, sources.get("hardgamers", {}), now_iso)
            all_offers.extend(hg_offers)
            warnings.extend(hg_warnings)

        max_results = int(component.get("max_results", config.get("max_results_per_component", 8)))
        results[component["name"]] = dedupe_offers(all_offers)[:max_results]

    return results, warnings


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

    for line in text.splitlines():
        line_len = len(line) + 1
        if current and current_len + line_len > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        if line_len > limit:
            chunks.append(line[:limit])
            continue
        current.append(line)
        current_len += line_len

    if current:
        chunks.append("\n".join(current))

    return chunks


def send_telegram_message(text: str) -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    thread_id = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "").strip()

    if not token or not chat_id:
        return "omitido: faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID"

    session = requests.Session()
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

    if args.telegram:
        try:
            telegram_message = build_telegram_message(config, results, warnings)
            telegram_status = send_telegram_message(telegram_message)
            print(f"Telegram: {telegram_status}")
        except Exception as exc:  # noqa: BLE001 - notification failures should be visible in Actions.
            print(f"Telegram fallo: {exc}", file=sys.stderr)
            return 1

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

    return 0


if __name__ == "__main__":
    sys.exit(main())
