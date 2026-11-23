"""
Lecture Processor module for PoliTO Agent.
Handles lecture audio capture, local Whisper transcription, automatic slide recognition,
LLM technical note synthesis, Pandoc/XeLaTeX PDF generation, and temp cleanup.
"""

import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import (
    API_BASE_URL,
    API_KEY,
    GEMINI_API_KEY,
    MODEL_NAME,
    PDF_OUTPUT_DIR,
    TEMP_DIR,
    ASR_MODEL,
    ASR_MODEL_EN,
    ASR_MODEL_IT,
    ASR_QUANTIZATION,
)
from file_organizer import FileOrganizer
from polito_client import PolitoClient
from slide_matcher import SlideMatch, SlideMatcher

logger = logging.getLogger("lecture_processor")

TUTOR_SYSTEM_PROMPT = """Sei un tutor universitario esperto di Ingegneria Informatica al Politecnico di Torino.
Ricevi la trascrizione con timestamp di una videolezione universitaria e, quando disponibili, il testo estratto dalle slide ufficiali del corso su cui si basa la lezione.

Il tuo obiettivo è trasformare questo materiale in appunti universitari completi, rigorosi, chiari e pronti per la stampa in PDF (Markdown + LaTeX compilato con Pandoc/XeLaTeX).

Regole obbligatorie di redazione:
1. Pulizia e Struttura:
   - Rimuovi intercalari, esitazioni, frasi spezzate e ripetizioni del parlato.
   - Struttura la lezione in capitoli e sottoparagrafi chiari con intestazioni Markdown (`#`, `##`, `###`).
2. Rigore Matematico e LaTeX:
   - Formalizza in LaTeX (`$...$` inline e `$$...$$` in blocco) ogni formula, equazione, definizione matematica, dimostrazione o complessità asintotica (es. $O(n \\log n)$, $\\Theta(n)$).
   - Non usare caratteri Unicode matematici isolati fuori dal LaTeX (scrivi sempre `$\\alpha$`, `$\\le$`, `$\\rightarrow$`, `$\\forall$`, ecc.).
3. Codice Completo e Diagrammi:
   - Aggiungi autonomamente **implementazioni complete, funzionanti e ben commentate** (in C, Python, Java, Assembly o pseudocodice a seconda della materia/argomento) per ogni algoritmo, struttura dati, protocollo o meccanismo descritto dal docente.
   - Usa blocchi di codice con linguaggio esplicito (es. ```c, ```python, ```java).
   - Quando utile per chiarire flussi, architetture, alberi, automi o pacchetti, includi diagrammi in puro ASCII (`+---+`, `|`, `->`, evitare box-drawing Unicode).
4. Uso del Contesto Slide + Audio:
   - Se vengono fornite le slide di riferimento: integra in modo coerente definizioni, notazione e struttura delle slide con tutte le spiegazioni, gli esempi svolti a voce, le intuizioni e i dettagli operativi forniti dal docente nell'audio.
   - Se NON vengono fornite slide: costruisci gli appunti basandoti esclusivamente sulla trascrizione audio, mantenendo la massima precisione tecnica.
5. Sezione Finale Obbligatoria:
   - Chiudi sempre con una sezione intitolata `## Casi Limite, Errori Comuni e Domande d'Esame` che elenchi edge case, trappole tipiche d'esame e possibili domande orali/scritte sull'argomento della lezione.
"""


COMPACT_CONTEXT_INSTRUCTIONS = """Genera appunti universitari COMPLETI, DETTAGLIATI ed ESAUSTIVI in italiano (Markdown + LaTeX compilabile con Pandoc/XeLaTeX), senza omettere spiegazioni o esempi:
- Struttura in capitoli (`#`, `##`, `###`), rimuovendo solo intercalari e ripetizioni del parlato.
- Formule/dimostrazioni/complessità in LaTeX (`$...$`, `$$...$$`, niente simboli Unicode isolati fuori dal LaTeX).
- Includi codice completo e commentato SOLO se la lezione/materia riguarda programmazione, algoritmi o implementazioni software (altrimenti ometti il codice); usa diagrammi solo ASCII (`+---+`, `->`) per architetture, reti e flussi.
- Chiudi con `## Casi Limite, Errori Comuni e Domande d'Esame`.
- Output e archiviazione: crea `../Appunti_Lezioni/` se non esiste, salva lì il PDF con lo stesso nome base di questo file (`<nome>.pdf`), poi sposta questo `.md` in `used/`."""


def compact_transcript_for_context(transcript_text: str, block_seconds: int = 90) -> str:
    """
    Groups line-by-line `[MM:SS] ...` transcript lines into ~90-second paragraphs
    to significantly reduce token count in saved context/*.md files while keeping timestamps.
    """
    paragraphs: List[str] = []
    cur_words: List[str] = []
    cur_start_sec: Optional[int] = None
    cur_label = "[00:00]"

    for line in transcript_text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\[(\d+):(\d{2})\]\s*(.*)$", line)
        if not m:
            cur_words.append(line)
            continue
        sec = int(m.group(1)) * 60 + int(m.group(2))
        text = m.group(3).strip()
        if not text:
            continue
        if cur_start_sec is None:
            cur_start_sec = sec
            cur_label = f"[{m.group(1)}:{m.group(2)}]"
        elif sec - cur_start_sec >= block_seconds and cur_words:
            paragraphs.append(f"{cur_label} " + " ".join(cur_words))
            cur_words = []
            cur_start_sec = sec
            cur_label = f"[{m.group(1)}:{m.group(2)}]"
        cur_words.append(text)

    if cur_words:
        paragraphs.append(f"{cur_label} " + " ".join(cur_words))
    return "\n\n".join(paragraphs)


def infer_course_language(course_name: str = "", lesson_title: str = "") -> str:
    """
    Infers lecture language ('en' or 'it') directly from the course name/title:
    English-named PoliTO courses -> 'en', Italian-named courses -> 'it'.
    """
    combined = f"{course_name} {lesson_title}".lower()
    en_markers = {
        "programming", "operating", "systems", "technologies", "ecosystems",
        "computer", "software", "engineering", "networks", "security",
        "database", "databases", "architecture", "algorithms", "data",
        "machine", "learning", "artificial", "intelligence", "object",
        "oriented", "distributed", "cloud", "embedded", "signals",
        "lecture", "introduction",
    }
    it_markers = {
        "sistemi", "operativi", "programmazione", "oggetti", "elettromagnetismo",
        "teoria", "circuiti", "elaborazione", "segnali", "reti", "calcolatori",
        "controlli", "automatici", "algoritmi", "strutture", "dati", "fisica",
        "elettrotecnica", "analisi", "matematica", "fondamenti", "informatica",
    }
    tokens = set(re.findall(r"[a-z]+", combined))
    if tokens & it_markers:
        return "it"
    if tokens & en_markers:
        return "en"
    return "it"


def select_asr_model(language: str) -> str:
    """Selects the optimal ONNX-ASR model for the course language unless ASR_MODEL overrides it."""
    if ASR_MODEL:
        return ASR_MODEL
    return ASR_MODEL_EN if language == "en" else ASR_MODEL_IT


def sanitize_filename(name: str) -> str:
    """Sanitizes a title for safe cross-platform filenames."""
    clean = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    clean = re.sub(r"\s+", "_", clean)
    clean = re.sub(r"_+", "_", clean).strip("_")
    return clean[:100] or "Lezione_PoliTO"


def format_lesson_filename_stem(lesson_title: str, lesson_date: str) -> str:
    """
    Formats a clean filename stem as `<AAAA-MM-GG>_<Argomento>` matching Portale della Didattica.
    Examples:
      'Lezione del 17/11/2026 -Build Management (Maven) and CI' -> '2026-11-17_Build_Management_(Maven)_and_CI'
      'Lezione del 21/09/2026' -> '2026-09-21_Lezione'
    """
    topic = lesson_title.strip()
    # Strip leading YYYY-MM-DD_ if passed from temp/
    m_prefix = re.match(r"^(\d{4}-\d{2}-\d{2})_(.+)$", topic)
    if m_prefix:
        lesson_date = m_prefix.group(1)
        topic = m_prefix.group(2)

    topic_spaces = topic.replace("_", " ").strip()
    # Extract DD/MM/YYYY if present in title when lesson_date wasn't already set from it
    m_it = re.search(r"\b(\d{2})[/\s-](\d{2})[/\s-](\d{4})\b", topic_spaces)
    if m_it:
        lesson_date = f"{m_it.group(3)}-{m_it.group(2)}-{m_it.group(1)}"

    # Strip redundant 'Lezione del DD/MM/YYYY' / 'Lecture DD/MM/YYYY' wrappers
    stripped = re.sub(
        r"^(?:20\d{2}\s*[-–]\s*[^-]+?\s+)?(?:lezione\s+del|lecture)\s+\d{2}[\s/_-]\d{2}[\s/_-]\d{4}\s*[-–:.]*\s*",
        "",
        topic_spaces,
        flags=re.I,
    ).strip()
    # Also strip trailing ' - DD/MM/YYYY' (e.g. 'Course introduction - 21/09/2026')
    stripped = re.sub(r"\s*[-–:]\s*\d{2}[\s/_-]\d{2}[\s/_-]\d{4}\s*$", "", stripped).strip(" -–:.")

    argomento = sanitize_filename(stripped) if stripped else "Lezione"
    return f"{lesson_date}_{argomento}"


class LLMHelper:
    """Unified LLM wrapper supporting OpenAI-compatible endpoints (OpenRouter, etc.) and Gemini."""

    def __init__(
        self,
        api_base_url: str = API_BASE_URL,
        api_key: str = API_KEY,
        model_name: str = MODEL_NAME,
        gemini_api_key: str = GEMINI_API_KEY,
    ):
        self.api_base_url = api_base_url
        self.api_key = api_key
        self.model_name = model_name
        self.gemini_api_key = gemini_api_key
        self.gemini_client = None

        if not self.api_key and self.gemini_api_key:
            from google import genai
            self.gemini_client = genai.Client(api_key=self.gemini_api_key)

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key or self.gemini_client)

    def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> str:
        """Generates a text response using the configured LLM backend."""
        if self.api_key:
            import requests

            endpoint = f"{self.api_base_url.rstrip('/')}/chat/completions"
            resp = requests.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model_name,
                    "temperature": temperature,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
                timeout=180,
            )
            resp.raise_for_status()
            data = resp.json()
            return (data["choices"][0]["message"]["content"] or "").strip()

        if self.gemini_client is not None:
            from google.genai import types
            gemini_model = "gemini-2.5-pro"
            if "/" in self.model_name and "gemini" in self.model_name.lower():
                gemini_model = self.model_name.split("/")[-1]
            resp = self.gemini_client.models.generate_content(
                model=gemini_model,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=temperature,
                ),
            )
            return (resp.text or "").strip()

        raise RuntimeError(
            "Nessun modello LLM configurato! Imposta `API_KEY` (con `API_BASE_URL` e `MODEL_NAME`) "
            "oppure `GEMINI_API_KEY` nel file .env."
        )

    def choose_matching_slide(
        self,
        lesson_title: str,
        transcript_excerpt: str,
        candidates: List[Dict[str, str]],
    ) -> Optional[str]:
        """
        Uses the LLM to pick the exact slide PDF matching the lecture (or 'NONE').
        Returns the chosen filename, 'NONE', or None if LLM is unavailable/fails.
        """
        if not self.is_configured or not candidates:
            return None

        cand_lines = []
        for idx, c in enumerate(candidates, start=1):
            cand_lines.append(
                f"{idx}. File: {c['filename']} | Corso: {c['course']} | Categoria: {c['category']} | Score: {c['score']}\n"
                f"   Estratto prima pagina: {c['excerpt']}"
            )

        prompt = (
            f"Titolo lezione: {lesson_title}\n\n"
            f"Estratto trascrizione audio:\n{transcript_excerpt}\n\n"
            f"PDF candidati trovati sul Desktop:\n" + "\n\n".join(cand_lines) + "\n\n"
            "Quale di questi file PDF rappresenta esattamente le slide su cui si basa questa lezione? "
            "Rispondi SOLO con il nome esatto del file (es. `07_Signals.pdf`), oppure `NONE` se nessuno corrisponde davvero."
        )
        try:
            ans = self.generate(
                "Sei un assistente che associa trascrizioni di lezioni universitarie alle relative slide PDF.",
                prompt,
                temperature=0.0,
            ).strip().strip("`\"'")
            if ans.upper() == "NONE":
                return "NONE"
            for c in candidates:
                if c["filename"] == ans or c["filename"] in ans:
                    return c["filename"]
        except Exception as e:
            logger.warning(f"LLM slide selection fallback to heuristic score: {e}")
        return None


class LectureProcessor:
    def __init__(
        self,
        client: Optional[PolitoClient] = None,
        organizer: Optional[FileOrganizer] = None,
        llm: Optional[LLMHelper] = None,
        temp_dir: Path = TEMP_DIR,
        pdf_output_dir: Path = PDF_OUTPUT_DIR,
    ):
        self.organizer = organizer or FileOrganizer()
        self.client = client or PolitoClient()
        self.llm = llm or LLMHelper()
        self.slide_matcher = SlideMatcher(organizer=self.organizer)
        self.temp_dir = Path(temp_dir)
        self.pdf_output_dir = Path(pdf_output_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.pdf_output_dir.mkdir(parents=True, exist_ok=True)

    def _extract_audio_from_stream_info(self, stream_info: Dict[str, Any]) -> Dict[str, Any]:
        """Downloads audio-only MP3 from a captured stream_info dict using ffmpeg/yt-dlp."""
        lesson_date = stream_info["date"]
        lesson_title = stream_info["title"]
        prefix = f"{lesson_date}_{sanitize_filename(lesson_title)}"

        out_template = self.temp_dir / f"{prefix}.%(ext)s"
        expected_mp3 = self.temp_dir / f"{prefix}.mp3"
        stream_url = stream_info["stream_url"]
        referer = stream_info.get("referer") or ""
        cookies_path = stream_info.get("cookies_file") or (self.temp_dir / "cookies.txt")

        lower_url = stream_url.lower().split("?")[0]
        is_direct_media = any(lower_url.endswith(ext) for ext in (".mp4", ".m4a", ".webm", ".mp3", ".ogg"))

        if is_direct_media and shutil.which("ffmpeg"):
            logger.info(f"Estrazione diretta solo audio con ffmpeg -> {expected_mp3.name}")
            ff_cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
            if referer:
                ff_cmd.extend(["-headers", f"Referer: {referer}\r\n"])
            if stream_info.get("user_agent"):
                ff_cmd.extend(["-user_agent", stream_info["user_agent"]])
            ff_cmd.extend(["-i", stream_url, "-vn", "-acodec", "libmp3lame", "-q:a", "5", str(expected_mp3)])
            ff_proc = subprocess.run(ff_cmd, capture_output=True, text=True)
            if ff_proc.returncode == 0 and expected_mp3.exists() and expected_mp3.stat().st_size > 0:
                return {
                    "audio_path": expected_mp3,
                    "prefix": prefix,
                    "title": lesson_title,
                    "date": lesson_date,
                    "course_hint": stream_info.get("course_hint", ""),
                    "page_context": stream_info.get("page_context", ""),
                }
            logger.warning(f"ffmpeg diretto fallito, provo con yt-dlp: {ff_proc.stderr.strip()}")

        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--cookies", str(cookies_path),
            "--referer", referer,
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "5",
            "-o", str(out_template),
            stream_url,
        ]
        if stream_info.get("user_agent"):
            cmd.extend(["--user-agent", stream_info["user_agent"]])

        logger.info(f"Avvio download solo audio con yt-dlp -> {expected_mp3.name}")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"Errore yt-dlp (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
            )

        if not expected_mp3.exists():
            mp3_candidates = sorted(self.temp_dir.glob(f"{prefix}*.mp3"))
            if not mp3_candidates:
                raise FileNotFoundError(f"File MP3 atteso non trovato in {self.temp_dir}")
            expected_mp3 = mp3_candidates[0]

        return {
            "audio_path": expected_mp3,
            "prefix": prefix,
            "title": lesson_title,
            "date": lesson_date,
            "course_hint": stream_info.get("course_hint", ""),
            "page_context": stream_info.get("page_context", ""),
        }

    def download_audio_from_url(self, lesson_url: str) -> Dict[str, Any]:
        """
        Captures the lecture stream via Playwright (or refreshes expired video.polito.it links)
        and downloads audio-only MP3. Always deletes temp/cookies.txt immediately afterward.
        """
        cookies_path = self.temp_dir / "cookies.txt"
        try:
            stream_info = self.client.capture_lecture_stream(lesson_url, cookies_path)
            return self._extract_audio_from_stream_info(stream_info)
        finally:
            if cookies_path.exists():
                try:
                    cookies_path.unlink()
                    logger.info("File temporaneo cookies.txt eliminato.")
                except Exception as e:
                    logger.warning(f"Impossibile eliminare {cookies_path}: {e}")

    def download_audio_from_course_lesson(
        self,
        course_hint: str,
        lesson_selector: str,
        course_info: Optional[Dict[str, Any]] = None,
        video_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Finds a course Virtual Classroom recording by index, date, or title and downloads audio-only MP3."""
        target_course = course_info
        chosen = video_info

        if target_course is None:
            from config import COURSE_ALIASES
            enrolled = self.client.get_enrolled_courses()
            kw = course_hint.lower().strip()
            mapped = COURSE_ALIASES.get(kw, kw)
            for c in enrolled:
                c_low = c["name"].lower()
                if kw in c_low or mapped in c_low:
                    target_course = c
                    break
            if not target_course:
                raise ValueError(f"Corso '{course_hint}' non trovato tra i tuoi corsi iscritti.")

        if chosen is None:
            videos = self.client.list_course_videos(target_course)
            if not videos:
                raise RuntimeError(f"Nessuna registrazione trovata nella Virtual Classroom di {target_course['name']}.")

            sel = lesson_selector.strip()
            if sel.isdigit():
                idx = int(sel)
                for v in videos:
                    if v["index"] == idx:
                        chosen = v
                        break
            if chosen is None:
                sel_low = sel.lower()
                for v in videos:
                    if sel_low in v["title"].lower() or sel_low in v["date"].lower() or sel_low == v["bbb_id"].lower():
                        chosen = v
                        break
            if chosen is None:
                raise ValueError(
                    f"Nessuna lezione corrispondente a '{lesson_selector}' in {target_course['name']}. "
                    f"Usa `polito videos \"{target_course['name']}\"` per vedere l'elenco."
                )

        cookies_path = self.temp_dir / "cookies.txt"
        try:
            stream_info = self.client.get_fresh_video_stream(
                id_inc=chosen["id_inc"],
                bbb_id=chosen["bbb_id"],
                cookies_out_path=cookies_path,
                course_info=target_course,
                lesson_hint_title=chosen["title"],
                lesson_hint_date=chosen["date"],
            )
            return self._extract_audio_from_stream_info(stream_info)
        finally:
            if cookies_path.exists():
                try:
                    cookies_path.unlink()
                    logger.info("File temporaneo cookies.txt eliminato.")
                except Exception:
                    pass

    def transcribe_audio(
        self,
        audio_path: Path,
        transcript_path: Path,
        context_prompt: str = "",
        language: str = "it",
    ) -> str:
        """
        Transcribes an audio file locally using onnx-asr (Parakeet TDT) + Silero VAD
        with [MM:SS] timestamps and saves a backup copy in transcript_path.
        Uses ffmpeg to decode and normalize audio to 16kHz mono float32 PCM.
        """
        import numpy as np
        import onnx_asr
        import onnxruntime as ort

        model_name = select_asr_model(language)
        threads = min(8, os.cpu_count() or 4)
        logger.info(
            f"Trascrizione locale con onnx-asr (modello={model_name}, "
            f"language={language}, quantization={ASR_QUANTIZATION}, threads={threads})..."
        )

        audio_input: Any = str(audio_path)
        duration_sec = 0.0
        if shutil.which("ffmpeg"):
            dec = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(audio_path),
                    "-af", "highpass=f=80,dynaudnorm=f=150:g=15",
                    "-f", "f32le", "-acodec", "pcm_f32le",
                    "-ac", "1", "-ar", "16000", "pipe:1",
                ],
                capture_output=True,
            )
            if dec.returncode == 0 and dec.stdout:
                audio_input = np.frombuffer(dec.stdout, dtype=np.float32)
                duration_sec = len(audio_input) / 16000.0

        sess_options = ort.SessionOptions()
        sess_options.intra_op_num_threads = threads
        sess_options.inter_op_num_threads = 1

        quant = ASR_QUANTIZATION or None
        model = onnx_asr.load_model(
            model_name,
            quantization=quant,
            sess_options=sess_options,
        )
        vad = onnx_asr.load_vad("silero", sess_options=sess_options)
        vad_model = model.with_vad(
            vad,
            batch_size=2,
            threshold=0.25,
            min_speech_duration_ms=100,
            min_silence_duration_ms=1500,
            speech_pad_ms=400,
            max_speech_duration_s=30,
        )
        segments = vad_model.recognize(audio_input, sample_rate=16000)

        lines = []
        kept_sec = 0.0
        total_sec_float = duration_sec if duration_sec > 0 else 0.0

        try:
            from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn
        except ImportError:
            Progress = None  # type: ignore[assignment]

        if Progress is not None and total_sec_float > 0:
            with Progress(
                SpinnerColumn(),
                TextColumn("[bold cyan]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TextColumn("({task.fields[pos]}/{task.fields[tot]})"),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
            ) as progress:
                tot_m, tot_s = divmod(int(total_sec_float), 60)
                task = progress.add_task(
                    "Trascrizione",
                    total=total_sec_float,
                    pos="00:00",
                    tot=f"{tot_m:02d}:{tot_s:02d}",
                )
                for seg in segments:
                    total_sec = max(0, int(seg.start))
                    end_sec = min(total_sec_float, max(0.0, float(seg.end)))
                    kept_sec += max(0.0, float(seg.end) - float(seg.start))
                    minutes, seconds = divmod(total_sec, 60)
                    text = (seg.text or "").strip()
                    if text:
                        lines.append(f"[{minutes:02d}:{seconds:02d}] {text}")
                    progress.update(task, completed=end_sec, pos=f"{minutes:02d}:{seconds:02d}")
                progress.update(task, completed=total_sec_float)
        else:
            last_logged_min = -5
            for seg in segments:
                total_sec = max(0, int(seg.start))
                kept_sec += max(0.0, float(seg.end) - float(seg.start))
                minutes, seconds = divmod(total_sec, 60)
                text = (seg.text or "").strip()
                if text:
                    lines.append(f"[{minutes:02d}:{seconds:02d}] {text}")
                if minutes - last_logged_min >= 5:
                    last_logged_min = minutes
                    logger.info(f"Trascrizione in corso: [{minutes:02d}:{seconds:02d}]...")

        transcript_text = "\n".join(lines)
        if duration_sec > 0:
            removed_sec = max(0.0, duration_sec - kept_sec)
            logger.info(
                f"Riepilogo VAD: durata totale {duration_sec/60:.1f} min | "
                f"parlato tenuto {kept_sec/60:.1f} min | silenzi rimossi {removed_sec/60:.1f} min"
            )
        if duration_sec > 120 and len(transcript_text) < (duration_sec / 60.0) * 120:
            logger.warning(
                f"Trascrizione insolitamente corta ({len(transcript_text)} char su {duration_sec/60:.1f} min): "
                "possibile audio con poco parlato o volume molto basso."
            )
        if not transcript_text.strip():
            raise RuntimeError("Trascrizione vuota: nessun parlato rilevato nell'audio.")

        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(transcript_text + "\n", encoding="utf-8")
        logger.info(f"Trascrizione salvata in {transcript_path}")
        return transcript_text

    def synthesize_notes_markdown(
        self,
        transcript_text: str,
        lesson_title: str,
        lesson_date: str,
        course_name: str,
        slide_match: Optional[SlideMatch],
        md_path: Path,
    ) -> str:
        """Generates structured university notes in Markdown+LaTeX via LLM."""
        if slide_match is not None:
            context_header = (
                f"SLIDE DI RIFERIMENTO RICONOSCIUTE: {slide_match.filename} "
                f"(Corso: {slide_match.course}, Pagine: {slide_match.page_count})\n\n"
                f"=== CONTENUTO ESTRATTO DALLE SLIDE ===\n{slide_match.extracted_text}\n"
                f"=== FINE SLIDE ===\n\n"
            )
            mode_note = (
                "Modalità contestuale: AUDIO + SLIDE. Allinea notazione, formule e definizioni "
                "alle slide fornite e arricchisci con ogni spiegazione ed esempio dalla trascrizione."
            )
        else:
            context_header = "NESSUNA SLIDE CORRISPONDENTE TROVATA: basa gli appunti esclusivamente sulla trascrizione audio.\n\n"
            mode_note = "Modalità contestuale: SOLO AUDIO."

        user_prompt = (
            f"Corso: {course_name or 'Ingegneria Informatica - PoliTO'}\n"
            f"Titolo Lezione: {lesson_title}\n"
            f"Data: {lesson_date}\n"
            f"{mode_note}\n\n"
            f"{context_header}"
            f"=== TRASCRIZIONE AUDIO CON TIMESTAMP ===\n{transcript_text}\n"
            f"=== FINE TRASCRIZIONE ===\n\n"
            "Genera ora gli appunti universitari completi in Markdown."
        )

        logger.info("Generazione appunti tecnici tramite LLM...")
        markdown_content = self.llm.generate(TUTOR_SYSTEM_PROMPT, user_prompt)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown_content + "\n", encoding="utf-8")
        logger.info(f"Markdown generato salvato in {md_path}")
        return markdown_content

    @staticmethod
    def _pick_installed_font(candidates: List[str], default: str) -> str:
        """Returns the first font family installed on the system via fc-list."""
        try:
            res = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=5)
            families = {line.split(",")[0].strip() for line in res.stdout.splitlines() if line.strip()}
            for cand in candidates:
                if cand in families:
                    return cand
        except Exception:
            pass
        return default

    def compile_pdf(
        self,
        md_path: Path,
        pdf_path: Path,
        course_name: str,
        lesson_title: str,
        lesson_date: str,
        slide_match: Optional[SlideMatch],
    ) -> Path:
        """Compiles Markdown notes to PDF using pandoc and xelatex."""
        if not shutil.which("pandoc"):
            raise RuntimeError(
                f"Comando `pandoc` non trovato. Il file Markdown è stato salvato in {md_path}. "
                "Esegui `./setup.sh` su Fedora per installare pandoc e xelatex."
            )
        if not shutil.which("xelatex"):
            raise RuntimeError(
                f"Comando `xelatex` non trovato. Il file Markdown è stato salvato in {md_path}. "
                "Esegui `./setup.sh` su Fedora per installare texlive-xetex."
            )

        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        monofont = self._pick_installed_font(["Liberation Mono", "Noto Sans Mono", "DejaVu Sans Mono"], "Liberation Mono")
        mainfont = self._pick_installed_font(["Liberation Sans", "Noto Sans", "DejaVu Sans"], "Liberation Sans")

        slides_label = slide_match.filename if slide_match else "Nessuna slide corrispondente (solo audio)"
        raw_course = course_name or "Politecnico di Torino"
        display_title = lesson_title.replace("_", " ").strip() or lesson_title

        def _escape_tex(val: str) -> str:
            for ch, rep in [
                ("\\", "\\textbackslash{}"),
                ("&", "\\&"),
                ("%", "\\%"),
                ("$", "\\$"),
                ("#", "\\#"),
                ("_", "\\_"),
                ("{", "\\{"),
                ("}", "\\}"),
            ]:
                val = val.replace(ch, rep)
            return val

        tex_course = _escape_tex(raw_course)
        tex_date = _escape_tex(lesson_date)

        header_tex = self.temp_dir / f"{pdf_path.stem}_header.tex"
        header_tex.write_text(
            "\\usepackage{fancyhdr}\n"
            "\\pagestyle{fancy}\n"
            "\\fancyhf{}\n"
            f"\\lhead{{\\small\\textbf{{{tex_course}}}}}\n"
            f"\\rhead{{\\small {tex_date}}}\n"
            "\\cfoot{\\thepage}\n"
            "\\renewcommand{\\headrulewidth}{0.4pt}\n",
            encoding="utf-8",
        )

        cmd = [
            "pandoc",
            str(md_path),
            "-o", str(pdf_path),
            "--from", "markdown+tex_math_dollars+fenced_code_blocks+backtick_code_blocks",
            "--pdf-engine=xelatex",
            "-V", "geometry:margin=2cm",
            "-V", f"mainfont={mainfont}",
            "-V", f"monofont={monofont}",
            "-M", f"title={display_title}",
            "-M", f"subtitle={raw_course} — Slide: {slides_label}",
            "-M", f"date={lesson_date}",
            "--include-in-header", str(header_tex),
            "--highlight-style=tango",
        ]

        logger.info(f"Compilazione PDF con Pandoc/XeLaTeX -> {pdf_path}...")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"Errore compilazione Pandoc/XeLaTeX: {proc.stderr.strip() or proc.stdout.strip()}\n"
                f"Markdown preservato in: {md_path}"
            )

        return pdf_path

    def _sync_course_slides_if_requested(self, course_hint: Optional[str]):
        """Syncs only missing slide PDFs for a matching course from PoliTO portal before slide matching."""
        if not course_hint:
            return
        try:
            from config import COURSE_ALIASES
            from sync_slides import filter_missing_portal_pdfs

            enrolled = self.client.get_enrolled_courses()
            kw = course_hint.lower().strip()
            mapped = COURSE_ALIASES.get(kw, kw)
            for c in enrolled:
                c_name = c["name"].lower()
                if kw in c_name or mapped in c_name:
                    materials = self.client.crawl_course_materials(c)
                    missing_slides = filter_missing_portal_pdfs(self.organizer, c["name"], materials)
                    if missing_slides:
                        logger.info(
                            f"Download di {len(missing_slides)} nuovi PDF di slide mancanti per {c['name']}..."
                        )
                        self.client.download_materials_batch(missing_slides)
                    else:
                        logger.info(f"Tutte le slide di {c['name']} sono gia' presenti in locale.")
        except Exception as e:
            logger.warning(f"Impossibile sincronizzare le slide dal portale (uso i file locali): {e}")

    def _save_course_context(
        self,
        course_name: str,
        lesson_title: str,
        lesson_date: str,
        transcript_text: str,
        slide_match: Optional[SlideMatch],
        source_ref: str = "",
    ) -> Path:
        """
        Saves lecture context (slides + audio transcript) inside:
        ~/Desktop/PoliTO/<Corso>/context/<AAAA-MM-GG>_<Argomento>.md
        """
        clean_course = self.organizer.sanitize_name(course_name)
        context_dir = self.organizer.base_dir / clean_course / "context"
        context_dir.mkdir(parents=True, exist_ok=True)

        stem = format_lesson_filename_stem(lesson_title, lesson_date)
        context_path = context_dir / f"{stem}.md"
        counter = 2
        while context_path.exists():
            context_path = context_dir / f"{stem}_{counter}.md"
            counter += 1

        slides_section = (
            f"### Slide: {slide_match.filename}\n\n{slide_match.extracted_text}\n\n"
            if slide_match
            else "### Slide: Nessuna (solo audio)\n\n"
        )
        compact_transcript = compact_transcript_for_context(transcript_text)
        display_heading = stem.replace("_", " ")
        context_path.write_text(
            f"# {display_heading}\n"
            f"Corso: {clean_course} | Data: {lesson_date}\n\n"
            f"## Istruzioni\n{COMPACT_CONTEXT_INSTRUCTIONS}\n\n"
            f"{slides_section}"
            f"## Trascrizione\n{compact_transcript}\n",
            encoding="utf-8",
        )
        logger.info(f"Contesto salvato in: {context_path}")
        return context_path

    def cleanup_completed_audio_in_temp(self, extra_stems: Optional[List[str]] = None) -> List[Path]:
        """
        Checks temp/ for audio files whose corresponding PDF already exists in
        appunti_pdf/ or ~/Desktop/PoliTO/<Corso>/Appunti_Lezioni/, and deletes them.
        Never touches files outside self.temp_dir.
        """
        audio_exts = {".mp3", ".m4a", ".wav", ".webm", ".ogg"}
        pdf_stems = set(extra_stems or [])

        if self.pdf_output_dir.exists():
            for p in self.pdf_output_dir.glob("*.pdf"):
                pdf_stems.add(p.stem)
        if self.organizer.base_dir.exists():
            for p in self.organizer.base_dir.glob("*/Appunti_Lezioni/*.pdf"):
                pdf_stems.add(p.stem)

        normalized_pdf_stems = set(pdf_stems)
        for s in list(pdf_stems):
            m = re.match(r"^(\d{4}-\d{2}-\d{2})_(.+)$", s)
            if m:
                normalized_pdf_stems.add(format_lesson_filename_stem(m.group(2), m.group(1)))

        removed: List[Path] = []
        if not self.temp_dir.exists():
            return removed

        for item in self.temp_dir.iterdir():
            if not item.is_file() or item.suffix.lower() not in audio_exts:
                continue
            item_norm = format_lesson_filename_stem(item.stem, "")
            if item.stem in normalized_pdf_stems or item_norm in normalized_pdf_stems:
                try:
                    item.unlink()
                    removed.append(item)
                    logger.info(f"Audio temporaneo eliminato dopo creazione PDF: {item.name}")
                except Exception as e:
                    logger.warning(f"Impossibile eliminare audio temporaneo {item}: {e}")
        return removed

    def run_recap(
        self,
        lesson_url: Optional[str] = None,
        audio_path: Optional[Path] = None,
        transcript_path: Optional[Path] = None,
        markdown_path: Optional[Path] = None,
        course_hint: Optional[str] = None,
        lesson_selector: Optional[str] = None,
        preloaded_course: Optional[Dict[str, Any]] = None,
        preloaded_video: Optional[Dict[str, Any]] = None,
        explicit_slides: Optional[Path] = None,
        sync_slides: bool = True,
        keep_temp: bool = False,
        prepare_only: bool = False,
    ) -> Dict[str, Any]:
        """
        End-to-end recap pipeline:
        1. Acquire audio (from URL or local audio), read existing transcript, or compile from Markdown
        2. Transcribe locally with faster-whisper
        3. Automatically recognize matching PDF slides & course (or fall back to audio-only)
        4. Generate Markdown+LaTeX notes via LLM (or save context in <Corso>/context/Video_Lezione_<N>.md)
        5. Compile PDF via Pandoc/XeLaTeX and organize into appunti_pdf + Desktop/PoliTO
        6. Clean up temp files unless keep_temp=True
        """
        created_temp_files: List[Path] = []
        lesson_title = "Lezione_PoliTO"
        lesson_date = datetime.now().strftime("%Y-%m-%d")
        detected_course = course_hint or ""
        page_context = ""

        if markdown_path is not None:
            md_file = Path(markdown_path).expanduser().resolve()
            if not md_file.exists():
                raise FileNotFoundError(f"File Markdown non trovato: {md_file}")
            prefix = md_file.stem
            if re.match(r"^\d{4}-\d{2}-\d{2}_", md_file.stem):
                lesson_date = md_file.stem[:10]
                lesson_title = md_file.stem[11:]
            else:
                lesson_title = md_file.stem
            if not detected_course and md_file.parent.name.lower() == "context":
                detected_course = md_file.parent.parent.name
            slide_match = None
            if explicit_slides is not None:
                slide_match = self.slide_matcher.match_slides("", lesson_title, detected_course or None, explicit_slides)
            effective_course = self.slide_matcher.infer_course(
                transcript_text="",
                lesson_title=lesson_title,
                course_hint=detected_course or None,
                slide_match=slide_match,
            )
            pdf_path = self.pdf_output_dir / f"{prefix}.pdf"
            header_tex = self.temp_dir / f"{pdf_path.stem}_header.tex"
            created_temp_files.append(header_tex)
            self.compile_pdf(
                md_path=md_file,
                pdf_path=pdf_path,
                course_name=effective_course,
                lesson_title=lesson_title,
                lesson_date=lesson_date,
                slide_match=slide_match,
            )
            desktop_pdf_path = self.organizer.save_file(
                course_name=effective_course,
                filename=pdf_path.name,
                content=pdf_path.read_bytes(),
                source_url=str(md_file),
                remote_path="Appunti_Lezioni",
                custom_category="Appunti_Lezioni",
            )
            if not keep_temp:
                if header_tex.exists():
                    header_tex.unlink()
                self.cleanup_completed_audio_in_temp(extra_stems=[pdf_path.stem])
            return {
                "success": True,
                "prepared_only": False,
                "pdf_path": str(pdf_path),
                "desktop_pdf_path": str(desktop_pdf_path) if desktop_pdf_path else None,
                "course": effective_course,
                "lesson_title": lesson_title,
                "lesson_date": lesson_date,
                "slides_used": slide_match.filename if slide_match else None,
                "slides_path": str(slide_match.pdf_path) if slide_match else None,
                "slides_reason": slide_match.reason if slide_match else "Compilato direttamente da Markdown.",
                "kept_temp": keep_temp,
            }

        if lesson_url:
            dl = self.download_audio_from_url(lesson_url)
            audio_path = dl["audio_path"]
            lesson_title = dl["title"]
            lesson_date = dl["date"]
            detected_course = detected_course or dl.get("course_hint", "")
            page_context = dl.get("page_context", "")
            prefix = dl["prefix"]
            created_temp_files.append(audio_path)
            transcript_file = self.temp_dir / f"{prefix}.txt"
            ctx_hint = f"Politecnico di Torino, Ingegneria Informatica. {detected_course}. {lesson_title}".strip()
            lang = infer_course_language(detected_course, lesson_title)
            transcript_text = self.transcribe_audio(audio_path, transcript_file, context_prompt=ctx_hint, language=lang)
            created_temp_files.append(transcript_file)

        elif (lesson_selector and detected_course) or (preloaded_course and preloaded_video):
            dl = self.download_audio_from_course_lesson(
                detected_course or (preloaded_course or {}).get("name", ""),
                lesson_selector or str((preloaded_video or {}).get("index", "")),
                course_info=preloaded_course,
                video_info=preloaded_video,
            )
            audio_path = dl["audio_path"]
            lesson_title = dl["title"]
            lesson_date = dl["date"]
            detected_course = dl.get("course_hint", "") or detected_course
            page_context = dl.get("page_context", "")
            prefix = dl["prefix"]
            created_temp_files.append(audio_path)
            transcript_file = self.temp_dir / f"{prefix}.txt"
            ctx_hint = f"Politecnico di Torino, Ingegneria Informatica. {detected_course}. {lesson_title}".strip()
            lang = infer_course_language(detected_course, lesson_title)
            transcript_text = self.transcribe_audio(audio_path, transcript_file, context_prompt=ctx_hint, language=lang)
            created_temp_files.append(transcript_file)

        elif audio_path is not None:
            audio_file = Path(audio_path).expanduser().resolve()
            if not audio_file.exists():
                raise FileNotFoundError(f"File audio non trovato: {audio_file}")
            lesson_title = audio_file.stem
            if re.match(r"^\d{4}-\d{2}-\d{2}_", audio_file.stem):
                lesson_date = audio_file.stem[:10]
                lesson_title = audio_file.stem[11:]
            prefix = format_lesson_filename_stem(lesson_title, lesson_date)
            transcript_file = self.temp_dir / f"{prefix}.txt"
            ctx_hint = f"Politecnico di Torino, Ingegneria Informatica. {detected_course}. {lesson_title}".strip()
            lang = infer_course_language(detected_course, lesson_title)
            transcript_text = self.transcribe_audio(audio_file, transcript_file, context_prompt=ctx_hint, language=lang)
            created_temp_files.append(transcript_file)

        elif transcript_path is not None:
            t_file = Path(transcript_path).expanduser().resolve()
            if not t_file.exists():
                raise FileNotFoundError(f"File trascrizione non trovato: {t_file}")
            transcript_text = t_file.read_text(encoding="utf-8")
            lesson_title = t_file.stem
            prefix = f"{lesson_date}_{sanitize_filename(lesson_title)}"
            if re.match(r"^\d{4}-\d{2}-\d{2}_", t_file.stem):
                prefix = t_file.stem
                lesson_date = t_file.stem[:10]
                lesson_title = t_file.stem[11:]

        else:
            raise ValueError("Specifica almeno una sorgente: --url, --audio, --transcript oppure --from-md.")

        # Quick course pre-detection from title / page_context only (not spoken transcript body)
        quick_course_hint = detected_course or None
        if not quick_course_hint:
            meta_blob = f"{lesson_title}\n{page_context}".lower()
            for d in self.slide_matcher.resolve_course_dirs(None):
                if d.name.lower() in meta_blob:
                    quick_course_hint = d.name
                    break
            if not quick_course_hint and page_context.strip():
                from config import COURSE_ALIASES
                for alias, target in COURSE_ALIASES.items():
                    if re.search(rf"\b{re.escape(alias)}\b", meta_blob):
                        resolved = self.slide_matcher.resolve_course_dirs(target)
                        if resolved:
                            quick_course_hint = resolved[0].name
                            break

        if sync_slides and not explicit_slides and quick_course_hint:
            self._sync_course_slides_if_requested(quick_course_hint)

        # Recognize matching slide PDF from Desktop/PoliTO (across all courses if still unknown)
        llm_chooser = self.llm.choose_matching_slide if self.llm.is_configured else None
        slide_match = self.slide_matcher.match_slides(
            transcript_text=transcript_text,
            lesson_title=lesson_title,
            course_hint=quick_course_hint,
            explicit_slides=explicit_slides,
            llm_chooser=llm_chooser,
        )

        effective_course = self.slide_matcher.infer_course(
            transcript_text=transcript_text,
            lesson_title=lesson_title,
            course_hint=quick_course_hint,
            page_context=page_context,
            slide_match=slide_match,
        )

        # If --prepare-only or no LLM API key is configured, save context inside
        # ~/Desktop/PoliTO/<Corso>/context/Video_Lezione_<N>.md
        if prepare_only or not self.llm.is_configured:
            context_path = self._save_course_context(
                course_name=effective_course,
                lesson_title=lesson_title,
                lesson_date=lesson_date,
                transcript_text=transcript_text,
                slide_match=slide_match,
                source_ref=lesson_url or str(audio_path or transcript_path or ""),
            )
            if (lesson_url or lesson_selector) and not keep_temp:
                for tf in created_temp_files:
                    if tf.exists():
                        try:
                            tf.unlink()
                        except Exception:
                            pass
            return {
                "success": True,
                "prepared_only": True,
                "context_path": str(context_path),
                "course": effective_course,
                "lesson_title": lesson_title,
                "lesson_date": lesson_date,
                "slides_used": slide_match.filename if slide_match else None,
                "slides_path": str(slide_match.pdf_path) if slide_match else None,
                "slides_reason": slide_match.reason if slide_match else "Nessuna slide corrispondente trovata (solo audio).",
                "kept_temp": keep_temp,
            }

        md_path = self.temp_dir / f"{prefix}.md"
        created_temp_files.append(md_path)

        self.synthesize_notes_markdown(
            transcript_text=transcript_text,
            lesson_title=lesson_title,
            lesson_date=lesson_date,
            course_name=effective_course,
            slide_match=slide_match,
            md_path=md_path,
        )

        # Compile PDF into appunti_pdf/<prefix>.pdf
        pdf_path = self.pdf_output_dir / f"{prefix}.pdf"
        header_tex = self.temp_dir / f"{pdf_path.stem}_header.tex"
        created_temp_files.append(header_tex)

        self.compile_pdf(
            md_path=md_path,
            pdf_path=pdf_path,
            course_name=effective_course,
            lesson_title=lesson_title,
            lesson_date=lesson_date,
            slide_match=slide_match,
        )

        # Also organize a copy in ~/Desktop/PoliTO/<Corso>/Appunti_Lezioni/
        desktop_pdf_path = None
        if effective_course:
            desktop_pdf_path = self.organizer.save_file(
                course_name=effective_course,
                filename=pdf_path.name,
                content=pdf_path.read_bytes(),
                source_url=lesson_url or str(audio_path or transcript_path or ""),
                remote_path="Appunti_Lezioni",
                custom_category="Appunti_Lezioni",
            )

        # Cleanup temp files after PDF succeeded (unless --keep-temp)
        if not keep_temp:
            for tf in created_temp_files:
                if tf.exists():
                    try:
                        tf.unlink()
                    except Exception as e:
                        logger.warning(f"Impossibile rimuovere file temporaneo {tf}: {e}")
            self.cleanup_completed_audio_in_temp(extra_stems=[pdf_path.stem])

        return {
            "success": True,
            "pdf_path": str(pdf_path),
            "desktop_pdf_path": str(desktop_pdf_path) if desktop_pdf_path else None,
            "course": effective_course,
            "lesson_title": lesson_title,
            "lesson_date": lesson_date,
            "slides_used": slide_match.filename if slide_match else None,
            "slides_path": str(slide_match.pdf_path) if slide_match else None,
            "slides_reason": slide_match.reason if slide_match else "Nessuna slide corrispondente trovata: recap basato solo sull'audio.",
            "kept_temp": keep_temp,
        }
