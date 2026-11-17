#!/usr/bin/env python3
"""
Standalone CLI entrypoint for PoliTO Lecture Transcriber & PDF Notes Generator.
Delegates to PolitoClient and LectureProcessor inside polito-agent.
"""

import argparse
from pathlib import Path

from rich.console import Console

from lecture_processor import LectureProcessor
from polito_client import PolitoClient

console = Console()


def main():
    parser = argparse.ArgumentParser(
        description="PoliTO Sbobinatore: scarica audio videolezioni, trascrive localmente, riconosce le slide e genera PDF"
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Apre Chromium persistente su didattica.polito.it per il login manuale (inclusa 2FA)",
    )
    parser.add_argument("--url", help="URL della videolezione sul Portale della Didattica")
    parser.add_argument("--audio", help="Percorso locale a un file audio/video già presente")
    parser.add_argument("--transcript", help="Percorso a una trascrizione .txt esistente")
    parser.add_argument("--course", help="Nome/parola chiave del corso (opzionale, aiuta a filtrare le slide)")
    parser.add_argument("--slides", help="Percorso esplicito al PDF delle slide (altrimenti riconosciute automaticamente)")
    parser.add_argument("--sync-slides", action="store_true", help="Sincronizza prima le slide del corso dal Portale")
    parser.add_argument("--keep-temp", action="store_true", help="Conserva .mp3, .txt e .md nella cartella temp/")

    args = parser.parse_args()

    if args.login:
        client = PolitoClient(headless=False)
        console.print("[bold blue]Apertura browser su didattica.polito.it...[/bold blue]")
        console.print("[dim]Completa il login (inclusa 2FA) e chiudi la finestra del browser al termine.[/dim]")
        profile_dir = client.login_interactive()
        console.print(f"[bold green]✔[/bold green] Sessione salvata in {profile_dir}")
        return

    if not any([args.url, args.audio, args.transcript]):
        parser.print_help()
        return

    processor = LectureProcessor()
    result = processor.run_recap(
        lesson_url=args.url,
        audio_path=Path(args.audio) if args.audio else None,
        transcript_path=Path(args.transcript) if args.transcript else None,
        course_hint=args.course,
        explicit_slides=Path(args.slides) if args.slides else None,
        sync_slides=args.sync_slides,
        keep_temp=args.keep_temp,
    )

    console.print(f"[bold green]✔ PDF generato:[/bold green] {result['pdf_path']}")
    if result.get("desktop_pdf_path"):
        console.print(f"[bold green]✔ Copia organizzata sul Desktop:[/bold green] {result['desktop_pdf_path']}")
    if result.get("slides_used"):
        console.print(f"[bold cyan]Slide riconosciute:[/bold cyan] {result['slides_used']} ({result['slides_reason']})")
    else:
        console.print(f"[yellow]Slide:[/yellow] {result['slides_reason']}")


if __name__ == "__main__":
    main()
