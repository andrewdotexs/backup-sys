"""Modello della configurazione (salvata in TOML)."""

from __future__ import annotations

import re
import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class RetentionPolicy(BaseModel):
    """Regole di rotazione delle vecchie versioni su un disco (D3)."""

    max_versions_percent: float = Field(30.0, ge=1, le=95)
    min_free_percent: float = Field(10.0, ge=0, le=90)
    min_age_days: int = Field(30, ge=0)


class Destination(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    path: str
    kind: Literal["local", "network"] = "local"
    #: ID scritto nel marker del disco; ``None`` finché il disco non è stato inizializzato.
    volume_id: str | None = None
    encrypted: bool = False
    #: Utente per le condivisioni di rete (la password è nel keyring di Windows).
    network_username: str | None = None
    retention: RetentionPolicy = Field(default_factory=RetentionPolicy)

    @field_validator("path")
    @classmethod
    def _path_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Il percorso della destinazione è obbligatorio")
        return v


class Schedule(BaseModel):
    #: Espressione cron (minuto ora giorno mese giorno-settimana), es. "0 13 * * *".
    cron: str | None = None
    #: In alternativa al cron: ogni N minuti.
    interval_minutes: int | None = Field(None, ge=5)
    #: Esegui quando un disco del job viene collegato.
    on_connect: bool = False

    @field_validator("cron")
    @classmethod
    def _valid_cron(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        from croniter import croniter

        if not croniter.is_valid(v.strip()):
            raise ValueError(f"Espressione cron non valida: {v}")
        return v.strip()

    @model_validator(mode="after")
    def _one_kind(self) -> Schedule:
        if self.cron and self.interval_minutes:
            raise ValueError("Specifica un'espressione cron oppure un intervallo, non entrambi")
        return self

    @property
    def is_manual(self) -> bool:
        return not (self.cron or self.interval_minutes or self.on_connect)


DEFAULT_EXCLUDES = [
    "**/~$*",
    "**/*.tmp",
    "**/Thumbs.db",
    "**/desktop.ini",
    "**/$RECYCLE.BIN/**",
    "**/System Volume Information/**",
    "**/node_modules/**",
    "**/__pycache__/**",
]


class Job(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    sources: list[str]
    destinations: list[str] = Field(default_factory=list)  # id delle destinazioni
    excludes: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    schedule: Schedule = Field(default_factory=Schedule)
    enabled: bool = True
    #: Confronto profondo con hash SHA-256 (più lento) invece di dimensione + data.
    deep_compare: bool = False
    follow_symlinks: bool = False
    #: Guard anti-ransomware: soglia % di file esistenti modificati in un'unica esecuzione.
    guard_threshold_percent: float = Field(30.0, ge=1, le=100)
    #: Il guard si attiva solo se il backup contiene almeno questo numero di file.
    guard_min_files: int = Field(50, ge=1)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Il nome del job è obbligatorio")
        return v

    @field_validator("sources")
    @classmethod
    def _sources(cls, v: list[str]) -> list[str]:
        v = [s.strip() for s in v if s.strip()]
        if not v:
            raise ValueError("Serve almeno una cartella sorgente")
        return v

    @property
    def slug(self) -> str:
        """Nome della cartella del job sul disco di backup (stabile: include l'id)."""
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", self.name).strip("-")[:40] or "job"
        return f"{base}-{self.id}"


class UISettings(BaseModel):
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(8765, ge=1024, le=65535)
    session_minutes: int = Field(30, ge=5)


class AppConfig(BaseModel):
    jobs: list[Job] = Field(default_factory=list)
    destinations: list[Destination] = Field(default_factory=list)
    ui: UISettings = Field(default_factory=UISettings)

    def job(self, job_id: str) -> Job:
        for j in self.jobs:
            if j.id == job_id:
                return j
        raise KeyError(job_id)

    def destination(self, dest_id: str) -> Destination:
        for d in self.destinations:
            if d.id == dest_id:
                return d
        raise KeyError(dest_id)
