# backup-sys

Sistema di backup automatico **append-only** per Windows 11: copia i file del PC su uno o più dischi
esterni o percorsi di rete, a orari programmati o quando colleghi il disco, e **non cancella mai**
la copia dei tuoi file sul backup.

- **Copia solo-aggiunta**: un file cancellato dal PC resta sul backup; un file che cancelli tu dal backup
  viene rimesso al giro successivo (se è ancora sul PC).
- **Versioni**: quando un file cambia, la versione precedente viene conservata in `versions/`.
  Le versioni più vecchie vengono ruotate solo quando lo spazio sul disco supera i limiti che imposti.
- **Più destinazioni**: dischi USB (riconosciuti anche se cambia la lettera di unità) e condivisioni di rete.
- **Interfaccia web locale** in italiano, raggiungibile solo dal tuo PC, con icona nella tray.
- **Sicurezza**: password dell'interfaccia, anti-ransomware, verifica d'integrità SHA-256,
  audit log a prova di manomissione, cifratura AES-256 opzionale per disco.

Il piano di sviluppo e le decisioni prese sono in [`docs/PIANO_DI_SVILUPPO.md`](docs/PIANO_DI_SVILUPPO.md).

---

## Installazione

### Eseguibile per Windows
1. Su GitHub apri **Actions → Build Windows → Run workflow**. A fine build scarica l'artefatto `backup-sys-windows`.
2. Estrai la cartella, per esempio in `%LOCALAPPDATA%\Programs\backup-sys`.
3. Avvia **`backup-sys.exe`** con un doppio clic: parte il servizio, compare l'icona nella tray
   e si apre l'interfaccia su <http://127.0.0.1:8765/>.
4. Al primo accesso scegli la password dell'interfaccia.
5. In **Sicurezza → Avvio automatico** attiva l'avvio all'accesso a Windows.

### Da sorgente (sviluppo)
Prerequisiti: Python ≥ 3.12 e [uv](https://docs.astral.sh/uv/).

```powershell
git clone https://github.com/andrewdotexs/backup-sys
cd backup-sys
uv sync
uv run backup-sys daemon --open
```

### Aggiornamento
1. Chiudi backup-sys dall'icona nella tray: **Esci**.
2. Sostituisci il contenuto della cartella del programma con quello della nuova versione: elimina i vecchi
   `backup-sys*.exe` e `_internal`, poi estrai lì il nuovo zip. Usa la **stessa cartella**, così l'avvio
   automatico continua a funzionare.
3. Avvia `backup-sys.exe`.

Configurazione, password, storico e credenziali restano: sono in `%APPDATA%`, `%LOCALAPPDATA%` e nel
Gestore credenziali, non nella cartella del programma. Il catalogo viene aggiornato da solo al primo avvio.
**Non usare il disinstallatore per aggiornare**: cancellerebbe anche configurazione e storico.

### Disinstallazione
Avvia **`backup-sys-uninstall.exe`** dalla cartella del programma (oppure `backup-sys uninstall` da sorgente).
Prima di chiedere conferma mostra cosa rimuoverà; poi bisogna scrivere `RIMUOVI`.

Rimuove:
- la configurazione e la password dell'interfaccia (`%APPDATA%\backup-sys`);
- catalogo, storico, audit log e log (`%LOCALAPPDATA%\backup-sys`);
- le credenziali nel Gestore credenziali di Windows (password di rete, chiavi di cifratura, chiave dell'audit log);
- l'avvio automatico;
- i file del programma.

Del programma vengono rimossi solo `backup-sys*.exe` e `_internal`. La cartella viene eliminata solo se poi
resta vuota: se avevi estratto il programma, per esempio, sul Desktop, il resto del Desktop non viene toccato.

**I dischi di backup non vengono mai toccati**: restano leggibili con Esplora file (o con `decrypt` se cifrati).
Se un disco è cifrato, dopo la disinstallazione serviranno la passphrase o il codice di recupero.
Per eliminare i backup, formatta il disco.

Opzioni: `--keep-program` rimuove solo i dati; `--yes` salta la conferma.

---

## Primi passi
1. **Formatta il disco** (consigliato NTFS): per sicurezza backup-sys accetta solo dischi **vuoti**.
2. **Dischi → Aggiungi disco**: un nome (es. "Disco USB blu") e il percorso (`E:\`, `E:\Backup` oppure `\\NAS\backup`).
   Una sottocartella che non esiste ancora viene creata al primo backup.
   Per la rete puoi indicare utente e password: vengono salvati nel Gestore credenziali di Windows.
3. **Backup → Nuovo backup**: scegli le cartelle da salvare (una per riga), i dischi e la frequenza:
   ogni giorno, ogni settimana, ogni N ore o cron avanzato. Opzionalmente anche "quando collego un disco".
4. Premi **Esegui ora** per il primo backup e segui l'avanzamento nella **Panoramica** o sull'icona nella tray,
   che durante la copia mostra la percentuale; il tooltip e il menu dell'icona riportano backup, disco e avanzamento.
5. Se alcuni file vengono saltati (es. sempre bloccati da un programma), in **Storico → dettaglio** puoi premere
   **Escludi dai prossimi backup** accanto al file: viene aggiunto alle esclusioni come percorso esatto.
   Le copie già salvate restano; per annullare modifica le esclusioni del backup.

---

## Come funziona

### Struttura sul disco di backup
```
E:\backup-sys\
├── .bsys-volume.json                  ID del disco: lo riconosce anche se diventa F:
└── Documenti-1a2b3c4d5e6f\            una cartella per ogni backup (job)
    ├── current\C\Users\andrea\...     copia speculare, navigabile con Esplora file
    ├── versions\2026-11-06T13-00-00\  versioni sostituite in quell'esecuzione
    └── .bsys\manifests\               impronte SHA-256 di tutto ciò che è stato scritto
```
I file in `current\` e `versions\` sono **normali file** (se il disco non è cifrato): li puoi aprire
e copiare anche senza backup-sys.

### Cosa succede quando…
| Evento | Comportamento |
|---|---|
| Crei un file sul PC | Viene copiato |
| Modifichi un file | La nuova versione va in `current\`, la vecchia in `versions\<data>\` |
| Cancelli un file dal PC | Resta sul backup |
| Rinomini o sposti un file sul PC | Viene riconosciuto dal contenuto (stessa dimensione, data e impronta SHA-256): il nuovo nome viene **collegato** al dato già sul backup (hard link NTFS, nessuna copia e nessuno spazio in più). Il vecchio nome resta e la pagina "Versioni" del nuovo nome mostra anche la storia del vecchio. Su exFAT/FAT32 il file viene copiato normalmente |
| Cancelli un file dal backup | Viene ricopiato al giro successivo (se è ancora sul PC) |
| Il disco non è collegato all'ora prevista | Il backup su quel disco parte appena lo ricolleghi |
| Il PC era spento all'ora prevista | Il backup parte appena il PC si riaccende |
| Il disco di backup riceve un'altra lettera (E: → G:) | Viene cercato sulle altre unità (dischi fissi e rimovibili) tramite il suo identificativo, nello stesso percorso relativo; la nuova lettera viene salvata in configurazione |
| Al posto di E: c'è un altro disco | Su quel disco non viene scritto nulla: il backup va sul disco giusto se è collegato con un'altra lettera, altrimenti non parte |
| Il disco nuovo contiene altri file | Non viene inizializzato né toccato: va formattato prima |
| Stacchi il disco a metà copia | Nessun file corrotto: si scrive su `*.bsys-partial` e si rinomina solo a copia finita |
| Cambia di colpo una gran parte dei file (ransomware?) | Il backup va **in pausa** e chiede conferma |

### Rotazione delle versioni (l'unica cancellazione automatica)
Per ogni disco, in **Dischi → Gestisci → Spazio e versioni**, imposti:
- una **quota** facoltativa: lo spazio massimo che backup-sys può occupare su quel disco (vuoto = tutto il disco);
- la percentuale massima della quota (o del disco) per le versioni (default 30%);
- l'età minima sotto cui una versione non viene mai ruotata (default 30 giorni).

Le versioni vengono ruotate quando superano il loro limite, quando backup-sys supererebbe la quota,
oppure quando lo spazio libero non basta per la copia in corso. In quel caso conta il margine di sicurezza:
il 2% del disco, al massimo 64 MB. In quei casi il programma cancella le cartelle `versions\<data>` **più vecchie**,
una alla volta, finché rientra. Non tocca mai `current\`, né le versioni più giovani dell'età minima.
Se non basta, si ferma e ti avvisa. Ogni rotazione viene registrata nell'audit log.

### Sicurezza
| Componente | Dettagli |
|---|---|
| Interfaccia | Solo `127.0.0.1`, password Argon2id, sessioni con scadenza, token CSRF, CSP rigida, protezione DNS-rebinding, blocco dopo 5 tentativi errati |
| Credenziali | Password di rete e chiavi nel Gestore credenziali di Windows, mai nei file |
| Anti-ransomware | Pausa se in un'esecuzione cambia più della soglia (default 30%) dei file, o compaiono estensioni tipiche (`.locked`, `.encrypted`, …) |
| Integrità | SHA-256 calcolato durante la copia. Verifica periodica (default ogni 30 giorni): i file corrotti vengono ricopiati dal PC in automatico |
| Audit log | Ogni operazione e modifica è registrata con catena HMAC: righe alterate o rimosse vengono rilevate |
| Cifratura | Opzionale per disco: AES-256-GCM a blocchi, chiave protetta da passphrase (Argon2id), codice di recupero stampabile |
| Privilegi | Il servizio gira come utente normale, senza diritti di amministratore |

---

## Ripristino
Dall'interfaccia, in **Ripristino**: scegli il backup e il disco, poi naviga nella copia corrente o in una data
passata. Puoi ripristinare un file, una sua versione precedente o un'intera cartella, nella posizione originale
o altrove. **Nessun file sul PC viene sovrascritto**: se esiste già, il ripristinato si chiama
`nome (ripristinato AAAA-MM-GG HH-MM).ext`.

### Recupero d'emergenza senza backup-sys
- **Disco non cifrato**: apri `E:\backup-sys\<job>\current\` con Esplora file e copia ciò che ti serve.
  Le versioni precedenti sono in `versions\`.
- **Disco cifrato**: usa il comando `decrypt` con il codice di recupero oppure con la passphrase:
  ```powershell
  backup-sys-cli.exe decrypt "E:\backup-sys\Documenti-…\current" "C:\Recuperati" --code XXXX-XXXX-…
  backup-sys-cli.exe decrypt "E:\backup-sys\Documenti-…\current" "C:\Recuperati" --keyfile "E:\backup-sys\.bsys-key.json"
  ```

---

## Riga di comando
| Comando | Funzione |
|---|---|
| `backup-sys daemon [--open] [--no-tray]` | Avvia il servizio con interfaccia e scheduler |
| `backup-sys run "Documenti" [--confirm]` | Esegue subito un backup (`--confirm` sblocca una pausa anti-ransomware) |
| `backup-sys status` | Stato di dischi e backup |
| `backup-sys verify "Documenti"` | Verifica d'integrità su tutti i dischi del job |
| `backup-sys decrypt SORGENTE DESTINAZIONE --code …` | Decifra file di un disco cifrato |
| `backup-sys autostart on` / `off` | Attiva o disattiva l'avvio automatico |
| `backup-sys reset-password` | Nuova password dell'interfaccia (serve accesso al PC) |
| `backup-sys uninstall [--keep-program] [--yes]` | Disinstalla e rimuove i dati locali (i dischi di backup restano) |

Con l'eseguibile Windows usa `backup-sys-cli.exe` al posto di `backup-sys`; per la disinstallazione basta un doppio clic su `backup-sys-uninstall.exe`.

## File locali
| Cosa | Dove |
|---|---|
| Configurazione (`config.toml`, `auth.json`) | `%APPDATA%\backup-sys\` |
| Catalogo, audit log, log | `%LOCALAPPDATA%\backup-sys\` |

---

## Sviluppo
```bash
uv sync
uv run pytest                  # unit, integrazione, property-based, test anti-cancellazione
uv run ruff check src tests
uv run mypy                    # strict
uv run mypy --platform win32   # type-check del codice specifico Windows
```
Su Linux senza desktop, per avviare il daemon in sviluppo: `BACKUPSYS_KEYRING=memory BACKUPSYS_HOME=/tmp/bsys uv run backup-sys daemon --no-tray`.

### Dischi di backup: regola del disco vuoto
Un disco diventa disco di backup solo se è vuoto. Per i dischi locali si controlla l'intero volume,
ignorando le cartelle di sistema di Windows (`System Volume Information`, `$RECYCLE.BIN`). Per i percorsi
di rete si controlla la cartella indicata. Un disco già inizializzato (con `backup-sys\.bsys-volume.json`)
viene sempre riconosciuto, anche se in seguito ci aggiungi altri file.

### Garanzie verificate dai test
- `tests/unit/test_no_delete_policy.py` analizza il codice sorgente e fallisce se compare un'API di cancellazione
  o sovrascrittura fuori dalle eccezioni motivate (solo `core/retention.py` può cancellare, e solo in `versions/`).
- `tests/property/test_append_only.py` genera migliaia di sequenze casuali di eventi (scritture, cancellazioni sul PC
  e sul backup, conflitti file/cartella) e verifica che nessun contenuto salvato vada mai perso.

### Rinomine e spostamenti
Il riconoscimento confronta i file "nuovi" con quelli presenti sul backup ma non più sul PC: richiede una lettura
dell'elenco dei file del backup (solo quando ci sono file nuovi) e dell'intero file sul PC, che però non viene
riscritto sul disco di backup. Nel conteggio dello spazio i dati collegati valgono una volta sola (per i file da 1 MB in su).

### Limiti noti
- I file aperti in modo esclusivo (es. `.pst` di Outlook aperto) vengono saltati e ritentati al giro dopo.
  Non c'è supporto alle copie shadow VSS.
- Senza "confronto profondo" un file modificato che mantiene identiche dimensione e data di modifica
  (raro, entro 2 secondi) non viene rilevato finché non cambia di nuovo.
  La verifica periodica rileva comunque eventuali corruzioni sul disco di backup.
