#!/usr/bin/env python3
"""
Sincronizza i PDF delle slide/materiali dal Portale della Didattica PoliTO:
rileva i titoli dei PDF sul portale e scarica solo quelli non ancora presenti in locale.

Uso:
    python3 sync_slides.py                  # Controlla tutti i corsi iscritti
    python3 sync_slides.py --course "OOP"   # Controlla solo un corso specifico
    python3 sync_slides.py --dry-run        # Mostra i PDF mancanti senza scaricarli
    python3 sync_slides.py --all-categories # Include anche esercitazioni, laboratori, esami
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from config import COURSE_ALIASES
from file_organizer import FileOrganizer
from polito_client import PolitoClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sync_slides")

DEFAULT_SLIDE_CATEGORIES = {"Lezioni", "Materiale_Generale"}
EXCLUDED_LOCAL_DIRS = {"appunti_lezioni", "appunti_pdf", "appunti", "context"}


def list_local_pdfs(organizer: FileOrganizer, course_name: str) -> Set[str]:
    """Restituisce i nomi (lowercase) dei PDF gia' presenti localmente per il corso."""
    course_dir = organizer.base_dir / organizer.sanitize_name(course_name)
    if not course_dir.exists():
        return set()

    local_names: Set[str] = set()
    for pdf_path in course_dir.rglob("*.pdf"):
        if any(part.lower() in EXCLUDED_LOCAL_DIRS for part in pdf_path.relative_to(course_dir).parts):
            continue
        local_names.add(pdf_path.name.lower())
    return local_names


def filter_missing_portal_pdfs(
    organizer: FileOrganizer,
    course_name: str,
    materials: List[Dict[str, Any]],
    allowed_categories: Optional[Set[str]] = DEFAULT_SLIDE_CATEGORIES,
) -> List[Dict[str, Any]]:
    """Filtra i materiali PDF del portale non ancora presenti in locale."""
    local_pdfs = list_local_pdfs(organizer, course_name)
    missing: List[Dict[str, Any]] = []
    seen_filenames: Set[str] = set()

    for mat in materials:
        filename = mat.get("filename", "")
        if not filename.lower().endswith(".pdf"):
            continue
        if allowed_categories is not None:
            cat = organizer.categorize(filename, mat.get("remote_path", ""))
            if cat not in allowed_categories:
                continue

        clean_name = organizer.sanitize_name(filename).lower()
        if clean_name in local_pdfs or clean_name in seen_filenames:
            continue
        seen_filenames.add(clean_name)
        missing.append(mat)

    return missing


def sync_missing_pdfs(
    course_filter: Optional[str] = None,
    all_categories: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Rileva i PDF sul Portale della Didattica e scarica quelli assenti in locale.
    """
    organizer = FileOrganizer()
    client = PolitoClient(organizer=organizer)

    enrolled = client.get_enrolled_courses()
    if course_filter:
        kw = course_filter.lower().strip()
        mapped = COURSE_ALIASES.get(kw, kw)
        enrolled = [
            c for c in enrolled
            if kw in c["name"].lower() or mapped in c["name"].lower()
        ]

    if not enrolled:
        logger.warning("Nessun corso trovato corrispondente al filtro specificato.")
        return {"courses_checked": 0, "missing_found": 0, "downloaded": 0}

    allowed = None if all_categories else DEFAULT_SLIDE_CATEGORIES
    all_missing: List[Dict[str, Any]] = []

    for course in enrolled:
        cname = course["name"]
        logger.info(f"Controllo PDF sul portale per: {cname}...")
        materials = client.crawl_course_materials(course)
        pdf_total = sum(1 for m in materials if m.get("filename", "").lower().endswith(".pdf"))
        missing = filter_missing_portal_pdfs(organizer, cname, materials, allowed_categories=allowed)

        logger.info(
            f"  -> {pdf_total} PDF totali sul portale, {len(missing)} nuovi/mancanti in locale."
        )
        for m in missing:
            logger.info(f"     [MANCANTE] {m['filename']} ({m.get('remote_path', '')})")
        all_missing.extend(missing)

    downloaded_count = 0
    if all_missing and not dry_run:
        logger.info(f"Avvio download di {len(all_missing)} PDF mancanti...")
        results = client.download_materials_batch(all_missing)
        downloaded_count = len(results)
        logger.info(f"Download completato: {downloaded_count}/{len(all_missing)} PDF salvati in {organizer.base_dir}.")
    elif all_missing and dry_run:
        logger.info("Modalita' --dry-run attiva: nessun file scaricato.")
    else:
        logger.info("Tutti i PDF del portale sono gia' presenti in locale.")

    return {
        "courses_checked": len(enrolled),
        "missing_found": len(all_missing),
        "downloaded": downloaded_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rileva i PDF sul portale della didattica PoliTO e scarica quelli mancanti in locale."
    )
    parser.add_argument("-c", "--course", type=str, default=None, help="Filtra per nome o alias del corso (es. OOP)")
    parser.add_argument("--all-categories", action="store_true", help="Include anche esercitazioni, laboratori ed esami")
    parser.add_argument("--dry-run", action="store_true", help="Elenca solo i PDF mancanti senza scaricarli")
    args = parser.parse_args()

    try:
        sync_missing_pdfs(
            course_filter=args.course,
            all_categories=args.all_categories,
            dry_run=args.dry_run,
        )
        return 0
    except Exception as e:
        logger.error(f"Errore durante la sincronizzazione: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
