"""
File Organizer module for PoliTO Agent.
Manages local organization of downloaded files inside ~/Desktop/PoliTO/.
"""

import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

from config import POLITO_OUTPUT_DIR, INDEX_FILE


class FileOrganizer:
    def __init__(self, base_dir: Path = POLITO_OUTPUT_DIR, index_file: Path = INDEX_FILE):
        self.base_dir = Path(base_dir)
        self.index_file = Path(index_file)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.index = self._load_index()

    def _load_index(self) -> Dict[str, Any]:
        """Loads or initializes the download catalog."""
        if self.index_file.exists():
            try:
                with open(self.index_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"files": {}, "courses": {}}

    def _save_index(self):
        """Persists the download catalog."""
        self.index_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.index_file, "w", encoding="utf-8") as f:
            json.dump(self.index, f, indent=2, ensure_ascii=False)

    def sanitize_name(self, name: str) -> str:
        """Removes or replaces invalid characters for directory and file names."""
        clean = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
        clean = re.sub(r"\s+", " ", clean)
        return clean or "Senza_Titolo"

    def categorize(self, name: str, folder_path: str = "") -> str:
        """
        Determines the subfolder category based on file or folder name:
        - Lezioni (slides, lezioni, dispense, teoria)
        - Esercitazioni (esercitazioni, esercizi, homework)
        - Laboratori (laboratori, lab, progetti)
        - Esami (temi esame, compiti, test, prove)
        - Altro (default)
        """
        combined = f"{folder_path} {name}".lower()

        if re.search(r"\b(esam[ei]|compit[io]|appell[io]|tema|temi|test)\b", combined):
            return "Temi_Esame"
        if re.search(r"\b(lab|laboratori[io]|progett[io]|project)\b", combined):
            return "Laboratori"
        if re.search(r"\b(eserciz[io]|esercitazion[ei]|homework|task|scheda)\b", combined):
            return "Esercitazioni"
        if re.search(r"\b(lezion[ei]|slide[s]?|lucid[io]|dispens[ae]|teoria|lecture[s]?|unit[aà]?|capitol[io])\b", combined):
            return "Lezioni"
        return "Materiale_Generale"

    def get_target_path(self, course_name: str, category: str, filename: str) -> Path:
        """Computes the full target destination path for a file."""
        clean_course = self.sanitize_name(course_name)
        clean_category = self.sanitize_name(category)
        clean_file = self.sanitize_name(filename)

        course_dir = self.base_dir / clean_course / clean_category
        course_dir.mkdir(parents=True, exist_ok=True)
        return course_dir / clean_file

    def save_file(
        self,
        course_name: str,
        filename: str,
        content: bytes,
        source_url: str = "",
        remote_path: str = "",
        custom_category: Optional[str] = None,
    ) -> Path:
        """
        Saves file to Desktop/PoliTO/<Course>/<Category>/<filename>
        and updates the catalog with checksum and metadata.
        """
        category = custom_category or self.categorize(filename, remote_path)
        dest_path = self.get_target_path(course_name, category, filename)

        # Compute hash
        sha256 = hashlib.sha256(content).hexdigest()

        # Check if already in index and identical
        file_key = f"{course_name}::{remote_path}/{filename}"
        if file_key in self.index["files"]:
            existing_info = self.index["files"][file_key]
            if existing_info.get("sha256") == sha256 and dest_path.exists():
                return dest_path

        # If file with same name exists but different hash, avoid overwriting
        if dest_path.exists():
            existing_hash = hashlib.sha256(dest_path.read_bytes()).hexdigest()
            if existing_hash != sha256:
                stem = dest_path.stem
                suffix = dest_path.suffix
                dest_path = dest_path.with_name(f"{stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{suffix}")

        # Write file
        dest_path.write_bytes(content)

        # Record in index
        self.index["files"][file_key] = {
            "course": course_name,
            "filename": filename,
            "category": category,
            "local_path": str(dest_path),
            "size_bytes": len(content),
            "sha256": sha256,
            "downloaded_at": datetime.now().isoformat(),
            "source_url": source_url,
            "remote_path": remote_path,
        }

        # Track course
        if course_name not in self.index["courses"]:
            self.index["courses"][course_name] = {
                "name": course_name,
                "first_seen": datetime.now().isoformat(),
                "last_synced": datetime.now().isoformat(),
            }
        else:
            self.index["courses"][course_name]["last_synced"] = datetime.now().isoformat()

        self._save_index()
        return dest_path

    def list_local_files(self, course_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns catalog entries, optionally filtered by course."""
        results = []
        for key, item in self.index["files"].items():
            if course_name:
                if course_name.lower() in item["course"].lower():
                    results.append(item)
            else:
                results.append(item)
        return results

    def list_courses(self) -> List[str]:
        """Lists all courses currently present on the desktop."""
        if not self.base_dir.exists():
            return []
        return [
            d.name
            for d in self.base_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".") and d.name != "data"
        ]

    def get_summary(self) -> Dict[str, Any]:
        """Returns statistics on managed files."""
        courses = self.list_courses()
        total_files = len(self.index.get("files", {}))
        total_bytes = sum(f.get("size_bytes", 0) for f in self.index.get("files", {}).values())
        return {
            "base_dir": str(self.base_dir),
            "courses_count": len(courses),
            "courses": courses,
            "total_files": total_files,
            "total_size_mb": round(total_bytes / (1024 * 1024), 2),
        }
