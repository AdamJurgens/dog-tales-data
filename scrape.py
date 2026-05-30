#!/usr/bin/env python3
"""Scrape Dog Tales public adoption listings into a static TypeScript data file.

This intentionally keeps runtime app behavior simple: the app reads static data generated
from Dog Tales' public pages rather than scraping on-device.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests

BASE_URL = "https://www.dogtales.ca"
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "src" / "data" / "adoptablePets.ts"
# Stamped into the generated TS file. Hard-coded (not datetime.now()) so regenerating
# without a content change does not churn the file; bump when refreshing the data.
# Stamp the scrape date; the auto-refresh Action overrides nothing and just uses today.
UPDATED_AT = os.environ.get("DOG_TALES_UPDATED_AT") or _date.today().isoformat()
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Dog Tales partner app data refresh/1.0 (+https://www.dogtales.ca)"})

LISTING_PAGES = {
    "dog": f"{BASE_URL}/adopt-a-dog",
    "cat": f"{BASE_URL}/pagea",
}

NAV_PATHS = {
    "/",
    "/about",
    "/meet-our-staff",
    "/faq",
    "/accessibility",
    "/pagea",
    "/cat-adoptions-cattales",
    "/adopt-a-dog",
    "/adoption-information",
    "/dog-foster-application",
    "/surrender-a-dog",
    "/rescue-partners",
    "/adoptable-guinea-pigs",
    "/guinea-pig-adoption-application",
    "/our-beautiful-horses",
    "/our-cows",
    "/our-donkeys",
    "/our-pigs",
    "/our-sheep",
    "/sponsor-a-horse",
    "/canadian-horse-meat-trade",
    "/careers",
    "/open-house",
    "/volunteer",
    "/donate",
    "/press",
    "/contact",
    "/search",
}

TRAIT_IMAGE_WORDS = (
    "friendly",
    "selective",
    "untested",
    "energy",
    "kids",
    "cats",
    "dogs",
    "teens",
    "adult",
    "medium",
    "high",
    "low",
)

# Squarespace status/badge overlays (e.g. "RESCUED BY DTCF", "Medical Needs") are
# decorative graphics, not real pet photos — never include them in the gallery.
BADGE_IMAGE_WORDS = (
    "rescued by",
    "dtcf",
    "medical needs",
    "special needs",
    "bonded pair",
    "family at home",
    "experienced home",
    "experienced only",
    "adult home",
    "no stairs",
    "blind",
    "deaf",
    "apartment",
    "senior dog",
)


@dataclass(frozen=True)
class ListingPet:
    species: str
    profile_path: str
    listing_title: str
    image_url: str


def fetch(url: str) -> str:
    response = SESSION.get(url, timeout=30)
    response.raise_for_status()
    return response.text


def strip_tags(value: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value))).strip()


def attr(tag: str, name: str) -> str | None:
    match = re.search(rf"\b{name}=([\"'])(.*?)\1", tag, re.I | re.S)
    if not match:
        return None
    return html.unescape(match.group(2)).strip()


def normalize_image_url(url: str) -> str:
    url = html.unescape(url).strip()
    if url.startswith("//"):
        url = f"https:{url}"
    if url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    if "?" not in url and "images.squarespace-cdn.com" in url:
        return f"{url}?format=750w"
    return url


def status_from_title(title: str) -> str:
    lowered = title.lower()
    if "sanctuary" in lowered:
        return "Sanctuary pet"
    if "foster" in lowered:
        return "Foster home"
    return "Available"


def clean_pet_name(title: str) -> str:
    cleaned = re.sub(r"\s*\([^)]*\)\s*", " ", title).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def clean_trait(value: str) -> str:
    """Strip scraper artifacts from a trait label.

    Squarespace trait badges arrive with index/count noise baked into the alt text,
    e.g. "Dog Friendly (1) 2" / "Cat Selective-30" / "High Energy 3". Those numbers
    are sprite-sheet / gallery indices, never meaningful to a reader, so drop them.
    """
    trait = html.unescape(value)
    trait = re.sub(r"\s+", " ", trait).strip()
    # Remove parenthetical counts anywhere, e.g. "Untested With Cats (1)" -> "...Cats".
    trait = re.sub(r"\s*\(\s*\d+\s*\)", "", trait)
    # Remove trailing hyphen+number, e.g. "Cat Selective-30" -> "Cat Selective".
    trait = re.sub(r"\s*-\s*\d+\s*$", "", trait)
    # Remove any remaining trailing standalone number(s), e.g. "High Energy 3" -> "High Energy".
    trait = re.sub(r"(?:\s+\d+)+\s*$", "", trait)
    return re.sub(r"\s+", " ", trait).strip()


def clean_traits(values: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in values:
        trait = clean_trait(raw)
        if trait and trait not in cleaned:
            cleaned.append(trait)
    return cleaned


def clean_detail(value: str, name: str) -> str | None:
    """Normalise a single detail line, dropping obvious scraper noise.

    Some profiles leak the pet's name (sometimes with a trailing gallery index, e.g.
    "Kai 2") in as a standalone detail line. Those are not real demographic facts, so
    drop any line that is just the name or name + number.
    """
    detail = re.sub(r"\s+", " ", html.unescape(value)).strip()
    if not detail:
        return None
    name_lower = name.lower()
    detail_lower = detail.lower()
    if detail_lower == name_lower:
        return None
    # "Kai 2", "Barron" etc. -- name optionally followed by a bare gallery index.
    if re.fullmatch(re.escape(name_lower) + r"(?:\s+\d+)?", detail_lower):
        return None
    return detail


def clean_details(values: Iterable[str], name: str) -> list[str]:
    cleaned: list[str] = []
    for raw in values:
        detail = clean_detail(raw, name)
        if detail and detail not in cleaned:
            cleaned.append(detail)
    return cleaned


def clean_summary(value: str, name: str) -> str:
    """Strip a leading "<name> • " (or "<name> <n> • ") artifact from a summary."""
    summary = re.sub(r"\s+", " ", html.unescape(value)).strip()
    leading = re.compile(r"^" + re.escape(name) + r"(?:\s+\d+)?\s*[•\-–]\s*", re.I)
    summary = leading.sub("", summary).strip()
    return summary


def parse_demographics(details: list[str]) -> dict:
    """Pull structured sex / age / breed / size out of the demographic detail lines.

    Demographic line format from Dog Tales is e.g. "Female, 7 Years Old, Caucasian
    Shepherd" with a separate "Size: Giant" line. Anything that does not parse cleanly
    is simply left unset rather than guessed.
    """
    fields: dict[str, str] = {}
    for detail in details:
        size_match = re.match(r"^\s*size\s*:\s*(.+)$", detail, re.I)
        if size_match and "size" not in fields:
            fields["size"] = size_match.group(1).strip()
            continue
        # Demographic line: "<sex>, <age> Years Old, <breed>" (breed optional).
        demo = re.match(
            r"^\s*([A-Za-z][A-Za-z &/]*?)\s*,\s*([^,]*?\bold\b)(?:\s*,\s*(.+))?$",
            detail,
            re.I,
        )
        if demo and "ageText" not in fields:
            sex = demo.group(1).strip()
            if re.fullmatch(r"(?:male|female)(?:\s*&\s*(?:male|female))*", sex, re.I):
                fields["sex"] = sex
            fields["ageText"] = demo.group(2).strip()
            breed = (demo.group(3) or "").strip()
            if breed:
                fields["breedText"] = breed
    return fields


def parse_listing_page(species: str, url: str) -> list[ListingPet]:
    text = fetch(url)
    pets: dict[str, ListingPet] = {}
    # Squarespace gallery slides use one `.slide` per pet. Splitting first avoids
    # regex matches accidentally spanning two cards and dropping every other pet.
    for slide in re.split(r"<div class=\"slide\"", text, flags=re.I):
        href_match = re.search(r"href=([\"'])(/[^\"']+)\1", slide, re.I | re.S)
        if not href_match:
            continue
        href = html.unescape(href_match.group(2)).strip()
        if not href or not href.startswith("/") or href in NAV_PATHS or href.startswith("/#"):
            continue
        image_match = re.search(r"<img\b[^>]*(?:data-src|src)=[\"']([^\"']+)[\"'][^>]*>", slide, re.I | re.S)
        if not image_match:
            continue
        image_url = normalize_image_url(image_match.group(1))
        if "squarespace-cdn.com" not in image_url:
            continue
        title_match = re.search(r"image-slide-title[^>]*>(.*?)</div>", slide, re.I | re.S)
        if title_match:
            title = strip_tags(title_match.group(1))
        else:
            image_tag = image_match.group(0)
            title = attr(image_tag, "alt") or href.strip("/").replace("-", " ").title()
        if not title or title.lower() in {"dog tales rescue and sanctuary", "search"}:
            continue
        pets[href] = ListingPet(species=species, profile_path=href, listing_title=title, image_url=image_url)
    return list(pets.values())


def parse_profile_page(pet: ListingPet) -> dict:
    url = urljoin(BASE_URL, pet.profile_path)
    text = fetch(url)
    h1_values = [strip_tags(value) for value in re.findall(r"<h1\b[^>]*>(.*?)</h1>", text, re.I | re.S)]
    h1_values = [value for value in h1_values if value and value != "Dog Tales Rescue and Sanctuary"]

    name = clean_pet_name(pet.listing_title)
    details: list[str] = []
    for value in h1_values:
        if value == name or value == pet.listing_title:
            continue
        if value not in details:
            details.append(value)

    image_urls: list[str] = []
    traits: list[str] = []
    for image_tag in re.findall(r"<img\b[^>]*>", text, re.I | re.S):
        source = attr(image_tag, "data-src") or attr(image_tag, "data-image") or attr(image_tag, "src")
        alt = attr(image_tag, "alt") or ""
        if not source or "squarespace-cdn.com" not in source:
            continue
        clean_alt = re.sub(r"\.(png|jpg|jpeg|webp)$", "", alt, flags=re.I).replace("+", " ").strip()
        normalized = normalize_image_url(source)
        alt_lower = clean_alt.lower()
        is_trait = any(word in alt_lower for word in TRAIT_IMAGE_WORDS) and clean_alt.lower() != name.lower()
        if is_trait:
            trait = re.sub(r"\s+", " ", clean_alt).strip()
            if trait and trait not in traits:
                traits.append(trait)
            continue
        normalized_lower = normalized.lower()
        if "logo" in alt_lower or "dog tales rescue and sanctuary" in alt_lower or "logo" in normalized_lower or "favicon" in normalized_lower:
            continue
        url_words = normalized_lower.replace("+", " ").replace("%20", " ")
        if any(b in alt_lower for b in BADGE_IMAGE_WORDS) or any(b in url_words for b in BADGE_IMAGE_WORDS):
            continue
        if normalized not in image_urls:
            image_urls.append(normalized)

    traits = clean_traits(traits)
    details = clean_details(details, name)

    profile_image_url = image_urls[0] if image_urls else pet.image_url
    # Build the photo gallery: the listing card image is the "best/main" shot, followed by
    # any additional profile images. De-duplicate while preserving order so photos[0] is main.
    photos: list[str] = []
    for candidate in [pet.image_url, profile_image_url, *image_urls]:
        if candidate and candidate not in photos:
            photos.append(candidate)

    summary_parts = []
    if details:
        summary_parts.append(details[0])
    if len(details) > 1:
        summary_parts.append(details[1])
    summary = " • ".join(summary_parts) if summary_parts else f"Meet {name} at Dog Tales."
    summary = clean_summary(summary, name)

    record = {
        "id": f"{pet.species}-{pet.profile_path.strip('/').replace('/', '-')}",
        "name": name,
        "species": pet.species,
        "profilePath": pet.profile_path,
        "profileUrl": url,
        "status": status_from_title(pet.listing_title),
        "imageUrl": pet.image_url,
        "profileImageUrl": profile_image_url,
        "summary": summary,
        "details": details[:5],
        "traits": traits[:6],
        "photos": photos[:8],
    }
    record.update(parse_demographics(record["details"]))
    return record


def ts_string(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def emit_ts(pets: Iterable[dict]) -> str:
    pet_list = list(pets)
    lines = [
        "export type AdoptableSpecies = 'dog' | 'cat';",
        "",
        "export type AdoptableStatus = 'Available' | 'Foster home' | 'Sanctuary pet';",
        "",
        "export interface AdoptablePet {",
        "  id: string;",
        "  name: string;",
        "  species: AdoptableSpecies;",
        "  profilePath: string;",
        "  profileUrl: string;",
        "  status: AdoptableStatus;",
        "  imageUrl: string;",
        "  profileImageUrl: string;",
        "  summary: string;",
        "  details: string[];",
        "  traits: string[];",
        "  // Optional, scraped where available. Older records may omit these.",
        "  photos?: string[];",
        "  sex?: string;",
        "  ageText?: string;",
        "  breedText?: string;",
        "  size?: string;",
        "}",
        "",
        "const DOG_TALES_BASE_URL = 'https://www.dogtales.ca';",
        "",
        f"export const ADOPTABLE_PETS_UPDATED_AT = {ts_string(UPDATED_AT)};",
        "",
        "// Generated from Dog Tales public adoption pages by scripts/scrape_dog_tales_adoptions.py.",
        "// Refresh when Dog Tales updates their adoptable pets.",
        "export const ADOPTABLE_PETS: AdoptablePet[] = [",
    ]
    for pet in pet_list:
        entry = [
            "  {",
            f"    id: {ts_string(pet['id'])},",
            f"    name: {ts_string(pet['name'])},",
            f"    species: {ts_string(pet['species'])} as AdoptableSpecies,",
            f"    profilePath: {ts_string(pet['profilePath'])},",
            f"    profileUrl: {ts_string(pet['profileUrl'])},",
            f"    status: {ts_string(pet['status'])} as AdoptableStatus,",
            f"    imageUrl: {ts_string(pet['imageUrl'])},",
            f"    profileImageUrl: {ts_string(pet['profileImageUrl'])},",
            f"    summary: {ts_string(pet['summary'])},",
            f"    details: {ts_string(pet['details'])},",
            f"    traits: {ts_string(pet['traits'])},",
        ]
        if pet.get("photos"):
            entry.append(f"    photos: {ts_string(pet['photos'])},")
        for field in ("sex", "ageText", "breedText", "size"):
            if pet.get(field):
                entry.append(f"    {field}: {ts_string(pet[field])},")
        entry.append("  },")
        lines.extend(entry)
    lines.extend(
        [
            "];",
            "",
            "export const ADOPTABLE_DOGS = ADOPTABLE_PETS.filter((pet) => pet.species === 'dog');",
            "export const ADOPTABLE_CATS = ADOPTABLE_PETS.filter((pet) => pet.species === 'cat');",
            "",
            "export const DOG_TALES_ADOPTION_LINKS = {",
            "  dogs: `${DOG_TALES_BASE_URL}/adoption-information`,",
            "  cats: `${DOG_TALES_BASE_URL}/cat-adoptions-cattales`,",
            "  dogListings: `${DOG_TALES_BASE_URL}/adopt-a-dog`,",
            "  catListings: `${DOG_TALES_BASE_URL}/pagea`,",
            "};",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    listings: list[ListingPet] = []
    for species, url in LISTING_PAGES.items():
        species_listings = parse_listing_page(species, url)
        print(f"{species}: {len(species_listings)} listings")
        listings.extend(species_listings)

    pets: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {executor.submit(parse_profile_page, pet): pet for pet in listings}
        for future in as_completed(future_map):
            pet = future_map[future]
            try:
                pets.append(future.result())
            except Exception as error:  # noqa: BLE001 - script should continue and report all failures.
                print(f"Failed {pet.profile_path}: {error}")

    order = {pet.profile_path: index for index, pet in enumerate(listings)}
    pets.sort(key=lambda item: (item["species"] != "dog", order.get(item["profilePath"], 9999)))

    # Optional runtime feed: write a {updatedAt, pets} JSON for the app to fetch live.
    feed_path = os.environ.get("DOG_TALES_FEED_JSON")
    if feed_path:
        feed = {"updatedAt": UPDATED_AT, "pets": pets}
        Path(feed_path).write_text(json.dumps(feed, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"wrote feed JSON ({len(pets)} pets) to {feed_path}")

    # The bundled TypeScript (offline fallback) is written unless we're only refreshing the feed.
    if os.environ.get("DOG_TALES_FEED_ONLY") == "1":
        return
    OUTPUT.write_text(emit_ts(pets), encoding="utf-8")
    print(f"wrote {len(pets)} pets to {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    started = time.time()
    main()
    print(f"done in {time.time() - started:.1f}s")
