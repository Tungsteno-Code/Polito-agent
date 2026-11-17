"""
PoliTO Portal Automation Client.
Handles IdP authentication, session persistence, course discovery,
material downloading, and lecture media stream interception.
"""

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from config import (
    CHROME_PROFILE_DIR,
    DATA_DIR,
    POLITO_DIDATTICA_URL,
    POLITO_IDP_LOGIN_URL,
    POLITO_PASSWORD,
    POLITO_USERNAME,
    SESSION_FILE,
)
from file_organizer import FileOrganizer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("polito_client")


class PolitoClient:
    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        session_file: Path = SESSION_FILE,
        chrome_profile_dir: Path = CHROME_PROFILE_DIR,
        headless: bool = True,
    ):
        self.username = username or POLITO_USERNAME
        self.password = password or POLITO_PASSWORD
        self.session_file = Path(session_file)
        self.chrome_profile_dir = Path(chrome_profile_dir)
        self.headless = headless
        self.organizer = FileOrganizer()

    def is_session_valid(self, context: BrowserContext) -> bool:
        """Checks if an existing session is still authenticated."""
        page = context.new_page()
        try:
            page.goto(POLITO_IDP_LOGIN_URL, wait_until="networkidle", timeout=15000)
            if page.query_selector('input[name="j_username"]'):
                return False
            content = page.content().lower()
            return any(k in content for k in ["logout", "esci", "benvenuto", "profilo", "didattica"])
        except Exception as e:
            logger.debug(f"Session check failed or timed out: {e}")
            return False
        finally:
            page.close()

    def login(self, page: Page, force: bool = False) -> bool:
        """
        Executes login on idp.polito.it.
        Supports automatic form submission and handles MFA if required.
        """
        logger.info(f"Navigating to login page: {POLITO_IDP_LOGIN_URL}")
        page.goto(POLITO_IDP_LOGIN_URL, wait_until="networkidle", timeout=30000)

        if not force and page.query_selector('input[name="j_username"]') is None:
            logger.info("Session already active (no login fields present).")
            return True

        if not self.username or not self.password:
            raise ValueError(
                "Credenziali mancanti! Imposta POLITO_USERNAME e POLITO_PASSWORD nel file .env "
                "oppure usa `polito login --manual` (o `python sbobina.py --login`)."
            )

        logger.info(f"Submitting credentials for user: {self.username}...")
        page.fill('input[name="j_username"]', self.username)
        page.fill('input[name="j_password"]', self.password)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle", timeout=20000)

        page_text = page.inner_text("body")
        if "Invalid credentials" in page_text or "Credenziali non corrette" in page_text:
            raise ValueError(
                "Credenziali PoliTO errate! Verifica username e password nel file .env."
            )

        if any(w in page_text.lower() for w in ["mfa", "secondo fattore", "otp", "codice di sicurezza", "notifica push"]):
            logger.warning("Rilevata richiesta di autenticazione a due fattori (MFA)!")
            logger.info("Attesa approvazione MFA (fino a 60 secondi)...")
            try:
                page.wait_for_selector('input[name="j_username"]', state="detached", timeout=60000)
            except Exception:
                pass

        logger.info("Autenticazione completata con successo!")
        return True

    def login_interactive(self) -> Path:
        """
        Opens a persistent non-headless Chromium profile on didattica.polito.it.
        The user completes login (including 2FA) manually and closes the browser window;
        session state is persisted in .chrome_polito_profile and exported to session.json.
        """
        self.chrome_profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.chrome_profile_dir),
                headless=False,
                accept_downloads=True,
            )
            page = context.pages[0] if context.pages else context.new_page()

            def _try_save_storage(*_args):
                try:
                    url = page.url.lower()
                    if "didattica.polito.it" in url and not page.query_selector('input[name="j_username"]'):
                        self.save_session(context)
                except Exception:
                    pass

            page.on("load", _try_save_storage)
            page.goto(f"{POLITO_DIDATTICA_URL}/", wait_until="domcontentloaded", timeout=30000)
            logger.info(
                "Finestra del browser aperta. Completa manualmente il login (inclusa eventuale 2FA) "
                "su didattica.polito.it e poi chiudi la finestra del browser per salvare la sessione."
            )

            try:
                while len(context.pages) > 0:
                    _try_save_storage()
                    time.sleep(0.5)
            except Exception:
                pass
            finally:
                try:
                    self.save_session(context)
                except Exception:
                    pass
                try:
                    context.close()
                except Exception:
                    pass

        return self.chrome_profile_dir

    def get_context(self, p, headless: Optional[bool] = None) -> Tuple[Browser, BrowserContext]:
        """Creates a browser context, restoring stored session state if available."""
        is_headless = self.headless if headless is None else headless
        browser = p.chromium.launch(headless=is_headless)

        if self.session_file.exists():
            try:
                context = browser.new_context(
                    storage_state=str(self.session_file),
                    accept_downloads=True,
                )
                logger.info(f"Loaded existing session state from {self.session_file}")
                return browser, context
            except Exception as e:
                logger.warning(f"Failed to load session state: {e}. Starting fresh context.")

        context = browser.new_context(accept_downloads=True)
        return browser, context

    def save_session(self, context: BrowserContext):
        """Saves session cookies and localStorage to session file."""
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(self.session_file))
        logger.info(f"Saved session state to {self.session_file}")

    def install_safety_guard(self, context: BrowserContext, allow_player_requests: bool = False):
        """
        Enforces a strict READ-ONLY sandbox on the browser session.
        1. Blocks state-modifying requests (POST, PUT, DELETE, PATCH), except IdP SAML
           and optionally harmless video-player requests when capturing streams.
        2. Blocks navigation to sensitive university career/payment sections.
        """
        def route_interceptor(route, request):
            method = request.method.upper()
            url = request.url.lower()

            sensitive_areas = [
                "prenotazione", "iscrizioniesami", "iscrizione_esami", "appelli",
                "verbalizzazione", "verbale", "piano_carriera", "carriera_studente",
                "tasse", "pagopa", "pagamenti", "bollettin", "rinuncia",
                "trasferimento", "modifica_piano",
            ]
            for term in sensitive_areas:
                if term in url:
                    logger.warning(
                        f"[BLOCCO DI SICUREZZA] Navigazione verso sezione sensibile BLOCCATA: {url}"
                    )
                    route.abort("blockedbyclient")
                    return

            if method in ["POST", "PUT", "DELETE", "PATCH"]:
                if "idp.polito.it/idp/profile/saml2" in url:
                    route.continue_()
                    return
                if allow_player_requests and (
                    "sviluppo.virtual_classroom" in url
                    or ("portal30" not in url and "idp.polito.it" not in url)
                ):
                    route.continue_()
                    return
                logger.warning(
                    f"[BLOCCO DI SICUREZZA] Richiesta {method} BLOCCATA verso {url} (modalità Sola Lettura attiva)."
                )
                route.abort("blockedbyclient")
                return

            route.continue_()

        context.route("**/*", route_interceptor)
        logger.info("Sandbox di sicurezza attiva: Modalità Sola Lettura (Read-Only) e Whitelist URL abilitata.")

    def ensure_authenticated_page(self, context: BrowserContext, allow_player_requests: bool = False) -> Page:
        """Returns a page guaranteed to be authenticated and protected by the safety guard."""
        page = context.new_page()
        page.goto(POLITO_IDP_LOGIN_URL, wait_until="networkidle", timeout=25000)

        if page.query_selector('input[name="j_username"]'):
            logger.info("Session expired or not found. Performing login...")
            self.login(page)
            self.save_session(context)

        self.install_safety_guard(context, allow_player_requests=allow_player_requests)
        return page

    def get_enrolled_courses(self) -> List[Dict[str, Any]]:
        """
        Retrieves list of enrolled courses from the PoliTO student API:
        https://didattica.polito.it/pls/portal30/sviluppo.studente.getStudentData
        """
        STUDENT_DATA_URL = "https://didattica.polito.it/pls/portal30/sviluppo.studente.getStudentData"
        with sync_playwright() as p:
            browser, context = self.get_context(p)
            try:
                page = self.ensure_authenticated_page(context)
                logger.info("Recupero piano di studi ed elenco corsi da getStudentData...")
                resp = page.goto(STUDENT_DATA_URL, wait_until="networkidle", timeout=25000)
                if not resp or resp.status != 200:
                    raise RuntimeError(
                        f"Impossibile accedere ai dati studente (HTTP Status: {resp.status if resp else 'N/A'})"
                    )

                data = json.loads(resp.text())
                carico = data.get("data", {}).get("carico", {}).get("data", [])

                courses = []
                for c in carico:
                    course_name = (c.get("name") or "").strip()
                    code = (c.get("shortcode") or "").strip()
                    cfu = c.get("cfu")
                    id_inc = c.get("id")
                    url = c.get("url") or (
                        f"https://didattica.polito.it/pls/portal30/sviluppo.chiama_materia"
                        f"?cod_ins={code}&incarico={id_inc}"
                    )

                    modules = []
                    if c.get("hasModules") and c.get("modules"):
                        for m in c["modules"]:
                            modules.append({
                                "name": (m.get("name") or "").strip(),
                                "code": (m.get("shortcode") or "").strip(),
                                "id_inc": m.get("id"),
                                "teacher": (m.get("teacherName") or "").strip(),
                                "url": m.get("url") or "",
                            })

                    courses.append({
                        "name": course_name,
                        "code": code,
                        "cfu": cfu,
                        "id_inc": id_inc,
                        "url": url,
                        "modules": modules,
                    })

                try:
                    cache_file = DATA_DIR / "enrolled_courses.json"
                    cache_file.write_text(json.dumps(courses, indent=2, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass

                return courses
            finally:
                browser.close()

    def crawl_course_materials(self, course_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Recursively retrieves all files and folders for a course using the PoliTO FileManager API.
        """
        FILEMANAGER_URL = "https://didattica.polito.it/pls/portal30/sviluppo.filemanager.list"
        course_name = course_info.get("name", "")

        targets = []
        if course_info.get("id_inc"):
            targets.append((course_name, course_info["id_inc"]))
        if course_info.get("modules"):
            for m in course_info["modules"]:
                if m.get("id_inc") and m["id_inc"] != course_info.get("id_inc"):
                    targets.append((f"{course_name} - {m['name']}", m["id_inc"]))

        if not targets:
            logger.warning(f"Nessun incarico/id_inc disponibile per il corso {course_name}.")
            return []

        files = []
        with sync_playwright() as p:
            browser, context = self.get_context(p)
            try:
                page = self.ensure_authenticated_page(context)

                for name, id_inc in targets:
                    logger.info(f"Esplorazione file manager per {name} (Incarico: {id_inc})...")

                    def crawl_folder(node_key=None, current_path=""):
                        query = f"p_id_inc={id_inc}"
                        if node_key:
                            query += f"&p_node={node_key}"

                        url = f"{FILEMANAGER_URL}?{query}"
                        resp = page.goto(url, wait_until="networkidle", timeout=20000)
                        if not resp or resp.status != 200:
                            return

                        try:
                            items = json.loads(resp.text())
                        except Exception:
                            return

                        for item in items:
                            item_name = item.get("name", "").strip()
                            if item.get("isDirectory"):
                                sub_key = item.get("key")
                                sub_path = f"{current_path}/{item_name}" if current_path else item_name
                                crawl_folder(sub_key, sub_path)
                            else:
                                dl_url = item.get("url")
                                if dl_url:
                                    files.append({
                                        "course": name,
                                        "title": item_name,
                                        "filename": item.get("filename") or item_name,
                                        "url": dl_url,
                                        "remote_path": current_path,
                                        "size": item.get("size"),
                                        "created": item.get("created"),
                                    })

                    crawl_folder()

                return files
            finally:
                browser.close()

    def _build_requests_session(self, context: BrowserContext) -> requests.Session:
        """Copies cookies from a Playwright context into a requests.Session."""
        s = requests.Session()
        for c in context.cookies():
            s.cookies.set(c["name"], c["value"], domain=c["domain"])
        return s

    def download_materials_batch(self, materials: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Downloads a batch of materials reusing a single authenticated Playwright session
        and a single requests.Session.
        """
        if not materials:
            return []

        downloaded = []
        with sync_playwright() as p:
            browser, context = self.get_context(p)
            try:
                self.ensure_authenticated_page(context)
                session = self._build_requests_session(context)

                for mat in materials:
                    course_name = mat["course"]
                    url = mat["url"]
                    filename = mat["filename"]
                    remote_path = mat.get("remote_path", "")

                    try:
                        logger.info(f"Download di {filename} ({remote_path})...")
                        resp = session.get(url, timeout=60, stream=True)
                        resp.raise_for_status()
                        content = resp.content
                        saved_path = self.organizer.save_file(
                            course_name=course_name,
                            filename=filename,
                            content=content,
                            source_url=url,
                            remote_path=remote_path,
                        )
                        downloaded.append({
                            "course": course_name,
                            "filename": filename,
                            "saved_to": str(saved_path),
                        })
                    except Exception as e:
                        logger.error(f"Errore download {filename}: {e}")
            finally:
                browser.close()

        return downloaded

    def download_and_organize_file(self, file_info: Dict[str, Any]) -> Optional[Path]:
        """Downloads a single file and organizes it in the local Desktop folder."""
        results = self.download_materials_batch([file_info])
        if results:
            return Path(results[0]["saved_to"])
        return None

    @staticmethod
    def export_netscape_cookies(cookies: List[Dict[str, Any]], output_path: Path) -> Path:
        """Writes Playwright cookies to Netscape cookies.txt format for yt-dlp."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Netscape HTTP Cookie File",
            "# Generated by PoliTO Agent",
            "",
        ]
        for c in cookies:
            domain = c.get("domain", "")
            if not domain:
                continue
            include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
            path = c.get("path", "/")
            secure = "TRUE" if c.get("secure") else "FALSE"
            raw_expires = c.get("expires", 0)
            try:
                expires = max(0, int(float(raw_expires)))
            except (TypeError, ValueError):
                expires = 0
            name = c.get("name", "")
            value = c.get("value", "")
            lines.append(f"{domain}\t{include_subdomains}\t{path}\t{secure}\t{expires}\t{name}\t{value}")

        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return output_path

    @staticmethod
    def _extract_lesson_metadata(page: Page, lesson_url: str) -> Dict[str, str]:
        """Extracts lesson title, course name (if visible), and date (YYYY-MM-DD) from page."""
        raw_title = ""
        try:
            raw_title = (page.title() or "").strip()
        except Exception:
            pass

        body_text = ""
        try:
            body_text = page.inner_text("body")
        except Exception:
            pass

        # Try common heading elements for lesson title
        heading_title = ""
        for selector in ["h1", "h2", ".lesson-title", ".video-title", ".titolo-lezione", "title"]:
            try:
                el = page.query_selector(selector)
                if el:
                    txt = (el.inner_text() or "").strip()
                    if txt and len(txt) > 3 and "polito" not in txt.lower():
                        heading_title = txt
                        break
            except Exception:
                continue

        lesson_title = heading_title or raw_title or "Lezione_PoliTO"
        lesson_title = re.sub(r"\s*[-|]\s*Politecnico di Torino.*$", "", lesson_title, flags=re.I).strip()

        # Attempt to find date pattern DD/MM/YYYY or YYYY-MM-DD
        lesson_date = datetime.now().strftime("%Y-%m-%d")
        search_blob = f"{lesson_title}\n{body_text[:4000]}"
        m_it = re.search(r"\b(\d{2})/(\d{2})/(\d{4})\b", search_blob)
        m_iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", search_blob)
        if m_it:
            day, month, year = m_it.groups()
            lesson_date = f"{year}-{month}-{day}"
        elif m_iso:
            lesson_date = f"{m_iso.group(1)}-{m_iso.group(2)}-{m_iso.group(3)}"

        # Attempt to infer course name from URL params, cached enrolled courses, breadcrumbs, or headers
        course_name = ""
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(lesson_url).query)
        url_codes = {v.lower() for k in ("cod_ins", "incarico", "p_id_inc", "id_inc") for v in qs.get(k, [])}

        candidate_texts = [raw_title, body_text[:2000]]
        for sel in [".breadcrumb", "nav[aria-label*='breadcrumb' i]", ".course-title", ".nome-corso", "h1", "h2"]:
            try:
                for el in page.query_selector_all(sel)[:4]:
                    txt = (el.inner_text() or "").strip()
                    if txt:
                        candidate_texts.append(txt)
            except Exception:
                continue

        # Check against cached enrolled courses first
        cache_file = DATA_DIR / "enrolled_courses.json"
        if cache_file.exists():
            try:
                cached_courses = json.loads(cache_file.read_text(encoding="utf-8"))
                joined_candidates = " \n ".join(candidate_texts).lower()
                for c in cached_courses:
                    c_name = (c.get("name") or "").strip()
                    c_code = (c.get("code") or "").strip().lower()
                    c_inc = str(c.get("id_inc") or "").strip().lower()
                    if (c_code and c_code in url_codes) or (c_inc and c_inc in url_codes):
                        course_name = c_name
                        break
                    if c_name and c_name.lower() in joined_candidates:
                        course_name = c_name
                        break
            except Exception:
                pass

        if not course_name:
            for sel in [".course-title", ".nome-corso", ".breadcrumb", "nav[aria-label*='breadcrumb' i]"]:
                try:
                    el = page.query_selector(sel)
                    if el:
                        lines = [ln.strip() for ln in (el.inner_text() or "").splitlines() if ln.strip()]
                        non_generic = [ln for ln in lines if ln.lower() not in {"home", "didattica", "portale della didattica"}]
                        if non_generic:
                            course_name = non_generic[0]
                            break
                except Exception:
                    continue

        return {
            "title": lesson_title or "Lezione_PoliTO",
            "date": lesson_date,
            "course_hint": course_name,
            "page_context": " \n ".join(t for t in candidate_texts if t)[:2500],
            "url": lesson_url,
        }

    def _try_trigger_play(self, page: Page):
        """Attempts to click play buttons on main page and inside iframes if autoplay did not trigger."""
        selectors = [
            "button[aria-label*='play' i]",
            "button[title*='play' i]",
            ".vjs-big-play-button",
            ".plyr__control--overlaid",
            "[data-testid*='play' i]",
            "button.play",
            ".play-button",
            "video",
        ]
        targets = [page] + list(page.frames)
        for frame in targets:
            for sel in selectors:
                try:
                    loc = frame.locator(sel).first
                    if loc.is_visible(timeout=400):
                        loc.click(timeout=1500)
                        logger.info(f"Click su pulsante play eseguito ({sel}).")
                        return True
                except Exception:
                    continue
        return False

    def _find_course_by_id_inc(self, id_inc: str) -> Optional[Dict[str, Any]]:
        """Finds an enrolled course (from cache or portal) matching a given incarico id."""
        id_str = str(id_inc).strip()
        cache_file = DATA_DIR / "enrolled_courses.json"
        courses: List[Dict[str, Any]] = []
        if cache_file.exists():
            try:
                courses = json.loads(cache_file.read_text(encoding="utf-8"))
            except Exception:
                courses = []

        def _match(clist: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            for c in clist:
                if str(c.get("id_inc") or "") == id_str:
                    return c
                for m in c.get("modules", []) or []:
                    if str(m.get("id_inc") or "") == id_str:
                        return {
                            "name": f"{c['name']} - {m['name']}" if m.get("name") else c["name"],
                            "code": m.get("code") or c.get("code"),
                            "id_inc": m.get("id_inc"),
                            "url": m.get("url") or c.get("url"),
                        }
            return None

        found = _match(courses)
        if found:
            return found

        courses = self.get_enrolled_courses()
        return _match(courses)

    def list_course_videos(self, course_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Lists all Virtual Classroom video recordings available on a course page.
        """
        course_url = course_info.get("url")
        course_name = course_info.get("name", "")
        if not course_url:
            return []

        with sync_playwright() as p:
            browser, context = self.get_context(p, headless=True)
            try:
                page = self.ensure_authenticated_page(context, allow_player_requests=True)
                page.goto(course_url, wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_selector("[data-bbb-id]", state="attached", timeout=12000)
                except Exception:
                    return []
                raw_items = page.eval_on_selector_all(
                    "[data-bbb-id]",
                    """els => els.map(e => ({
                        title: (e.innerText || '').trim(),
                        id_inc: e.getAttribute('data-id_inc') || '',
                        id_inc_prov: e.getAttribute('data-id_inc-prov') || '',
                        bbb_id: e.getAttribute('data-bbb-id') || '',
                        vc_date: e.getAttribute('data-vc') || ''
                    }))""",
                )
                videos: List[Dict[str, Any]] = []
                seen = set()
                for item in raw_items:
                    bbb_id = item.get("bbb_id", "").strip()
                    if not bbb_id or bbb_id in seen:
                        continue
                    seen.add(bbb_id)
                    videos.append({
                        "index": len(videos) + 1,
                        "title": item.get("title") or f"Lezione {item.get('vc_date', '')}".strip(),
                        "date": item.get("vc_date") or "",
                        "id_inc": item.get("id_inc") or str(course_info.get("id_inc") or ""),
                        "id_inc_prov": item.get("id_inc_prov") or "0",
                        "bbb_id": bbb_id,
                        "course": course_name,
                        "course_url": course_url,
                    })
                return videos
            finally:
                browser.close()

    def get_fresh_video_stream(
        self,
        id_inc: str,
        bbb_id: str,
        cookies_out_path: Path,
        course_info: Optional[Dict[str, Any]] = None,
        lesson_hint_title: str = "",
        lesson_hint_date: str = "",
    ) -> Dict[str, Any]:
        """
        Opens the course Virtual Classroom page, selects the target recording by bbb_id,
        and extracts a fresh, non-expired stream URL (video.polito.it .mp4 or .m3u8/.mpd).
        """
        course = course_info or self._find_course_by_id_inc(id_inc)
        if not course or not course.get("url"):
            raise RuntimeError(
                f"Impossibile risalire al corso per id_inc={id_inc}. "
                "Esegui `polito courses` oppure passa `--course`."
            )

        course_name = course.get("name", "")
        course_url = course["url"]
        captured_vc_urls: List[str] = []

        with sync_playwright() as p:
            browser, context = self.get_context(p, headless=True)
            try:
                page = self.ensure_authenticated_page(context, allow_player_requests=True)

                def on_resp(resp):
                    if "sviluppo.virtual_classroom.getvctpl" in resp.url.lower():
                        try:
                            txt = resp.text()
                            m = re.search(r'<source[^>]+src=["\']([^"\']+)["\']', txt, re.I)
                            if m:
                                captured_vc_urls.append(m.group(1))
                        except Exception:
                            pass

                page.on("response", on_resp)
                logger.info(f"Recupero link video aggiornato dalla pagina del corso: {course_name}...")
                page.goto(course_url, wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_selector(
                        f'[data-bbb-id="{bbb_id}"], video source',
                        state="attached",
                        timeout=12000,
                    )
                except Exception:
                    pass

                # Extract title/date from the matching [data-bbb-id] element if present
                item_meta = page.eval_on_selector(
                    f'[data-bbb-id="{bbb_id}"]',
                    """e => e ? ({
                        title: (e.innerText || '').trim(),
                        vc_date: e.getAttribute('data-vc') || '',
                        id_inc: e.getAttribute('data-id_inc') || ''
                    }) : null""",
                ) if page.query_selector(f'[data-bbb-id="{bbb_id}"]') else None

                title = (item_meta or {}).get("title") or lesson_hint_title or f"Lezione_{bbb_id}"
                raw_date = (item_meta or {}).get("vc_date") or lesson_hint_date
                lesson_date = datetime.now().strftime("%Y-%m-%d")
                m_it = re.search(r"\b(\d{2})/(\d{2})/(\d{4})\b", f"{raw_date} {title}")
                if m_it:
                    lesson_date = f"{m_it.group(3)}-{m_it.group(2)}-{m_it.group(1)}"

                # Check if already loaded in #videoPlayer_<id_inc> source
                stream_url = None
                existing_src = page.eval_on_selector_all(
                    "video source",
                    "els => els.map(e => e.src).filter(Boolean)",
                )
                for s in existing_src:
                    if bbb_id in s:
                        stream_url = s
                        break

                if not stream_url:
                    before_count = len(captured_vc_urls)
                    btn = page.query_selector(f'[data-bbb-id="{bbb_id}"]')
                    if not btn:
                        raise RuntimeError(
                            f"Registrazione `{bbb_id}` non trovata nella Virtual Classroom di {course_name}."
                        )
                    # If button is inside a hidden Bootstrap tab-pane, activate its tab and/or trigger JS click
                    btn.evaluate(
                        """el => {
                            const pane = el.closest('.tab-pane');
                            if (pane && pane.id) {
                                const tab = document.querySelector(`a[href="#${pane.id}"]`);
                                if (tab) tab.click();
                            }
                            el.click();
                        }"""
                    )
                    for _ in range(20):
                        if len(captured_vc_urls) > before_count:
                            stream_url = captured_vc_urls[-1]
                            break
                        sources_now = page.eval_on_selector_all(
                            "video source",
                            "els => els.map(e => e.src).filter(Boolean)",
                        )
                        match_now = [s for s in sources_now if bbb_id in s]
                        if match_now:
                            stream_url = match_now[-1]
                            break
                        page.wait_for_timeout(400)

                if not stream_url:
                    raise RuntimeError(
                        f"Impossibile ottenere un URL video aggiornato per `{title}` ({bbb_id})."
                    )

                self.export_netscape_cookies(context.cookies(), cookies_out_path)
                return {
                    "stream_url": stream_url,
                    "referer": course_url,
                    "user_agent": "",
                    "cookies_file": cookies_out_path,
                    "title": title,
                    "date": lesson_date,
                    "course_hint": course_name,
                    "page_context": f"{course_name}\n{title}\n{raw_date}",
                    "url": course_url,
                }
            finally:
                browser.close()

    def capture_lecture_stream(self, lesson_url: str, cookies_out_path: Path) -> Dict[str, Any]:
        """
        Opens a PoliTO lecture URL (or refreshes an expired video.polito.it/dl/... link),
        intercepts the stream URL + request headers, extracts lesson metadata,
        and exports Netscape cookies.txt.
        """
        # Handle direct/signed video.polito.it/dl/<token>/<exp>/vc<year>/<id_inc>/<video>.mp4 URLs
        m_vp = re.match(
            r"^https?://video\.polito\.it/dl/[^/]+/(\d+)/vc\d+/(\d+)/([^/?#]+?)(?:\.mp4|\.m3u8)?$",
            lesson_url.strip(),
            re.I,
        )
        if m_vp:
            exp_ts, id_inc, bbb_id = int(m_vp.group(1)), m_vp.group(2), m_vp.group(3)
            need_refresh = exp_ts <= int(time.time()) + 60
            if not need_refresh:
                try:
                    head_resp = requests.head(lesson_url, timeout=10, allow_redirects=True)
                    if head_resp.status_code >= 400:
                        need_refresh = True
                except Exception:
                    need_refresh = True

            if need_refresh:
                logger.info(
                    f"Link video.polito.it scaduto o non valido (exp={exp_ts}); "
                    f"rigenero automaticamente il link fresco dal corso (id_inc={id_inc})..."
                )
                return self.get_fresh_video_stream(id_inc, bbb_id, cookies_out_path)

            course = self._find_course_by_id_inc(id_inc)
            course_name = course["name"] if course else ""
            cookies_out_path.parent.mkdir(parents=True, exist_ok=True)
            cookies_out_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            return {
                "stream_url": lesson_url,
                "referer": (course or {}).get("url") or f"{POLITO_DIDATTICA_URL}/",
                "user_agent": "",
                "cookies_file": cookies_out_path,
                "title": f"Lezione_{bbb_id}",
                "date": datetime.now().strftime("%Y-%m-%d"),
                "course_hint": course_name,
                "page_context": course_name,
                "url": lesson_url,
            }

        captured: Dict[str, Any] = {
            "stream_url": None,
            "fallback_media_url": None,
            "headers": {},
        }

        def on_request(request):
            url = request.url
            lower = url.lower()
            if not captured["stream_url"] and (".m3u8" in lower or ".mpd" in lower):
                captured["stream_url"] = url
                captured["headers"] = dict(request.headers)
                logger.info(f"Manifest stream catturato: {url[:120]}")
            elif not captured["fallback_media_url"] and any(
                ext in lower for ext in [".mp4", ".m4a", ".webm", ".mp3"]
            ):
                captured["fallback_media_url"] = url
                if not captured["headers"]:
                    captured["headers"] = dict(request.headers)

        with sync_playwright() as p:
            use_persistent = (
                self.chrome_profile_dir.exists()
                and not self.session_file.exists()
                and not (self.username and self.password)
            )

            browser = None
            if use_persistent:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(self.chrome_profile_dir),
                    headless=True,
                    accept_downloads=True,
                )
                self.install_safety_guard(context, allow_player_requests=True)
                page = context.pages[0] if context.pages else context.new_page()
            else:
                browser, context = self.get_context(p, headless=True)
                page = self.ensure_authenticated_page(context, allow_player_requests=True)

            try:
                page.on("request", on_request)
                logger.info(f"Apertura URL lezione: {lesson_url}")
                nav_resp = page.goto(lesson_url, wait_until="domcontentloaded", timeout=35000)
                if nav_resp is not None and nav_resp.status >= 400:
                    raise RuntimeError(
                        f"La pagina della lezione ha risposto con HTTP {nav_resp.status} ({nav_resp.status_text}). "
                        "Se è un link temporaneo video.polito.it scaduto, usa `polito videos <corso>` o passa l'URL della pagina del corso."
                    )

                # Wait briefly for autoplay stream requests
                for _ in range(12):
                    if captured["stream_url"]:
                        break
                    page.wait_for_timeout(500)

                # If autoplay didn't fire, click play and wait
                if not captured["stream_url"]:
                    self._try_trigger_play(page)
                    for _ in range(24):
                        if captured["stream_url"]:
                            break
                        page.wait_for_timeout(500)

                stream_url = captured["stream_url"] or captured["fallback_media_url"]
                if not stream_url:
                    raise RuntimeError(
                        "Impossibile intercettare uno stream .m3u8, .mpd o media dalla pagina della lezione. "
                        "Verifica che la sessione sia attiva (`polito login`) e che l'URL punti al player video."
                    )

                metadata = self._extract_lesson_metadata(page, lesson_url)
                cookies = context.cookies()
                self.export_netscape_cookies(cookies, cookies_out_path)

                headers = captured["headers"]
                referer = headers.get("referer") or headers.get("Referer") or lesson_url
                user_agent = headers.get("user-agent") or headers.get("User-Agent") or ""

                return {
                    "stream_url": stream_url,
                    "referer": referer,
                    "user_agent": user_agent,
                    "cookies_file": cookies_out_path,
                    **metadata,
                }
            finally:
                if browser is not None:
                    browser.close()
                else:
                    context.close()
