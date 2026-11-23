# 🤖 PoliTO Materials & Lecture Recap Agent

Agente AI per il **Portale della Didattica del Politecnico di Torino** che:
1. **Sincronizza e organizza** automaticamente slide, esercitazioni, laboratori e temi d'esame in `~/Desktop/PoliTO/`.
2. **Scarica solo l'audio** delle videolezioni dal portale (`Playwright` + `yt-dlp`), lo **trascrive localmente** con `onnx-asr` (`Parakeet TDT` + `Silero VAD`), **riconosce automaticamente il PDF delle slide** su cui si basa la lezione (o usa solo l'audio se non ci sono slide corrispondenti) e **genera appunti universitari in PDF** impaginati con LaTeX tramite `pandoc` + `xelatex`.

---

## 🛠️ Setup (Fedora Linux)

1. Clona il repository ed esegui lo script di installazione (installa `ffmpeg`, `pandoc`, `texlive-xetex`, ricrea `.venv`, installa le dipendenze Python e Chromium):
   ```bash
   git clone https://github.com/Tungsteno-Code/Polito-agent.git
   cd Polito-agent
   chmod +x setup.sh
   ./setup.sh
   ```

2. Configura `.env` (partendo da `.env.example` se non esiste già):
   ```env
   POLITO_USERNAME=s123456
   POLITO_PASSWORD=la_tua_password
   POLITO_OUTPUT_DIR=~/Desktop/PoliTO

   # LLM OpenAI-compatibile (es. OpenRouter) oppure usa GEMINI_API_KEY
   API_BASE_URL=https://openrouter.ai/api/v1
   API_KEY=sk-or-...
   MODEL_NAME=google/gemini-2.5-pro
   GEMINI_API_KEY=
   ```

---

## 💻 Utilizzo con `polito`

### Modalità Interattiva (consigliata!)
Lancia semplicemente `polito` senza argomenti:
```bash
polito
```
1. Mostra l'elenco numerato dei tuoi corsi PoliTO.
2. Digita il numero del corso (es. `4`): carica tutte le videolezioni numerate.
3. Digita uno o più numeri separati da spazio (es. `4 5 2`, `12 2` oppure `1-3`): scarica solo l'audio, trascrive, riconosce le slide e salva `<AAAA-MM-GG>_<Argomento>.md` in `~/Desktop/PoliTO/<Corso>/context/` (o compila il PDF se hai configurato una API key).

### 1. Accesso al Portale
```bash
# Login automatico con credenziali in .env (usa --headful per MFA)
polito login
polito login --headful

# Oppure login manuale in browser persistente (senza salvare password in .env)
polito login --manual
```

### 2. Corsi e Materiali Desktop
```bash
polito courses
polito local
polito ask "scarica le slide di sistemi operativi"
```

### 3. Recap Lezione PDF (Riconoscimento Automatico Corso & Slide)
```bash
# Da URL videolezione del Portale della Didattica (--course è opzionale!)
polito recap --url "https://didattica.polito.it/..."

# Oppure in linguaggio naturale
polito ask "fammi il recap della lezione https://didattica.polito.it/..."

# Da file audio/video già presente in locale
polito recap --audio ~/Downloads/lezione7.mp3

# Compila PDF da un file Markdown già generato (rileva il corso anche da context/)
polito recap --from-md ~/Desktop/PoliTO/Operating\ systems/context/Video_Lezione_1_appunti.md

# Opzioni utili:
#   --course CORSO  forza o restringe il corso (opzionale, altrimenti rilevato in automatico)
#   --sync-slides   sincronizza prima i PDF del corso dal portale
#   --slides PATH   forza un PDF specifico invece del riconoscimento automatico
#   --prepare-only  prepara solo il file contesto senza chiamare API esterne
#   --keep-temp     conserva .mp3 e .txt in temp/
```

Come funziona il riconoscimento automatico:
* **Corso:** viene rilevato automaticamente dai parametri URL/pagina, dalla cache dei corsi iscritti, dalle slide corrispondenti oppure dal lessico della trascrizione rispetto ai PDF già presenti in `~/Desktop/PoliTO/`.
* **Slide:** confronta titolo e trascrizione audio con i PDF estratti (con cache locale veloce in `data/pdf_text_cache.json`). Se non c'è una slide corrispondente, usa solo l'audio.
* **Modalità senza API key (`--prepare-only` / fallback automatico):** salva il file pronto in `~/Desktop/PoliTO/<Corso>/context/<AAAA-MM-GG>_<Argomento>.md` (es. `2026-11-17_Build_Management_(Maven)_and_CI.md`).
* **PDF finale:** viene salvato in `appunti_pdf/` e copiato in `~/Desktop/PoliTO/<Corso>/Appunti_Lezioni/`.

---

## 💻 Utilizzo compatibile con `sbobina.py`
È disponibile anche l'entrypoint diretto `sbobina.py`:
```bash
.venv/bin/python sbobina.py --login
.venv/bin/python sbobina.py --url "https://didattica.polito.it/..."
.venv/bin/python sbobina.py --url "https://didattica.polito.it/..." --keep-temp
```

---

## 🔒 Sicurezza e Sola Lettura
* **Read-Only Guard**: blocca `POST`, `PUT`, `DELETE`, `PATCH` verso `portal30` e impedisce la navigazione verso sezioni sensibili (esami, piano carriera, tasse, pagamenti).
* **Cookie Temporanei**: `temp/cookies.txt` viene eliminato subito dopo il download dell'audio.
