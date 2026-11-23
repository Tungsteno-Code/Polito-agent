"""
Slide Matcher module for PoliTO Agent.
Searches downloaded course PDFs in POLITO_OUTPUT_DIR, extracts slide text,
and identifies which slide deck corresponds to a given lecture audio/transcript.
Returns None when no slide deck matches with sufficient confidence.
"""

import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from pypdf import PdfReader

from config import COURSE_ALIASES, DATA_DIR, POLITO_OUTPUT_DIR
from file_organizer import FileOrganizer

logger = logging.getLogger("slide_matcher")
logging.getLogger("pypdf").setLevel(logging.ERROR)

PDF_CACHE_FILE = DATA_DIR / "pdf_text_cache.json"

STOPWORDS = {
    "della", "delle", "dello", "degli", "nell", "nella", "nelle", "nello", "negli",
    "sulla", "sulle", "sullo", "sugli", "dalla", "dalle", "dallo", "dagli", "alla",
    "alle", "allo", "agli", "questo", "questa", "questi", "queste", "quello", "quella",
    "quelli", "quelle", "anche", "come", "cosa", "dove", "quando", "perche", "perché",
    "quindi", "allora", "cioe", "cioè", "sono", "siamo", "avere", "essere", "fare",
    "fatto", "fatta", "faremo", "vediamo", "abbiamo", "possiamo", "dobbiamo", "puo",
    "può", "piu", "più", "molto", "poco", "tutto", "tutti", "tutte", "ogni", "altro",
    "altra", "altri", "altre", "stesso", "stessa", "stessi", "prima", "dopo", "sopra",
    "sotto", "dentro", "fuori", "verso", "circa", "proprio", "sempre", "ancora",
    "inoltre", "oppure", "mentre", "invece", "quindi", "dunque", "ecco", "bene",
    "okay", "ok", "ragazzi", "professore", "lezione", "corso", "esempio", "parte",
    "slide", "pagina", "capitolo", "with", "that", "this", "from", "have", "they",
    "which", "their", "there", "about", "into", "some", "more", "than", "when",
    "what", "where", "your", "will", "would", "could", "should", "then", "also",
}

CATEGORY_WEIGHTS = {
    "lezioni": 1.25,
    "materiale_generale": 1.05,
    "esercitazioni": 0.90,
    "laboratori": 0.85,
    "temi_esame": 0.70,
}


@dataclass
class SlideMatch:
    pdf_path: Path
    filename: str
    course: str
    score: float
    reason: str
    extracted_text: str
    page_count: int


def tokenize(text: str) -> List[str]:
    """Extracts normalized content tokens (words >= 3 chars, excluding stopwords)."""
    raw = re.findall(r"[a-zA-ZÀ-ÿ0-9_]{3,}", text.lower())
    return [t for t in raw if t not in STOPWORDS and not t.isdigit()]


def extract_numbers(text: str) -> List[str]:
    """Extracts lecture/unit numbers like 07, 7, TC03, J06."""
    nums = re.findall(r"(?:\blez(?:ione)?|\blec|\btc|\bj|\bmodulo|\bcap)?[_\s-]*0*(\d{1,2})\b", text.lower())
    return list(dict.fromkeys(nums))


_PDF_CACHE: Optional[Dict[str, Dict[str, object]]] = None


def _load_pdf_cache() -> Dict[str, Dict[str, object]]:
    global _PDF_CACHE
    if _PDF_CACHE is not None:
        return _PDF_CACHE
    if PDF_CACHE_FILE.exists():
        try:
            _PDF_CACHE = json.loads(PDF_CACHE_FILE.read_text(encoding="utf-8"))
            return _PDF_CACHE
        except Exception:
            pass
    _PDF_CACHE = {}
    return _PDF_CACHE


def _save_pdf_cache():
    if _PDF_CACHE is None:
        return
    try:
        PDF_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PDF_CACHE_FILE.write_text(json.dumps(_PDF_CACHE, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def extract_pdf_text(pdf_path: Path, max_pages: int = 120, max_chars: int = 45000) -> Tuple[str, int]:
    """
    Extracts text page-by-page from a PDF using pypdf, with a local mtime/size cache.
    Returns (formatted_text, total_page_count).
    """
    cache = _load_pdf_cache()
    try:
        st = pdf_path.stat()
        cache_key = f"{pdf_path}::{max_pages}::{max_chars}"
        entry = cache.get(cache_key)
        if entry and entry.get("mtime") == st.st_mtime_ns and entry.get("size") == st.st_size:
            return str(entry.get("text", "")), int(entry.get("pages", 0))
    except OSError:
        st = None
        cache_key = ""

    try:
        reader = PdfReader(str(pdf_path))
        total_pages = len(reader.pages)
        chunks = []
        total_len = 0

        for idx, page in enumerate(reader.pages[:max_pages], start=1):
            try:
                txt = (page.extract_text() or "").strip()
            except Exception:
                txt = ""
            if not txt:
                continue
            block = f"--- Slide {idx} ---\n{txt}"
            if total_len + len(block) > max_chars:
                remaining = max_chars - total_len
                if remaining > 200:
                    chunks.append(block[:remaining] + "\n[...troncato...]")
                break
            chunks.append(block)
            total_len += len(block)

        result_text = "\n\n".join(chunks)
        if st is not None and cache_key:
            cache[cache_key] = {
                "mtime": st.st_mtime_ns,
                "size": st.st_size,
                "text": result_text,
                "pages": total_pages,
            }
            _save_pdf_cache()
        return result_text, total_pages
    except Exception as e:
        logger.warning(f"Impossibile leggere il PDF {pdf_path.name}: {e}")
        return "", 0


EXCLUDED_SLIDE_DIRS = {"appunti_lezioni", "appunti_pdf", "appunti", "context"}


class SlideMatcher:
    def __init__(self, organizer: Optional[FileOrganizer] = None):
        self.organizer = organizer or FileOrganizer()

    def resolve_course_dirs(self, course_hint: Optional[str]) -> List[Path]:
        """Resolves matching course directories under POLITO_OUTPUT_DIR."""
        base_dir = self.organizer.base_dir
        if not base_dir.exists():
            return []

        all_dirs = [
            d for d in sorted(base_dir.iterdir())
            if d.is_dir() and not d.name.startswith(".") and d.name != "data"
        ]
        if not course_hint:
            return all_dirs

        kw = course_hint.lower().strip()
        mapped = COURSE_ALIASES.get(kw, kw)
        # 1. Exact or substring match first
        exact = [d for d in all_dirs if kw in d.name.lower() or mapped in d.name.lower()]
        if exact:
            return exact

        # 2. Token overlap fallback (require majority of tokens to match)
        target_tokens = set(tokenize(mapped))
        if not target_tokens:
            return []
        scored_dirs = []
        for d in all_dirs:
            d_tokens = set(tokenize(d.name))
            hits = len(target_tokens & d_tokens)
            if hits > 0 and (hits / len(target_tokens)) >= 0.5:
                scored_dirs.append((hits / len(target_tokens), d))
        scored_dirs.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in scored_dirs]

    def collect_candidate_pdfs(self, course_hint: Optional[str] = None) -> List[Tuple[str, str, Path]]:
        """
        Collects candidate PDFs as (course_name, category_name, pdf_path).
        Skips generated recap folders (Appunti_Lezioni / Appunti_PDF).
        """
        course_dirs = self.resolve_course_dirs(course_hint)
        candidates: List[Tuple[str, str, Path]] = []

        for c_dir in course_dirs:
            for pdf in sorted(c_dir.rglob("*.pdf")):
                rel_parts = pdf.relative_to(c_dir).parts
                category = rel_parts[0] if len(rel_parts) > 1 else "Materiale_Generale"
                if category.lower() in EXCLUDED_SLIDE_DIRS:
                    continue
                candidates.append((c_dir.name, category, pdf))

        return candidates

    def match_slides(
        self,
        transcript_text: str,
        lesson_title: str = "",
        course_hint: Optional[str] = None,
        explicit_slides: Optional[Path] = None,
        llm_chooser: Optional[Callable[[str, str, List[Dict[str, str]]], Optional[str]]] = None,
    ) -> Optional[SlideMatch]:
        """
        Recognizes which downloaded PDF slide deck the lecture is based on.
        Returns None if no PDF exists or none crosses the relevance threshold.
        """
        if explicit_slides is not None:
            slide_path = Path(explicit_slides).expanduser().resolve()
            if not slide_path.exists():
                raise FileNotFoundError(f"File slide non trovato: {slide_path}")
            text, page_count = extract_pdf_text(slide_path)
            course_name = course_hint or slide_path.parent.parent.name
            return SlideMatch(
                pdf_path=slide_path,
                filename=slide_path.name,
                course=course_name,
                score=1.0,
                reason="Slide specificate esplicitamente dall'utente",
                extracted_text=text,
                page_count=page_count,
            )

        candidates = self.collect_candidate_pdfs(course_hint)
        if not candidates and course_hint:
            # Fallback across all courses in case course_hint didn't match folder name
            candidates = self.collect_candidate_pdfs(None)

        if not candidates:
            logger.info("Nessun PDF candidato trovato sul Desktop per questo corso.")
            return None

        transcript_tokens = tokenize(transcript_text)
        transcript_counts = Counter(transcript_tokens)
        top_transcript_terms = {w for w, _ in transcript_counts.most_common(120)}
        title_tokens = set(tokenize(lesson_title))
        title_numbers = set(extract_numbers(lesson_title))

        scored: List[Dict[str, object]] = []
        for course_name, category, pdf_path in candidates:
            # Skip huge textbooks (> 25 MB) during automatic multi-course slide scanning
            try:
                if pdf_path.stat().st_size > 25 * 1024 * 1024:
                    continue
            except OSError:
                continue
            pdf_text, page_count = extract_pdf_text(pdf_path, max_pages=35, max_chars=20000)
            if not pdf_text.strip():
                continue

            pdf_tokens = set(tokenize(pdf_text))
            name_tokens = set(tokenize(pdf_path.stem))
            name_numbers = set(extract_numbers(pdf_path.stem))

            # 1. Transcript-to-slide lexical overlap (weighted by frequency in lecture)
            overlap_terms = top_transcript_terms & pdf_tokens
            weighted_hits = sum(math.log1p(transcript_counts[w]) for w in overlap_terms)
            max_possible = sum(math.log1p(transcript_counts[w]) for w in top_transcript_terms) or 1.0
            content_score = weighted_hits / max_possible

            # 2. Lesson title vs PDF filename/first page overlap
            first_page = pdf_text[:1200]
            first_page_tokens = set(tokenize(first_page))
            title_overlap = 0.0
            if title_tokens:
                hits = len(title_tokens & (name_tokens | first_page_tokens))
                title_overlap = hits / len(title_tokens)

            # 3. Lecture number match (e.g. Lezione 7 <-> 07_Signals.pdf)
            num_bonus = 0.18 if (title_numbers and (title_numbers & name_numbers)) else 0.0

            cat_weight = CATEGORY_WEIGHTS.get(category.lower(), 0.9)
            raw_score = (0.65 * content_score + 0.35 * title_overlap + num_bonus) * cat_weight

            scored.append({
                "course": course_name,
                "category": category,
                "pdf_path": pdf_path,
                "pdf_text": pdf_text,
                "page_count": page_count,
                "score": round(raw_score, 4),
                "overlap_count": len(overlap_terms),
                "first_page": first_page,
            })

        if not scored:
            return None

        scored.sort(key=lambda x: float(x["score"]), reverse=True)
        best = scored[0]
        logger.info(
            f"Miglior candidato slide: {best['pdf_path'].name} "
            f"(score={best['score']}, overlap_terms={best['overlap_count']})"
        )

        # Optional LLM verification when top candidates exist
        if llm_chooser is not None and len(scored) >= 1:
            top_k = [
                {
                    "filename": item["pdf_path"].name,
                    "course": str(item["course"]),
                    "category": str(item["category"]),
                    "score": str(item["score"]),
                    "excerpt": str(item["first_page"])[:600],
                }
                for item in scored[:5]
            ]
            chosen_name = llm_chooser(lesson_title, transcript_text[:3500], top_k)
            if chosen_name == "NONE":
                logger.info("L'LLM ha confermato che nessuna delle slide locali corrisponde alla lezione.")
                return None
            if chosen_name:
                for item in scored[:5]:
                    if item["pdf_path"].name == chosen_name:
                        full_text, page_count = extract_pdf_text(item["pdf_path"])
                        return SlideMatch(
                            pdf_path=item["pdf_path"],
                            filename=item["pdf_path"].name,
                            course=str(item["course"]),
                            score=float(item["score"]),
                            reason=f"Riconoscimento semantico LLM + match lessicale (score {item['score']})",
                            extracted_text=full_text,
                            page_count=page_count,
                        )

        # Strict minimum threshold when using heuristic scoring alone
        if float(best["score"]) < 0.18 or int(best["overlap_count"]) < 6:
            logger.info(
                f"Confidenza insufficiente per associare le slide "
                f"(score={best['score']}, overlap={best['overlap_count']}). Uso solo trascrizione audio."
            )
            return None

        full_text, page_count = extract_pdf_text(best["pdf_path"])
        return SlideMatch(
            pdf_path=best["pdf_path"],
            filename=best["pdf_path"].name,
            course=str(best["course"]),
            score=float(best["score"]),
            reason=f"Match automatico audio-slide ({best['overlap_count']} concetti chiave in comune, score {best['score']})",
            extracted_text=full_text,
            page_count=page_count,
        )

    def infer_course(
        self,
        transcript_text: str,
        lesson_title: str = "",
        course_hint: Optional[str] = None,
        page_context: str = "",
        slide_match: Optional[SlideMatch] = None,
    ) -> str:
        """
        Automatically determines the best matching PoliTO course folder name from:
        1. Explicit course_hint (mapped to existing folder)
        2. Matched slide's course folder
        3. Page/title text vs course names & aliases
        4. Transcript vocabulary vs each course's downloaded PDF corpus
        Falls back to course_hint or 'PoliTO_Appunti' if no course matches.
        """
        if course_hint:
            resolved = self.resolve_course_dirs(course_hint)
            if resolved:
                return resolved[0].name

        if slide_match is not None and slide_match.course:
            return slide_match.course

        all_dirs = self.resolve_course_dirs(None)
        if not all_dirs:
            return course_hint or "PoliTO_Appunti"

        # 3. Check page_context / lesson_title / transcript intro against folder names & aliases
        meta_blob = f"{lesson_title}\n{page_context}\n{transcript_text[:1500]}".lower()
        for d in all_dirs:
            if d.name.lower() in meta_blob:
                return d.name
        for alias, target in COURSE_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", meta_blob):
                resolved = self.resolve_course_dirs(target)
                if resolved:
                    return resolved[0].name

        # 4. Aggregate vocabulary match across each course's local PDFs
        t_counts = Counter(tokenize(transcript_text))
        top_terms = {w for w, _ in t_counts.most_common(120)}
        if not top_terms:
            return course_hint or "PoliTO_Appunti"

        best_course = None
        best_score = 0.0
        best_hits = 0

        for d in all_dirs:
            course_tokens: Counter = Counter()
            course_tokens.update(tokenize(d.name))
            pdfs = [
                p for p in sorted(d.rglob("*.pdf"))
                if p.relative_to(d).parts[0].lower() not in EXCLUDED_SLIDE_DIRS
            ]
            for pdf in pdfs[:12]:
                txt, _ = extract_pdf_text(pdf, max_pages=20, max_chars=8000)
                course_tokens.update(tokenize(txt))

            if not course_tokens:
                continue
            vocab = {w for w, _ in course_tokens.most_common(250)}
            overlap = top_terms & vocab
            score = sum(math.log1p(t_counts[w]) for w in overlap) / (
                sum(math.log1p(t_counts[w]) for w in top_terms) or 1.0
            )
            if score > best_score:
                best_score = score
                best_course = d.name
                best_hits = len(overlap)

        if best_course and best_score >= 0.10 and best_hits >= 5:
            logger.info(
                f"Corso riconosciuto automaticamente dal contenuto: '{best_course}' "
                f"(score={best_score:.3f}, hits={best_hits})"
            )
            return best_course

        return course_hint or "PoliTO_Appunti"

    def match_slide_by_title(self, lesson_title: str, course_hint: str) -> Optional[str]:
        """
        Fast title-only slide lookup for video listing tables.
        Matches distinctive topic words or lecture numbers in lesson_title against
        local PDF filenames and first-page titles for the course.
        """
        generic_title_words = {
            "lezione", "lecture", "shared", "video", "part", "parte", "corso", "course",
            "lab", "guided", "solution", "soluzione", "predefined", "and", "the", "with",
            "for", "from", "del", "della", "dei", "degli", "2024", "2025", "2026", "2027",
        }
        generic_title_words.update(tokenize(course_hint))
        cleaned_title = re.sub(r"\b\d{2}[/-]\d{2}[/-]\d{4}\b", " ", lesson_title)
        title_tokens = {t for t in tokenize(cleaned_title) if t not in generic_title_words}
        is_lab_video = bool(re.search(r"\b(lab\d*|laboratorio)\b", lesson_title, re.I))
        title_nums = set() if is_lab_video else set(extract_numbers(cleaned_title))
        if not title_tokens and not title_nums:
            return None

        candidates = self.collect_candidate_pdfs(course_hint)
        if not candidates:
            return None

        best_name: Optional[str] = None
        best_score = 0.0
        for _course, category, pdf_path in candidates:
            name_tokens = set(tokenize(pdf_path.stem)) - generic_title_words
            # Also split CamelCase in filenames like CourseIntro2026 / JavaInheritance
            camel_parts = re.findall(r"[A-Z][a-z]+", pdf_path.stem)
            name_tokens.update(t.lower() for t in camel_parts if len(t) >= 3 and t.lower() not in generic_title_words)
            name_numbers = set(extract_numbers(pdf_path.stem))

            first_text, _ = extract_pdf_text(pdf_path, max_pages=3, max_chars=1800)
            first_tokens = set(tokenize(first_text)) - generic_title_words

            filename_hits = len(title_tokens & name_tokens)
            first_hits = len(title_tokens & first_tokens)
            num_hit = 1 if (title_nums and (title_nums & name_numbers)) else 0

            # Require at least one filename/title match or strong first-page match
            if filename_hits == 0 and num_hit == 0 and first_hits < 2:
                continue

            score = (
                filename_hits * 2.0
                + first_hits * 0.6
                + num_hit * 1.5
                + (0.3 if category.lower() == "lezioni" else 0.0)
            )
            if score > best_score:
                best_score = score
                best_name = pdf_path.name

        return best_name if best_score >= 1.5 else None
