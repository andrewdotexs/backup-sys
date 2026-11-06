# backup-sys — Piano di sviluppo

> Stato: **APPROVATO** (2026-11-06) — decisioni prese in [§9](#9-decisioni-prese). Fasi 0–7 implementate (v0.1.0).

---

## 1. Panoramica

`backup-sys` è un programma che copia **automaticamente e periodicamente** i file di un PC su uno o più
dischi di backup (USB/esterni o cartelle di rete), con una interfaccia per gestire e controllare i backup
e un modulo di sicurezza.

La regola che guida tutto il progetto:

> **Invariante "solo-aggiunta" (append-only)**: il programma **non cancella mai nulla** nella copia corrente
> (`current/`) dei dischi di backup. Ogni file presente sul PC è sempre presente sul backup.
> L'**unica** eccezione, scelta esplicitamente (D3), è la **rotazione delle vecchie versioni** in `versions/`
> quando lo spazio sul disco supera i limiti configurati — con le regole rigide descritte in §4.5.

Conseguenze dirette dell'invariante:

| Evento sul PC / sul backup                               | Comportamento del programma                                              |
|----------------------------------------------------------|--------------------------------------------------------------------------|
| File nuovo sul PC                                        | Copiato sul backup                                                       |
| File modificato sul PC                                   | Nuova versione copiata; la **vecchia versione viene conservata** (versioning), non sovrascritta |
| File cancellato sul PC                                   | **Resta** sul backup                                                     |
| File rinominato/spostato sul PC                          | Copiato col nuovo nome; il vecchio resta sul backup                      |
| L'utente cancella un file sul backup                     | Al giro successivo, se il file esiste ancora sul PC, **viene ricopiato** |
| L'utente cancella una vecchia versione sul backup        | Nulla (non è più sul PC, non si può ricreare)                            |

> Nota sul "file modificato": sovrascrivere un file sul backup equivale di fatto a cancellarne il contenuto
> precedente. Per questo la vecchia versione viene spostata in un'area versioni (vedi §4.3). Questo è anche
> la miglior difesa contro i ransomware: se il PC viene cifrato, le versioni buone restano sul backup.

---

## 2. Requisiti

### 2.1 Funzionali (dalla tua richiesta)
1. **Funzionalità di base**: backup incrementale, esclusioni, storico esecuzioni, log, ripristino (restore), verifica integrità.
2. **Interfaccia utente**: creare/modificare job di backup, vedere stato, storico, errori, spazio sui dischi, avviare manualmente, sfogliare e ripristinare file.
3. **Modulo di sicurezza**: autenticazione all'interfaccia, gestione sicura delle credenziali di rete, verifica integrità, audit log, cifratura opzionale.
4. **Esecuzione automatica e schedulata** su **una o più** destinazioni (dischi collegati o percorsi di rete).
5. **Solo copia, mai cancellazione** sul backup + ricopia dei file cancellati dall'utente sul backup.

### 2.2 Non funzionali
- **Affidabilità**: un'interruzione (stacco del disco, spegnimento) non deve mai lasciare file corrotti sul backup.
- **Semplicità**: niente funzioni superflue; configurazione comprensibile.
- **Testabilità**: l'invariante "mai cancellare" deve essere *verificata automaticamente* dai test, non solo promessa.
- **Portabilità**: Windows / Linux / macOS (vedi **D1**).

### 2.3 Fuori ambito (v1)
Backup su cloud (S3, Google Drive…), deduplicazione a blocchi, compressione, backup immagine-disco,
gestione multi-utente/multi-PC centralizzata, snapshot VSS di Windows (vedi §8 rischi).

---

## 3. Architettura

```
                ┌──────────────────────────────────────────────────────────┐
                │                    backup-sys daemon                      │
                │  (processo in background, parte all'avvio del sistema)    │
                │                                                          │
  Browser ────► │  Web UI (FastAPI+Jinja2)  ◄──►  Service layer            │
 127.0.0.1      │      ▲ auth / CSRF                │                      │
                │      │                            ▼                      │
  CLI ────────► │  API locale              Scheduler (croniter)            │
                │                                   │ trigger: cron,       │
                │                                   │ intervallo, disco    │
                │                                   ▼ collegato            │
                │                         ┌──────────────────┐             │
                │                         │  Backup Engine   │             │
                │                         │ scan → diff →    │             │
                │                         │ plan → copy      │             │
                │                         └────────┬─────────┘             │
                │      Security module             │                       │
                │  (auth, keyring, crypto,   ┌─────▼──────┐                │
                │   integrity, audit)  ◄────►│ SafeWriter │ ◄── UNICO punto│
                │                            │ (no delete)│    che scrive  │
                │  Catalog (SQLite)          └─────┬──────┘    sul backup  │
                └──────────────────────────────────┼───────────────────────┘
                                                   ▼
                     ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
                     │ Disco USB  E:│   │ Disco USB  F:│   │ \\NAS\backup │
                     └──────────────┘   └──────────────┘   └──────────────┘
```

### 3.1 Stack tecnologico (proposta)
| Ambito            | Scelta                                   | Motivo                                                   |
|-------------------|------------------------------------------|----------------------------------------------------------|
| Linguaggio        | **Python 3.12+**                         | Il tuo linguaggio principale, ottimo I/O su filesystem   |
| Gestione progetto | `uv` + `pyproject.toml`                  | Veloce, lockfile riproducibile                           |
| Scheduler         | loop proprio + `croniter`                | Cron, intervalli, "disco collegato" e recupero esecuzioni perse con un'unica logica (deviazione da APScheduler, più semplice) |
| Database          | SQLite (stdlib `sqlite3`, WAL)           | Zero installazioni, transazionale                        |
| Config            | TOML (`tomllib` + `tomli-w`)             | Leggibile e modificabile anche a mano                    |
| UI                | FastAPI + Jinja2 + CSS/JS propri         | Nessun build JS né CDN: funziona offline, servita dal daemon (**D2**)|
| Tray icon         | `pystray`                                | Icona di stato + "Apri interfaccia"                      |
| Password          | `argon2-cffi` (Argon2id)                 | Standard attuale per hashing password                    |
| Credenziali       | `keyring`                                | Windows Credential Manager / Keychain / Secret Service   |
| Cifratura         | `cryptography` (AES-256-GCM)             | Libreria di riferimento, primitive AEAD                  |
| Notifiche         | notifiche della tray icon (`pystray`)    | Avvisi fine backup / errori                              |
| CLI               | `typer`                                  | Comandi `backup-sys run/status/restore/verify`           |
| Test              | `pytest`, `hypothesis`, `pytest-cov`     | Test property-based sull'invariante                      |
| Qualità           | `ruff`, `mypy --strict`                  |                                                          |
| CI                | GitHub Actions (matrice Win/Linux/macOS) |                                                          |
| Packaging         | PyInstaller (exe Windows) + wheel        |                                                          |

### 3.2 Struttura del repository
```
backup-sys/
├── pyproject.toml
├── src/backupsys/
│   ├── core/
│   │   ├── scanner.py        # scansione sorgenti + esclusioni
│   │   ├── differ.py         # confronto sorgente ↔ destinazione reale
│   │   ├── planner.py        # produce il piano di azioni (COPY / NEW_VERSION / SKIP)
│   │   ├── safe_writer.py    # UNICO modulo che scrive sul backup — nessuna API di cancellazione
│   │   ├── retention.py      # rotazione versioni: UNICO modulo che può cancellare (solo versions/)
│   │   ├── engine.py         # orchestrazione di un'esecuzione
│   │   └── restore.py        # ripristino file/versioni
│   ├── destinations/
│   │   ├── base.py           # interfaccia Destination
│   │   ├── local.py          # dischi locali/USB, identificati da marker
│   │   └── network.py        # percorsi UNC/montati + credenziali
│   ├── scheduler/            # loop di scheduling, trigger "disco collegato", lock
│   ├── security/
│   │   ├── auth.py           # password UI, sessioni, CSRF
│   │   ├── secrets.py        # keyring
│   │   ├── crypto.py         # cifratura opzionale per destinazione
│   │   ├── integrity.py      # hash SHA-256, manifest, verifica
│   │   ├── audit.py          # audit log a catena di hash
│   │   └── guard.py          # anti-ransomware (soglia anomalie)
│   ├── catalog/              # SQLite: job, esecuzioni, file, versioni, hash
│   ├── config/               # modello config (pydantic) + load/save TOML
│   ├── ui/                   # FastAPI app, templates Jinja2, static
│   ├── daemon.py             # avvio servizio, tray, autostart
│   └── cli.py
├── tests/
│   ├── unit/ integration/ property/
│   └── test_no_delete_policy.py   # test statico (AST) anti-cancellazione
└── docs/
```

---

## 4. Il motore di backup (cuore del sistema)

### 4.1 Ciclo di un'esecuzione
1. **Pre-check**: destinazione presente? è *proprio quel* disco (marker ID)? spazio libero sufficiente? lock acquisito?
2. **Scan** delle sorgenti (`os.scandir`, ricorsivo, iterativo) applicando le esclusioni (glob, es. `**/node_modules`, `*.tmp`, `~$*`).
3. **Diff** confrontando ogni file sorgente con il **file reale presente sulla destinazione** (non con il database!).
   - Assente sulla destinazione → `COPY` (così un file cancellato dall'utente sul backup viene ricopiato in automatico).
   - Presente ma diverso (size o mtime, tolleranza 2 s per FAT32/exFAT/SMB) → `NEW_VERSION`.
   - Uguale → `SKIP`. Modalità "profonda" opzionale: confronto hash SHA-256.
4. **Guard anti-ransomware**: se la percentuale di file modificati supera una soglia (es. 30%), il job si mette in pausa e chiede conferma all'utente.
5. **Copy** tramite `SafeWriter` (vedi §4.2), con hash calcolato *durante* la copia (streaming, un solo passaggio).
6. **Registrazione** su catalogo: esito, file copiati, byte, errori, durata. Audit log.
7. **Report + notifica**.

Per più destinazioni nello stesso job: scan una sola volta, poi diff/copy per ciascuna destinazione disponibile.
Destinazioni assenti (disco non collegato) → saltate e segnalate, recuperate al primo collegamento.

### 4.2 SafeWriter — come si garantisce il "mai cancellare"
Difesa a più livelli:
1. **Per costruzione**: `SafeWriter` espone solo `write_new()` e `archive_then_write()`. Non esiste un metodo di delete.
2. **Scrittura atomica**: copia su `nome.bsys-partial` → `fsync` → `os.replace`/rename verso il nome finale solo se il nome finale **non esiste**. Uno stacco del disco lascia al massimo un `.bsys-partial`, mai un file troncato.
   I `.bsys-partial` orfani non vengono cancellati: vengono riutilizzati/rinominati al giro dopo (o mostrati all'utente).
3. **Mai sovrascrivere**: prima di scrivere una nuova versione, la vecchia viene **spostata** (rename sullo stesso volume, operazione atomica e senza perdita di dati) nell'area versioni.
4. **Test statico (AST)**: un test fallisce se nel codice (fuori da `retention.py`) compaiono `os.remove`, `os.unlink`, `shutil.rmtree`, `Path.unlink`, `Path.rmdir`, `os.rmdir`, `shutil.move` con sovrascrittura, ecc.
5. **Test property-based (Hypothesis)**: genera sequenze casuali di operazioni (crea/modifica/cancella/rinomina file sul PC, cancella file sul backup, interrompi la copia…) ed esegue backup ripetuti, verificando ad ogni passo che **ogni contenuto mai scritto sul backup sia ancora presente** (salvo quelli cancellati dall'utente simulato).

### 4.3 Layout sul disco di backup (proposta, **D3**)
```
<DISCO>/backup-sys/
├── .bsys-volume.json             # ID univoco del disco (riconosciuto anche se cambia lettera E:→F:)
└── <nome-job>/
    ├── current/                  # copia speculare navigabile con Esplora Risorse
    │   └── C/Users/andrea/Documenti/...
    ├── versions/
    │   └── 2026-11-06T10-00-00/  # versioni precedenti, raggruppate per esecuzione
    │       └── C/Users/andrea/Documenti/relazione.docx
    └── .bsys/
        ├── manifest.sqlite       # hash e metadati dei file su QUESTO disco (verifica offline)
        └── runs/                 # copia dei report delle esecuzioni
```
I file sono **in chiaro e navigabili** (se la cifratura non è attiva): puoi recuperarli anche senza il programma.

### 4.4 Ripristino e verifica
- **Restore**: sfoglia `current/` o una data in `versions/`, ripristina file/cartelle nella posizione originale o altrove. Il restore non sovrascrive mai file sul PC senza conferma (rinomina in `nome (ripristinato).ext`).
- **Verify**: rilegge i file sul backup e ne confronta l'hash con il manifest → rileva bit-rot, settori danneggiati, manomissioni. Schedulabile (es. mensile).

### 4.5 Rotazione delle versioni (D3)
Il modulo `core/retention.py` è **l'unico** autorizzato a cancellare, e solo cartelle `versions/<data>` intere:
- **Quando** (opzione b, decisa il 2026-11-06): se le versioni occupano più di `max_versions_percent` (default 30%)
  della capacità (la quota `max_backup_gb` se impostata, altrimenti il disco); se backup-sys supererebbe la quota;
  oppure se lo spazio libero non basta per la copia in corso (+ margine del 2%, max 64 MB).
- **Cosa**: le cartelle `versions/<data>` più vecchie, una alla volta, finché si rientra nei limiti.
- **Mai**: nulla dentro `current/`; nessuna versione più giovane di `min_age_days` (default 30 giorni);
  nessun percorso che non sia esattamente `…/backup-sys/<job>/versions/<timestamp>` (controllo sul percorso risolto).
- Se rispettando queste regole lo spazio non basta, il programma **non cancella altro**: avvisa e ferma la copia su quel disco.
- Ogni cancellazione viene registrata nell'audit log.
- Il guard anti-ransomware viene valutato **prima** della rotazione: un'esecuzione sospetta non arriva mai a ruotare versioni.

Il test AST anti-cancellazione permette le API di cancellazione **solo** in `retention.py`.

---

## 5. Destinazioni
- **Dischi locali/USB**: identificati dal file marker `.bsys-volume.json` (UUID), non dalla lettera di unità. Se colleghi un disco diverso con la stessa lettera, il backup non parte.
- **Solo dischi vuoti** (decisione del 2026-11-06, per sicurezza): un disco viene inizializzato solo se l'intero volume è vuoto (ignorando le cartelle di sistema di Windows); per i percorsi di rete deve essere vuota la cartella indicata. Le sottocartelle mancanti vengono create.
- **Rete**: percorsi UNC (`\\NAS\backup`) o condivisioni montate (SMB/NFS). Credenziali salvate nel keyring del sistema operativo, mai nel file di config.
- **Più destinazioni per job**: ognuna con stato indipendente (un disco può essere aggiornato a ieri, un altro alla settimana scorsa).
- **Controllo spazio**: stima dei byte da copiare prima di iniziare; se lo spazio non basta il job si ferma *prima* di iniziare e avvisa l'utente (il programma non può fare pulizia da sé, per definizione).

---

## 6. Scheduling ed esecuzione automatica
- **Trigger**: espressione cron / intervallo ("ogni giorno alle 13:00", "ogni 4 ore") e **"quando il disco viene collegato"** (polling leggero dei volumi ogni ~10 s).
- **Recupero esecuzioni perse**: se il PC era spento all'ora prevista, il backup parte appena possibile.
- **Avvio automatico del daemon** all'accesso utente: Utilità di pianificazione su Windows, `systemd --user` su Linux, `launchd` su macOS. Installazione/rimozione gestita dal comando `backup-sys install-autostart`.
- **Concorrenza**: un lock per destinazione impedisce due esecuzioni simultanee sullo stesso disco.
- **Bassa priorità I/O/CPU** opzionale, per non rallentare il PC durante il lavoro.

---

## 7. Modulo di sicurezza
| Componente            | Cosa fa                                                                                              |
|-----------------------|------------------------------------------------------------------------------------------------------|
| **Accesso UI**        | Server in ascolto **solo su 127.0.0.1**; password locale (Argon2id); cookie di sessione `HttpOnly`/`SameSite=Strict`; token CSRF; blocco dopo N tentativi falliti |
| **Credenziali**       | Password di rete e chiavi nel keyring dell'OS                                                        |
| **Integrità**         | SHA-256 di ogni file copiato, manifest per disco, job di verifica                                    |
| **Audit log**         | Registro append-only di ogni operazione e modifica di configurazione; ogni riga contiene l'hash della precedente (catena) → manomissioni rilevabili |
| **Anti-ransomware**   | Soglia di file modificati per esecuzione + estensioni sospette → pausa e richiesta di conferma      |
| **Cifratura (opz.)**  | Per destinazione: AES-256-GCM a blocchi in streaming, chiave derivata da passphrase (Argon2id), file `.bsenc`. Utile per dischi che escono di casa. **Se perdi la passphrase perdi i dati**: verrà generato un "recovery kit" da stampare (**D4**) |
| **Minimo privilegio** | Il daemon gira come utente normale, non come amministratore/root                                     |

---

## 8. Rischi e punti delicati
| Rischio                                           | Mitigazione                                                                                  |
|---------------------------------------------------|----------------------------------------------------------------------------------------------|
| Disco che si riempie                              | Rotazione versioni (§4.5), dashboard spazio, stop preventivo se `current/` non ci sta     |
| File aperti/bloccati su Windows (Outlook .pst…)   | v1: retry + segnalazione nel report. VSS (snapshot) eventualmente in v2                      |
| mtime inaffidabili su FAT32/exFAT/rete            | Tolleranza 2 s + modalità hash opzionale                                                     |
| Percorsi lunghi Windows (>260 caratteri)          | Prefisso `\\?\` nelle operazioni su Windows                                                  |
| Nomi file non validi sul file system di destinazione (es. `:` da Linux verso NTFS/exFAT) | Mappatura reversibile dei caratteri + segnalazione |
| Case-sensitivity (Linux → NTFS)                   | Rilevamento collisioni `A.txt`/`a.txt`, segnalate                                            |
| Link simbolici / junction                         | Non seguiti di default (evita cicli e duplicati), opzione per seguirli                       |
| Stacco del disco a metà copia                     | Scrittura atomica `.bsys-partial` + rename                                                   |

---

## 9. Decisioni prese

| #  | Decisione | Scelta |
|----|-----------|--------|
| D1 | Sistema operativo | **Windows 11**, uso privato (il codice resta portabile, i test girano anche su Linux) |
| D2 | Interfaccia | **Web UI locale** (127.0.0.1) + icona nella tray |
| D3 | File modificato | **Versioning**, con **rotazione automatica delle versioni più vecchie** in base allo spazio (§4.5) |
| D4 | Cifratura | **Opzionale per destinazione**, disattivata di default |
| D5 | Pulizia | Nessuna funzione di cancellazione manuale nel programma; l'unica cancellazione automatica è la rotazione di D3 |
| D6 | Rete | **Percorsi UNC / unità di rete mappate** |
| D7 | Lingua | **Solo italiano** |

---

## 10. Fasi di sviluppo

Ogni fase si chiude con codice funzionante, test verdi e un commit/push sul branch.

| Fase | Contenuto | Deliverable verificabile |
|------|-----------|--------------------------|
| **0. Fondamenta** | `pyproject`, `uv`, ruff, mypy, pytest, CI GitHub Actions (Win/Linux/macOS), struttura pacchetti, modello di configurazione | `pytest` e CI verdi su scheletro vuoto |
| **1. Motore core** | scanner, esclusioni, differ, planner, **SafeWriter**, versioning, scrittura atomica, hash in streaming, catalogo SQLite | `backup-sys run` da CLI fa un backup corretto su cartella locale; test property-based e test AST anti-cancellazione verdi |
| **2. Destinazioni** | marker di volume, multi-destinazione, percorsi di rete + keyring, controllo spazio, lock | Backup di un job su 2+ destinazioni, rifiuto di disco "sbagliato" |
| **3. Scheduler & daemon** | loop di scheduling, trigger cron/intervallo/"disco collegato", recupero esecuzioni perse, autostart per OS, notifiche, tray | Il backup parte da solo all'orario e al collegamento del disco |
| **4. Sicurezza** | auth UI, audit log a catena, guard anti-ransomware, verifica integrità, cifratura opzionale | Test su manomissione audit log, verify che rileva file corrotti, roundtrip cifratura |
| **5. Interfaccia** | dashboard (stato job, ultimo esito, spazio dischi), CRUD job/destinazioni/schedule, storico e log, avvio manuale con avanzamento live, browser e restore | UI completa, test end-to-end con Playwright |
| **6. Restore & verify da UI** | ripristino file/cartelle/versioni, verifica schedulabile | Ripristino di una versione di 3 giorni fa da UI |
| **7. Packaging & docs** | eseguibile Windows (PyInstaller), installazione autostart, README d'uso, guida al ripristino "senza programma" | Pacchetto installabile + documentazione |

Ordine pensato perché la parte che *non deve mai sbagliare* (motore + invariante) venga costruita e blindata per prima;
UI e comodità vengono dopo, sopra fondamenta già testate.

---

## 11. Strategia di test
- **Unit**: scanner/esclusioni, differ (tolleranza mtime), planner, crypto, audit chain.
- **Integration**: backup reali su directory temporanee che simulano dischi; simulazione di disco scollegato a metà copia (eccezione iniettata nel writer).
- **Property-based (Hypothesis)**: l'invariante append-only su migliaia di sequenze casuali di eventi.
- **Statici**: test AST che vieta le API di cancellazione ovunque tranne `retention.py`; `mypy --strict`.
- **E2E UI**: Playwright (Chromium) sui flussi principali.
- **Copertura obiettivo**: ≥ 90% sul pacchetto `core/`.
