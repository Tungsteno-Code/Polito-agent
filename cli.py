"""
Command-Line Interface for PoliTO Agent.
Provides an interactive terminal interface with Rich styling for material sync
and slide-aware lecture transcription + PDF recap generation.
"""

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agent import PolitoAgent
from config import COURSE_ALIASES, DATA_DIR, PDF_OUTPUT_DIR, POLITO_USERNAME, SESSION_FILE
from file_organizer import FileOrganizer
from lecture_processor import LectureProcessor, format_lesson_filename_stem
from polito_client import PolitoClient
from slide_matcher import SlideMatcher

console = Console()


def show_banner():
    banner = """
    [bold cyan]╔═══════════════════════════════════════════════════════╗[/bold cyan]
    [bold cyan]║[/bold cyan]            [bold yellow]🤖 PoliTO Materials & Recap Agent[/bold yellow]          [bold cyan]║[/bold cyan]
    [bold cyan]║[/bold cyan]   [dim]Materiali Desktop, Sbobinatura & Appunti PDF AI[/dim]     [bold cyan]║[/bold cyan]
    [bold cyan]╚═══════════════════════════════════════════════════════╝[/bold cyan]
    """
    console.print(banner)


def _print_recap_result(recap: dict):
    """Displays a formatted summary panel for a generated lecture recap PDF or prepared context."""
    slides_line = (
        f"[green]{recap['slides_used']}[/green] ([dim]{recap['slides_reason']}[/dim])"
        if recap.get("slides_used")
        else f"[yellow]Nessuna[/yellow] ([dim]{recap.get('slides_reason', 'Solo trascrizione audio')}[/dim])"
    )
    if recap.get("prepared_only"):
        console.print(Panel(
            f"[bold]Corso:[/bold] {recap.get('course')}\n"
            f"[bold]Lezione:[/bold] {recap.get('lesson_title')} ({recap.get('lesson_date')})\n"
            f"[bold]Slide riconosciute:[/bold] {slides_line}\n"
            f"[bold]Contesto pronto in:[/bold] [bold cyan]{recap.get('context_path')}[/bold cyan]\n\n"
            "[dim]Nessuna API key impostata (o --prepare-only attivo): chiedi ad Antigravity/Gemini di generare "
            "il Markdown da questo file e poi compila il PDF con `polito recap --from-md <file.md>`.[/dim]",
            title="📝 Contesto Lezione Pronto (Audio + Slide)",
            border_style="yellow",
        ))
        return

    desktop_line = (
        f"\n[bold]Copia Desktop:[/bold] {recap['desktop_pdf_path']}"
        if recap.get("desktop_pdf_path")
        else ""
    )
    console.print(Panel(
        f"[bold]Corso:[/bold] {recap.get('course')}\n"
        f"[bold]Lezione:[/bold] {recap.get('lesson_title')} ({recap.get('lesson_date')})\n"
        f"[bold]Slide usate come contesto:[/bold] {slides_line}\n"
        f"[bold]PDF generato:[/bold] [bold cyan]{recap.get('pdf_path')}[/bold cyan]{desktop_line}",
        title="📄 Recap Lezione PDF Completato",
        border_style="green",
    ))


def cmd_login(args):
    """Verifica e salva la sessione di accesso al Portale PoliTO."""
    show_banner()
    client = PolitoClient(headless=not getattr(args, "headful", False))

    if getattr(args, "manual", False):
        console.print(
            "[bold blue]Apertura browser persistente su didattica.polito.it...[/bold blue]\n"
            "[dim]Completa manualmente il login (inclusa 2FA) e poi chiudi la finestra del browser.[/dim]"
        )
        try:
            profile_dir = client.login_interactive()
            console.print(
                f"[bold green]✔[/bold green] Sessione persistente salvata in: [dim]{profile_dir}[/dim]"
            )
        except Exception as e:
            console.print(f"[bold red]✖ Errore login manuale:[/bold red] {e}")
        return

    console.print(f"[bold blue]Tentativo di connessione con utente:[/bold blue] [green]{POLITO_USERNAME or 'Non impostato!'}[/green]")
    if not POLITO_USERNAME:
        console.print(
            "[bold red]Attenzione:[/bold red] Imposta POLITO_USERNAME e POLITO_PASSWORD in [yellow].env[/yellow] "
            "oppure usa [cyan]polito login --manual[/cyan]."
        )
        return

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        with console.status("[bold green]Apertura portale e autenticazione...[/bold green]", spinner="dots"):
            browser, context = client.get_context(p, headless=not args.headful)
            try:
                client.ensure_authenticated_page(context)
                client.save_session(context)
                console.print(f"[bold green]✔[/bold green] Autenticazione riuscita! Sessione salvata in: [dim]{SESSION_FILE}[/dim]")
            except Exception as e:
                console.print(f"[bold red]✖ Errore di autenticazione:[/bold red] {e}")
            finally:
                browser.close()


def cmd_courses(args):
    """Elenca i corsi iscritti nel Portale della Didattica."""
    show_banner()
    client = PolitoClient(headless=not args.headful)
    with console.status("[bold blue]Recupero elenco corsi dal Portale...[/bold blue]", spinner="dots"):
        try:
            courses = client.get_enrolled_courses()
        except Exception as e:
            console.print(f"[bold red]Errore:[/bold red] {e}")
            return

    if not courses:
        console.print("[yellow]Nessun corso rilevato o sessione non attiva.[/yellow]")
        return

    table = Table(title="📚 Corsi Iscritti nel Portale della Didattica", border_style="cyan")
    table.add_column("Codice", style="magenta", justify="center")
    table.add_column("Insegnamento", style="bold white")
    table.add_column("CFU", style="yellow", justify="center")
    table.add_column("Incarico", style="dim cyan", justify="center")

    for c in courses:
        cfu_str = str(c.get("cfu")) if c.get("cfu") is not None else "-"
        id_str = str(c.get("id_inc")) if c.get("id_inc") is not None else "-"
        table.add_row(c.get("code") or "-", c["name"], cfu_str, id_str)
        if c.get("modules"):
            for m in c["modules"]:
                m_id = str(m.get("id_inc")) if m.get("id_inc") else "-"
                teacher = f" ({m['teacher']})" if m.get("teacher") else ""
                table.add_row("", f"  [dim]└── Modulo: {m['name']}{teacher}[/dim]", "-", m_id)

    console.print(table)


def cmd_local(args):
    """Mostra i file e i corsi attualmente organizzati sul Desktop."""
    show_banner()
    organizer = FileOrganizer()
    summary = organizer.get_summary()

    console.print(Panel(
        f"[bold]Cartella Destinazione:[/bold] {summary['base_dir']}\n"
        f"[bold]Corsi organizzati:[/bold] {summary['courses_count']}\n"
        f"[bold]Totale file salvati:[/bold] {summary['total_files']} ({summary['total_size_mb']} MB)",
        title="📁 Stato File PoliTO sul Desktop",
        border_style="green",
    ))

    courses = organizer.list_courses()
    if courses:
        table = Table(title="Corsi presenti sul Desktop", border_style="blue")
        table.add_column("Corso", style="bold green")
        table.add_column("Categorie create", style="cyan")

        for c in courses:
            c_dir = organizer.base_dir / c
            cats = [d.name for d in c_dir.iterdir() if d.is_dir()]
            table.add_row(c, ", ".join(cats) or "(nessuna sottocartella)")
        console.print(table)


def _normalize_vc_date(raw_date: str) -> str:
    """Converts DD/MM/YYYY to YYYY-MM-DD, falling back to today's date."""
    from datetime import datetime
    m = re.search(r"\b(\d{2})/(\d{2})/(\d{4})\b", raw_date or "")
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return datetime.now().strftime("%Y-%m-%d")


def _render_videos_table(course_name: str, videos: List[Dict[str, Any]], organizer: FileOrganizer):
    """
    Renders the course video table distinguishing:
    - already transcribed / PDF generated
    - matching slides available on Desktop
    - neither transcribed nor slides matched
    """
    clean_course = organizer.sanitize_name(course_name)
    course_dir = organizer.base_dir / clean_course
    context_dirs = [
        d for d in (
            course_dir / "context",
            course_dir / "context" / "used",
            course_dir / "context" / "contest used",
        )
        if d.exists()
    ]
    appunti_dirs = [
        d for d in (
            course_dir / "Appunti_Lezioni",
            course_dir / "appunti",
        )
        if d.exists()
    ]
    matcher = SlideMatcher(organizer=organizer)

    def _find_lesson_file(dirs: list[Path], stem: str, date_prefix: str, ext: str) -> Path | None:
        for d in dirs:
            exact = d / f"{stem}{ext}"
            if exact.exists():
                return exact
        if date_prefix and date_prefix != "undated":
            for d in dirs:
                matches = sorted(d.glob(f"{date_prefix}_*{ext}"))
                if matches:
                    return matches[0]
        return None

    table = Table(title=f"🎥 Videolezioni: {course_name}", border_style="cyan")
    table.add_column("#", style="bold magenta", justify="right")
    table.add_column("Data", style="yellow")
    table.add_column("Titolo", style="bold white")
    table.add_column("Sbobina / PDF", justify="center")
    table.add_column("Slide Desktop")

    count_transcribed = 0
    count_with_slides = 0
    count_no_sub_no_slide = 0

    for v in videos:
        norm_date = _normalize_vc_date(v.get("date", ""))
        stem = format_lesson_filename_stem(v["title"], norm_date)

        ctx_file = _find_lesson_file(context_dirs, stem, norm_date, ".md")
        pdf_desktop = _find_lesson_file(appunti_dirs, stem, norm_date, ".pdf")
        pdf_local = PDF_OUTPUT_DIR / f"{stem}.pdf"

        has_pdf = (pdf_desktop is not None) or pdf_local.exists()
        has_ctx = ctx_file is not None
        is_done = has_pdf or has_ctx

        if has_pdf and has_ctx:
            sbobina_cell = "[bold green]✔ Context + PDF[/bold green]"
        elif has_pdf:
            sbobina_cell = "[bold green]✔ PDF[/bold green]"
        elif has_ctx:
            sbobina_cell = "[cyan]✔ Sbobinato[/cyan]"
        else:
            sbobina_cell = "[dim]— Da sbobinare[/dim]"

        # Determine slide status (from saved context if present, else fast title lookup)
        slide_name = None
        if has_ctx:
            try:
                head_txt = ctx_file.read_text(encoding="utf-8", errors="ignore")[:1500]
                m_slide = re.search(r"^###\s+Slide(?:\s+Riconosciute)?:\s+([^\s(]+)", head_txt, re.M)
                if m_slide and m_slide.group(1).lower() != "nessuna":
                    slide_name = m_slide.group(1).strip()
            except Exception:
                pass
        if slide_name is None and not has_ctx:
            slide_name = matcher.match_slide_by_title(v["title"], clean_course)

        if slide_name:
            slide_cell = f"[green]✔ {slide_name}[/green]"
        else:
            slide_cell = "[dim yellow]— Nessuna slide[/dim yellow]"

        if is_done:
            count_transcribed += 1
        if slide_name:
            count_with_slides += 1
        if not is_done and not slide_name:
            count_no_sub_no_slide += 1

        table.add_row(str(v["index"]), v["date"] or "-", v["title"], sbobina_cell, slide_cell)

    console.print(table)
    console.print(
        f"[bold]Riepilogo:[/bold] "
        f"[cyan]{count_transcribed} già sbobinati[/cyan] | "
        f"[green]{count_with_slides} con slide relative[/green] | "
        f"[yellow]{count_no_sub_no_slide} senza sbobina e senza slide[/yellow]"
    )


def cmd_videos(args):
    """Elenca le videolezioni disponibili nella Virtual Classroom di un corso."""
    show_banner()
    course_query = " ".join(args.course).strip()
    kw = course_query.lower()
    mapped = COURSE_ALIASES.get(kw, kw)

    client = PolitoClient()
    organizer = FileOrganizer()
    with console.status(f"[bold blue]Ricerca videolezioni per '{course_query}'...[/bold blue]", spinner="dots"):
        enrolled = client.get_enrolled_courses()
        target = next(
            (c for c in enrolled if kw in c["name"].lower() or mapped in c["name"].lower()),
            None,
        )
        if not target:
            console.print(f"[bold red]Corso '{course_query}' non trovato.[/bold red]")
            return
        videos = client.list_course_videos(target)

    if not videos:
        console.print(f"[yellow]Nessuna videolezione trovata per {target['name']}.[/yellow]")
        return

    _render_videos_table(target["name"], videos, organizer)
    console.print(
        f"[dim]Per generare il recap di una lezione usa:[/dim] "
        f"[cyan]polito recap --course \"{target['name']}\" --lesson 1[/cyan]"
    )


def cmd_recap(args):
    """Scarica/trascrive una videolezione, riconosce le slide e genera il PDF."""
    show_banner()
    if not any([args.url, args.audio, args.transcript, args.from_md, (args.course and args.lesson)]):
        console.print(
            "[bold red]Errore:[/bold red] Specifica almeno uno tra [cyan]--url[/cyan], "
            "[cyan]--audio[/cyan], [cyan]--transcript[/cyan], [cyan]--from-md[/cyan] "
            "oppure [cyan]--course + --lesson[/cyan]."
        )
        return

    processor = LectureProcessor()
    try:
        recap = processor.run_recap(
            lesson_url=args.url,
            audio_path=Path(args.audio) if args.audio else None,
            transcript_path=Path(args.transcript) if args.transcript else None,
            markdown_path=Path(args.from_md) if args.from_md else None,
            course_hint=args.course,
            lesson_selector=args.lesson,
            explicit_slides=Path(args.slides) if args.slides else None,
            sync_slides=args.sync_slides,
            keep_temp=args.keep_temp,
            prepare_only=args.prepare_only,
        )
        _print_recap_result(recap)
    except Exception as e:
        console.print(f"[bold red]✖ Errore generazione recap:[/bold red] {e}")


def parse_number_selection(raw: str, max_num: int) -> List[int]:
    """
    Parses a space/comma-separated list of numbers or ranges (e.g. '4 5 2', '12, 2', '1-3 5')
    into an ordered, deduplicated list of valid 1-based indices.
    """
    tokens = [t.strip() for t in re.split(r"[\s,;]+", raw.strip()) if t.strip()]
    selected: List[int] = []
    seen = set()

    for tok in tokens:
        m_range = re.match(r"^(\d+)-(\d+)$", tok)
        if m_range:
            start, end = int(m_range.group(1)), int(m_range.group(2))
            step = 1 if start <= end else -1
            for n in range(start, end + step, step):
                if 1 <= n <= max_num and n not in seen:
                    seen.add(n)
                    selected.append(n)
                elif not (1 <= n <= max_num):
                    console.print(f"[yellow]Numero fuori intervallo ignorato: {n}[/yellow]")
        elif tok.isdigit():
            n = int(tok)
            if 1 <= n <= max_num:
                if n not in seen:
                    seen.add(n)
                    selected.append(n)
            else:
                console.print(f"[yellow]Numero fuori intervallo ignorato: {n}[/yellow]")
        else:
            console.print(f"[yellow]Valore non valido ignorato: {tok}[/yellow]")

    return selected


def _load_courses_for_menu(client: PolitoClient, force_refresh: bool = False) -> List[Dict[str, Any]]:
    """Loads enrolled courses from cache (fast) or fetches from portal if missing/forced."""
    cache_file = DATA_DIR / "enrolled_courses.json"
    courses: List[Dict[str, Any]] = []
    if not force_refresh and cache_file.exists():
        try:
            courses = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            courses = []
    if not courses:
        with console.status("[bold blue]Recupero corsi dal Portale della Didattica...[/bold blue]", spinner="dots"):
            courses = client.get_enrolled_courses()
    return [c for c in courses if c.get("id_inc")]


def cmd_interactive():
    """Modalità interattiva principale lanciata con `polito`: scegli corso e video per numero."""
    show_banner()
    client = PolitoClient()
    organizer = FileOrganizer()
    # Automatic housekeeping: remove any temp audio whose PDF was already generated
    LectureProcessor(client=client, organizer=organizer).cleanup_completed_audio_in_temp()

    try:
        courses = _load_courses_for_menu(client)
    except Exception as e:
        console.print(f"[bold red]Errore recupero corsi:[/bold red] {e}")
        return

    if not courses:
        console.print("[yellow]Nessun corso trovato. Esegui prima `polito login`.[/yellow]")
        return

    while True:
        table = Table(title="📚 Seleziona un Corso", border_style="cyan")
        table.add_column("#", style="bold magenta", justify="right")
        table.add_column("Insegnamento", style="bold white")
        table.add_column("Codice", style="cyan", justify="center")
        table.add_column("Cartella Desktop", style="dim green")

        for idx, c in enumerate(courses, start=1):
            c_dir = organizer.base_dir / organizer.sanitize_name(c["name"])
            local_status = "✔ presente" if c_dir.exists() else "-"
            table.add_row(str(idx), c["name"], c.get("code") or "-", local_status)

        console.print(table)
        try:
            choice = console.input(
                "[bold cyan]Numero corso[/bold cyan] "
                "[dim](es: 4 | 's 4' sincronizza slide | 'r' aggiorna corsi | 'q' esci)[/dim]: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Uscita.[/dim]")
            return

        if not choice or choice.lower() in {"q", "exit", "quit", "esci"}:
            return

        if choice.lower() == "r":
            try:
                courses = _load_courses_for_menu(client, force_refresh=True)
            except Exception as e:
                console.print(f"[bold red]Errore aggiornamento:[/bold red] {e}")
            continue

        sync_only = False
        m_sync = re.match(r"^s\s+(\d+)$", choice, re.I)
        if m_sync:
            sync_only = True
            choice = m_sync.group(1)

        if not choice.isdigit() or not (1 <= int(choice) <= len(courses)):
            console.print(f"[red]Inserisci un numero valido tra 1 e {len(courses)}.[/red]")
            continue

        selected_course = courses[int(choice) - 1]

        if sync_only:
            with console.status(f"[bold green]Sincronizzazione materiali per {selected_course['name']}...[/bold green]", spinner="dots"):
                mats = client.crawl_course_materials(selected_course)
                downloaded = client.download_materials_batch(mats)
            console.print(f"[bold green]✔[/bold green] Sincronizzazione completata ({len(downloaded)} file elaborati).")
            continue

        with console.status(
            f"[bold blue]Ricerca videolezioni per {selected_course['name']}...[/bold blue]",
            spinner="dots",
        ):
            try:
                videos = client.list_course_videos(selected_course)
            except Exception as e:
                console.print(f"[bold red]Errore recupero video:[/bold red] {e}")
                continue

        if not videos:
            console.print(f"[yellow]Nessuna videolezione trovata per {selected_course['name']}.[/yellow]")
            continue

        _render_videos_table(selected_course["name"], videos, organizer)

        try:
            raw_nums = console.input(
                "[bold cyan]Numeri video da scaricare/sbobinare[/bold cyan] "
                "[dim](es: 4 5 2 oppure 12 2 | 'b' indietro | 'q' esci)[/dim]: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Uscita.[/dim]")
            return

        if not raw_nums or raw_nums.lower() in {"b", "back", "indietro"}:
            continue
        if raw_nums.lower() in {"q", "exit", "quit", "esci"}:
            return

        indices = parse_number_selection(raw_nums, len(videos))
        if not indices:
            console.print("[yellow]Nessun numero valido selezionato.[/yellow]")
            continue

        processor = LectureProcessor(client=client, organizer=organizer)
        results: List[Dict[str, Any]] = []
        for pos, vid_idx in enumerate(indices, start=1):
            vid = videos[vid_idx - 1]
            console.print(
                f"\n[bold yellow][{pos}/{len(indices)}][/bold yellow] "
                f"Elaborazione [bold white]#{vid_idx} — {vid['title']}[/bold white] ({vid['date']})..."
            )
            try:
                res = processor.run_recap(
                    course_hint=selected_course["name"],
                    lesson_selector=str(vid_idx),
                    preloaded_course=selected_course,
                    preloaded_video=vid,
                )
                results.append(res)
                _print_recap_result(res)
            except Exception as e:
                console.print(f"[bold red]✖ Errore sul video #{vid_idx}:[/bold red] {e}")

        if results:
            console.print(
                f"\n[bold green]✔ Completati {len(results)}/{len(indices)} video[/bold green] "
                f"per [bold]{selected_course['name']}[/bold].\n"
            )


def cmd_ask(args):
    """Interpreta ed esegue un comando in linguaggio naturale con l'Agente AI."""
    show_banner()
    query = " ".join(args.prompt)
    console.print(f"[bold cyan]Richiesta:[/bold cyan] \"{query}\"")

    agent = PolitoAgent()
    with console.status("[bold yellow]L'Agente sta elaborando la richiesta...[/bold yellow]", spinner="earth"):
        try:
            result = agent.execute(query)
        except Exception as e:
            console.print(f"[bold red]Errore durante l'esecuzione:[/bold red] {e}")
            return

    if result.get("success"):
        console.print(f"[bold green]✔[/bold green] {result.get('message')}")
        if result.get("recap"):
            _print_recap_result(result["recap"])
        if result.get("downloaded_files"):
            table = Table(title="File Scaricati & Organizzati", border_style="green")
            table.add_column("Corso", style="cyan")
            table.add_column("File", style="bold white")
            table.add_column("Posizione sul Desktop", style="dim")
            for f in result["downloaded_files"]:
                table.add_row(f["course"], f["filename"], f["saved_to"])
            console.print(table)
    else:
        console.print(f"[bold red]✖[/bold red] {result.get('message')}")
        if result.get("available_courses"):
            console.print("[dim]Corsi disponibili:[/dim]")
            for c in result["available_courses"]:
                console.print(f" - {c}")


def main():
    parser = argparse.ArgumentParser(description="PoliTO Materials & Lecture Recap Agent")
    subparsers = parser.add_subparsers(dest="command", help="Comando da eseguire")

    # Command: login
    p_login = subparsers.add_parser("login", help="Effettua il login e salva la sessione")
    p_login.add_argument("--headful", action="store_true", help="Mostra la finestra del browser (utile per MFA)")
    p_login.add_argument("--manual", action="store_true", help="Apre browser persistente su didattica.polito.it per login manuale senza .env")

    # Command: courses
    p_courses = subparsers.add_parser("courses", help="Elenca i corsi dal Portale della Didattica")
    p_courses.add_argument("--headful", action="store_true", help="Mostra il browser")

    # Command: local
    subparsers.add_parser("local", help="Mostra i corsi e file già organizzati sul Desktop")

    # Command: videos
    p_videos = subparsers.add_parser("videos", help="Elenca le videolezioni disponibili per un corso")
    p_videos.add_argument("course", nargs="+", help="Nome o sigla del corso (es. 'OOP' o 'Sistemi Operativi')")

    # Command: recap (alias: sbobina)
    p_recap = subparsers.add_parser(
        "recap",
        aliases=["sbobina"],
        help="Trascrive una videolezione, riconosce le slide e genera gli appunti in PDF",
    )
    p_recap.add_argument("--url", help="URL della videolezione sul Portale della Didattica")
    p_recap.add_argument("--audio", help="Percorso locale a un file audio/video già scaricato")
    p_recap.add_argument("--transcript", help="Percorso a una trascrizione .txt esistente")
    p_recap.add_argument("--from-md", help="Compila direttamente il PDF da un file Markdown già generato")
    p_recap.add_argument("--course", help="Nome o sigla del corso (opzionale con --url/--audio; richiesto con --lesson)")
    p_recap.add_argument("--lesson", help="Numero, data (es. 21/09/2026) o titolo della lezione da `polito videos`")
    p_recap.add_argument("--slides", help="Percorso esplicito al PDF delle slide (opzionale, altrimenti riconosciute in automatico)")
    p_recap.add_argument("--sync-slides", action="store_true", default=True, help="Sincronizza i PDF mancanti dal portale (attivo di default)")
    p_recap.add_argument("--no-sync-slides", dest="sync_slides", action="store_false", help="Salta la sincronizzazione automatica delle slide dal portale")
    p_recap.add_argument("--prepare-only", action="store_true", help="Prepara solo trascrizione + slide in context/ senza chiamare API esterne")
    p_recap.add_argument("--keep-temp", action="store_true", help="Conserva audio, trascrizione e Markdown in temp/")

    # Command: menu (anche default quando si lancia `polito` senza argomenti)
    subparsers.add_parser("menu", help="Avvia la modalità interattiva a numeri (default senza argomenti)")

    # Command: ask
    p_ask = subparsers.add_parser("ask", help="Invia una richiesta in linguaggio naturale all'agente")
    p_ask.add_argument("prompt", nargs="+", help="La richiesta (es: 'scarica le slide di Informatica' o 'recap <URL>')")

    args = parser.parse_args()

    if args.command == "login":
        cmd_login(args)
    elif args.command == "courses":
        cmd_courses(args)
    elif args.command == "local":
        cmd_local(args)
    elif args.command == "videos":
        cmd_videos(args)
    elif args.command in ("recap", "sbobina"):
        cmd_recap(args)
    elif args.command == "ask":
        cmd_ask(args)
    else:
        cmd_interactive()


if __name__ == "__main__":
    main()
