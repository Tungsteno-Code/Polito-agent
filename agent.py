"""
AI Agent for PoliTO materials management and lecture recap generation.
Interprets natural language commands, orchestrates PolitoClient, FileOrganizer,
and LectureProcessor, and returns structured responses.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import COURSE_ALIASES
from file_organizer import FileOrganizer
from lecture_processor import LLMHelper, LectureProcessor
from polito_client import PolitoClient

logger = logging.getLogger("polito_agent")


class PolitoAgent:
    def __init__(
        self,
        client: Optional[PolitoClient] = None,
        organizer: Optional[FileOrganizer] = None,
        llm: Optional[LLMHelper] = None,
    ):
        self.organizer = organizer or FileOrganizer()
        self.client = client or PolitoClient()
        self.llm = llm or LLMHelper()

    def parse_intent(self, user_prompt: str) -> Dict[str, Any]:
        """
        Interprets the user's natural language request.
        Uses configured LLM (OpenAI-compatible or Gemini) if available,
        otherwise employs heuristic NLP.
        """
        url_match = re.search(r"https?://\S+", user_prompt)
        extracted_url = url_match.group(0).rstrip(".,;)") if url_match else None

        audio_match = re.search(r"([^\s\"']+\.(?:mp3|mp4|m4a|wav|webm|ogg))", user_prompt, re.I)
        extracted_audio = audio_match.group(1) if audio_match else None

        if self.llm.is_configured:
            try:
                system_prompt = (
                    "Sei il motore di comprensione intenti per l'agente PoliTO. "
                    "Rispondi SOLO con JSON valido, senza testo o markdown aggiuntivo."
                )
                prompt = f"""
Dato questo comando dell'utente: "{user_prompt}"

Estrai un oggetto JSON con questi campi:
- action: una tra ["sync", "recap", "list_courses", "list_local"]
  Usa "recap" se l'utente chiede recap, riassunto, sbobinatura, trascrizione o appunti PDF di una lezione/audio/video.
- course_keyword: il nome o parola chiave dell'insegnamento menzionato (o null se generale)
- category: una tra ["lezioni", "esercitazioni", "laboratori", "esami", "tutto"]
- specific_keywords: parole chiave specifiche (es. "lezione 3", "lab 2", "signals") o null
- url: eventuale URL https://... presente nel testo, o null
- audio_path: eventuale percorso locale a file audio/video, o null
"""
                text = self.llm.generate(system_prompt, prompt, temperature=0.0)
                if "```json" in text:
                    text = text.split("```json")[1].split("```")[0].strip()
                elif "```" in text:
                    text = text.split("```")[1].split("```")[0].strip()
                parsed = json.loads(text)
                if extracted_url and not parsed.get("url"):
                    parsed["url"] = extracted_url
                if extracted_audio and not parsed.get("audio_path"):
                    parsed["audio_path"] = extracted_audio
                return parsed
            except Exception as e:
                logger.warning(f"LLM intent parsing failed: {e}. Falling back to heuristics.")

        # Fallback Heuristic NLP
        lower = user_prompt.lower()
        intent: Dict[str, Any] = {
            "action": "sync",
            "course_keyword": None,
            "category": "tutto",
            "specific_keywords": None,
            "url": extracted_url,
            "audio_path": extracted_audio,
        }

        if any(w in lower for w in ["mostra corsi", "elenca corsi", "quali corsi", "lista corsi"]):
            intent["action"] = "list_courses"
            return intent

        if any(w in lower for w in ["cosa ho", "file locali", "mostra cartella", "stato desktop", "file sul desktop"]):
            intent["action"] = "list_local"
            return intent

        if (
            extracted_url
            or extracted_audio
            or re.search(r"\b(recap|sbobina|sbobinatura|riassunto|riassumi|appunti|trascrivi|videolezion[ei])\b", lower)
        ):
            intent["action"] = "recap"

        if re.search(r"\b(slide[s]?|lezion[ei]|dispens[ae]|lucid[io]|teoria|lecture[s]?)\b", lower):
            intent["category"] = "lezioni"
        elif re.search(r"\b(lab|laboratori[io]|progett[io]|project)\b", lower):
            intent["category"] = "laboratori"
        elif re.search(r"\b(eserciz[io]|esercitazion[ei]|homework|scheda|task)\b", lower):
            intent["category"] = "esercitazioni"
        elif re.search(r"\b(esam[ei]|compit[io]|appell[io]|tema|temi)\b", lower):
            intent["category"] = "esami"

        # Remove URL/audio before extracting course name
        cleaned_prompt = re.sub(r"https?://\S+", "", user_prompt)
        words = cleaned_prompt.split()
        markers = ["di", "del", "corso", "materia", "insegnamento"]
        for idx, w in enumerate(words):
            if w.lower() in markers and idx + 1 < len(words):
                course_candidate = " ".join(words[idx + 1 : idx + 4])
                clean_candidate = re.sub(r"[,\.;\?!]", "", course_candidate).strip()
                if clean_candidate:
                    intent["course_keyword"] = clean_candidate
                    break

        return intent

    def execute(self, user_prompt: str) -> Dict[str, Any]:
        """Executes the user prompt end-to-end."""
        intent = self.parse_intent(user_prompt)
        logger.info(f"Interpreted Intent: {intent}")

        action = intent.get("action", "sync")
        course_kw = intent.get("course_keyword")
        category = intent.get("category", "tutto")

        if action == "list_local":
            local_summary = self.organizer.get_summary()
            files = self.organizer.list_local_files(course_kw)
            return {
                "success": True,
                "action": "list_local",
                "summary": local_summary,
                "files_count": len(files),
                "files": files[:20],
                "message": f"Trovati {len(files)} file salvati sul Desktop in {local_summary['courses_count']} corsi.",
            }

        if action == "list_courses":
            courses = self.client.get_enrolled_courses()
            return {
                "success": True,
                "action": "list_courses",
                "courses": courses,
                "message": f"Trovati {len(courses)} corsi iscritti nel Portale della Didattica.",
            }

        if action == "recap":
            lesson_url = intent.get("url")
            audio_path_str = intent.get("audio_path")
            if not lesson_url and not audio_path_str:
                return {
                    "success": False,
                    "action": "recap",
                    "message": (
                        "Per generare il recap PDF della lezione indicami l'URL della videolezione "
                        "oppure il percorso del file audio.\n"
                        "Esempi:\n"
                        "  polito recap --url \"https://didattica.polito.it/...\"\n"
                        "  polito recap --audio ~/Downloads/lezione.mp3 --course \"Sistemi Operativi\"\n"
                        "  polito ask \"fammi il recap della lezione https://... di sistemi operativi\""
                    ),
                }

            processor = LectureProcessor(client=self.client, organizer=self.organizer, llm=self.llm)
            recap_res = processor.run_recap(
                lesson_url=lesson_url,
                audio_path=Path(audio_path_str) if audio_path_str else None,
                course_hint=course_kw,
                sync_slides=bool(course_kw),
            )
            slides_info = (
                f"Slide usate: {recap_res['slides_used']}"
                if recap_res.get("slides_used")
                else "Nessuna slide corrispondente trovata (recap basato solo sull'audio)"
            )
            if recap_res.get("prepared_only"):
                msg = (
                    f"Contesto lezione preparato (nessuna API key configurata)! {slides_info}. "
                    f"File pronto in: {recap_res['context_path']}"
                )
            else:
                msg = f"Recap PDF generato! {slides_info}. Salvato in: {recap_res['pdf_path']}"
            return {
                "success": True,
                "action": "recap",
                "recap": recap_res,
                "message": msg,
            }

        # Sync / Download action
        enrolled = self.client.get_enrolled_courses()
        target_courses: List[Dict[str, Any]] = []

        if course_kw:
            kw_clean = course_kw.lower().strip()
            mapped_kw = COURSE_ALIASES.get(kw_clean, kw_clean)

            for c in enrolled:
                c_name = c["name"].lower()
                c_code = (c.get("code") or "").lower()

                matched = (
                    kw_clean in c_name
                    or mapped_kw in c_name
                    or (c_code and kw_clean in c_code)
                )

                if not matched and c.get("modules"):
                    for m in c["modules"]:
                        m_name = m["name"].lower()
                        if kw_clean in m_name or mapped_kw in m_name:
                            matched = True
                            break

                if matched:
                    target_courses.append(c)

        if not target_courses:
            if course_kw:
                return {
                    "success": False,
                    "action": "sync",
                    "message": f"Nessun corso corrispondente a '{course_kw}' trovato tra i tuoi insegnamenti iscritti.",
                    "available_courses": [c["name"] for c in enrolled],
                }
            target_courses = enrolled

        to_download: List[Dict[str, Any]] = []
        for course in target_courses:
            logger.info(f"Processing materials for: {course['name']}...")
            materials = self.client.crawl_course_materials(course)

            for mat in materials:
                cat_guessed = self.organizer.categorize(mat["filename"], mat.get("remote_path", "")).lower()
                if category not in ["tutto", "all"]:
                    if category == "lezioni" and cat_guessed not in ["lezioni", "materiale_generale"]:
                        continue
                    elif category != "lezioni" and category not in cat_guessed:
                        continue
                to_download.append(mat)

        downloaded_files = self.client.download_materials_batch(to_download)

        course_names = ", ".join(c["name"] for c in target_courses)
        if not downloaded_files:
            msg = (
                f"Nessun nuovo file da scaricare per '{course_names}' "
                "(i docenti potrebbero non aver ancora caricato materiali sul portale "
                "oppure i file sono già tutti sincronizzati sul tuo Desktop)."
            )
        else:
            msg = f"Completato! Scaricati e organizzati {len(downloaded_files)} file in {self.organizer.base_dir}."

        return {
            "success": True,
            "action": "sync",
            "target_courses": [c["name"] for c in target_courses],
            "downloaded_count": len(downloaded_files),
            "downloaded_files": downloaded_files,
            "desktop_dir": str(self.organizer.base_dir),
            "message": msg,
        }
