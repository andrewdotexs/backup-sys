import sys

from backupsys.cli import main

if __name__ == "__main__":
    # eseguibile Windows senza argomenti (doppio clic): avvia il servizio e apre l'interfaccia
    if getattr(sys, "frozen", False) and len(sys.argv) == 1:
        sys.argv += ["daemon", "--open"]
    main()
