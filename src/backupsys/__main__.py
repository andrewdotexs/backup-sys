import os
import sys

from backupsys.cli import main

if __name__ == "__main__":
    # eseguibili Windows senza argomenti (doppio clic)
    if getattr(sys, "frozen", False) and len(sys.argv) == 1:
        exe = os.path.splitext(os.path.basename(sys.executable))[0].lower()
        if exe == "backup-sys-uninstall":
            sys.argv += ["uninstall"]
        elif exe == "backup-sys":
            sys.argv += ["daemon", "--open"]
    main()
