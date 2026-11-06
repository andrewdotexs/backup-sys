"""Interfaccia web locale (FastAPI + Jinja2), in ascolto solo su 127.0.0.1."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.datastructures import FormData

from backupsys import __version__
from backupsys.catalog import db
from backupsys.config.models import DEFAULT_EXCLUDES, Destination, Job
from backupsys.core import restore as restore_mod
from backupsys.core.fsutil import human_bytes, is_link_or_junction, real_folder
from backupsys.core.layout import CURRENT
from backupsys.known_folders import known_folders
from backupsys.scheduler import autostart
from backupsys.scheduler.loop import Scheduler
from backupsys.scheduler.timing import describe
from backupsys.security import crypto
from backupsys.security.auth import AuthStore, LoginThrottle, Session, SessionManager
from backupsys.service import BackupService
from backupsys.ui.errors_view import group_errors
from backupsys.ui.forms import dest_from_form, job_from_form, schedule_to_form

HERE = Path(__file__).parent
COOKIE = "bsys_session"

STATUS_LABELS = {
    db.OK: "Completato",
    db.WARNING: "Con avvisi",
    db.FAILED: "Non riuscito",
    db.SKIPPED: "Rimandato",
    db.PAUSED: "In pausa",
    db.RUNNING: "In corso",
    db.CANCELLED: "Interrotto",
}
TRIGGER_LABELS = {
    "manual": "manuale",
    "schedule": "programmato",
    "connect": "disco collegato",
    "confirm": "confermato",
    "verify": "riparazione dopo verifica",
}


class InstallationDamagedError(RuntimeError):
    pass


class LoginRequired(Exception):
    pass


class CsrfError(Exception):
    pass


def _fmt_dt(v: Any) -> str:
    if not v:
        return "—"
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return str(v)
    assert isinstance(v, datetime)
    today = datetime.now().date()
    if v.date() == today:
        return f"oggi {v:%H:%M}"
    return f"{v:%d/%m/%Y %H:%M}"


def create_app(
    service: BackupService,
    scheduler: Scheduler | None = None,
    auth: AuthStore | None = None,
    sessions: SessionManager | None = None,
    port: int = 8765,
) -> FastAPI:
    missing = [d for d in (HERE / "templates", HERE / "static") if not d.is_dir()]
    if missing:
        raise InstallationDamagedError(
            "Installazione di backup-sys danneggiata: mancano i file dell'interfaccia "
            f"({', '.join(str(m) for m in missing)}). Estrai di nuovo lo zip del programma in una cartella pulita."
        )
    app = FastAPI(title="backup-sys", docs_url=None, redoc_url=None, openapi_url=None)
    auth = auth or AuthStore()
    sessions = sessions or SessionManager(service.config.ui.session_minutes)
    throttle = LoginThrottle()
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    env = templates.env
    env.filters["bytes"] = human_bytes
    env.filters["dt"] = _fmt_dt
    env.filters["status_label"] = lambda s: STATUS_LABELS.get(s, s)
    env.filters["trigger_label"] = lambda s: TRIGGER_LABELS.get(s, s)
    env.filters["describe"] = describe
    env.filters["q"] = lambda s: quote(str(s), safe="")
    env.globals["version"] = __version__
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost", "testserver"}

    # ------------------------------------------------------------------ sicurezza HTTP
    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        host = request.headers.get("host", "")
        if host not in allowed_hosts:  # protezione da DNS rebinding
            return Response("Host non consentito", status_code=400)
        response: Response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "login richiesto"}, status_code=401)
        return RedirectResponse("/setup" if not auth.configured else "/login", status_code=303)

    @app.exception_handler(CsrfError)
    async def _csrf(request: Request, exc: CsrfError) -> Response:
        return Response("Richiesta non valida (token CSRF). Ricarica la pagina.", status_code=403)

    def session(request: Request) -> Session:
        s = sessions.get(request.cookies.get(COOKIE))
        if s is None:
            raise LoginRequired
        return s

    async def form_checked(request: Request, s: Session) -> FormData:
        form = await request.form()
        token = form.get("csrf") or request.headers.get("x-csrf-token")
        if not token or token != s.csrf:
            raise CsrfError
        return form

    def render(request: Request, name: str, s: Session | None, **ctx: Any) -> HTMLResponse:
        ctx.update(
            request=request,
            csrf=s.csrf if s else "",
            logged=s is not None,
            ok=request.query_params.get("ok"),
            err=request.query_params.get("err"),
            nav=name.split(".")[0].split("_")[0],
        )
        return templates.TemplateResponse(request, name, ctx)

    def back(url: str, ok: str | None = None, err: str | None = None) -> RedirectResponse:
        sep = "&" if "?" in url else "?"
        if ok:
            url += f"{sep}ok={quote(ok)}"
        elif err:
            url += f"{sep}err={quote(err)}"
        return RedirectResponse(url, status_code=303)

    def _err(e: Exception) -> str:
        if isinstance(e, ValidationError):
            return "; ".join(str(x["msg"]).removeprefix("Value error, ") for x in e.errors())
        return str(e)

    # ------------------------------------------------------------------ accesso
    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request) -> Response:
        if auth.configured:
            return RedirectResponse("/login", status_code=303)
        return render(request, "setup.html", None)

    @app.post("/setup")
    async def setup_post(request: Request) -> Response:
        if auth.configured:
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        pw, pw2 = str(form.get("password", "")), str(form.get("password2", ""))
        if pw != pw2:
            return back("/setup", err="Le password non coincidono")
        try:
            auth.set_password(pw)
        except ValueError as e:
            return back("/setup", err=str(e))
        service.audit.log("password_set", actor="user")
        return _login_response()

    def _login_response() -> Response:
        s = sessions.create()
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(COOKIE, s.token, httponly=True, samesite="strict", secure=False)
        return r

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        if not auth.configured:
            return RedirectResponse("/setup", status_code=303)
        return render(request, "login.html", None, locked=throttle.remaining_lock())

    @app.post("/login")
    async def login_post(request: Request) -> Response:
        if throttle.remaining_lock():
            return back("/login", err=f"Troppi tentativi: riprova tra {throttle.remaining_lock()} secondi")
        form = await request.form()
        if auth.verify(str(form.get("password", ""))):
            throttle.success()
            service.audit.log("login", actor="user")
            return _login_response()
        throttle.failure()
        service.audit.log("login_failed", actor="user")
        return back("/login", err="Password errata")

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        s = session(request)
        await form_checked(request, s)
        sessions.revoke(s.token)
        r = RedirectResponse("/login", status_code=303)
        r.delete_cookie(COOKIE)
        return r

    # ------------------------------------------------------------------ panoramica
    def _job_rows() -> list[dict[str, Any]]:
        cfg = service.config
        rows = []
        for j in cfg.jobs:
            last = service.catalog.last_run(j.id)
            nxt = scheduler.next_run_for(j.id) if scheduler else None
            rows.append({"job": j, "last": last, "next": nxt, "busy": service.is_busy(j.id)})
        return rows

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Response:
        s = session(request)
        cfg = service.config
        jobs = _job_rows()
        paused = [r for r in jobs if r["last"] and r["last"].status == db.PAUSED]
        infos = [service.dest_info(d) for d in cfg.destinations]
        return render(request, "dashboard.html", s, jobs=jobs, paused=paused, infos=infos, snapshot=service.snapshot())

    @app.get("/api/status")
    async def api_status(request: Request) -> Response:
        session(request)
        snap = service.snapshot()
        run = snap.get("running")
        if isinstance(run, dict):
            run["bytes_done_h"] = human_bytes(run["bytes_done"])
            run["bytes_total_h"] = human_bytes(run["bytes_total"])
        return JSONResponse(snap)

    @app.post("/jobs/{job_id}/run")
    async def run_job(request: Request, job_id: str) -> Response:
        s = session(request)
        form = await form_checked(request, s)
        confirm = form.get("confirm") == "1"
        try:
            name = service.config.job(job_id).name
        except KeyError:
            return back("/", err="Job inesistente")
        added = service.enqueue_backup(job_id, trigger="confirm" if confirm else "manual", override_guard=confirm)
        if confirm:
            service.audit.log("guard_confirmed", actor="user", job=name)
        return back(str(form.get("next") or "/"), ok=f"Backup «{name}» avviato" if added else "Già in coda")

    @app.post("/cancel")
    async def cancel(request: Request) -> Response:
        s = session(request)
        await form_checked(request, s)
        service.cancel_running()
        return back("/", ok="Interruzione richiesta")

    # ------------------------------------------------------------------ job
    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_page(request: Request) -> Response:
        s = session(request)
        return render(request, "jobs.html", s, jobs=_job_rows(), dests={d.id: d for d in service.config.destinations})

    def _job_form(request: Request, s: Session, job: Job | None) -> Response:
        cfg = service.config
        known = known_folders()
        default_src = known[0][1] if known else str(Path.home())  # i Documenti reali (anche su OneDrive)
        j = job or Job(name="Nuovo backup", sources=[default_src], destinations=[d.id for d in cfg.destinations])
        return render(
            request,
            "jobs_form.html",
            s,
            job=j,
            is_new=job is None,
            sched=schedule_to_form(j.schedule),
            dests=cfg.destinations,
            default_excludes=DEFAULT_EXCLUDES,
            known=known,
        )

    @app.get("/jobs/new", response_class=HTMLResponse)
    async def job_new(request: Request) -> Response:
        return _job_form(request, session(request), None)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_edit(request: Request, job_id: str) -> Response:
        s = session(request)
        try:
            return _job_form(request, s, service.config.job(job_id))
        except KeyError:
            return back("/jobs", err="Job inesistente")

    @app.post("/jobs/save")
    async def job_save(request: Request) -> Response:
        s = session(request)
        form = await form_checked(request, s)
        data: dict[str, object] = {k: form.get(k) for k in form}
        data["destinations"] = form.getlist("destinations")
        job_id = str(form.get("id") or "")
        existing = None
        if job_id:
            try:
                existing = service.config.job(job_id)
            except KeyError:
                existing = None
        try:
            job = job_from_form(data, existing)
            job, notes = _check_sources(job)
            service.save_job(job)
        except (ValidationError, ValueError) as e:
            return back(f"/jobs/{job_id}" if existing else "/jobs/new", err=_err(e))
        if notes:
            return back(f"/jobs/{job.id}", err=f"Backup «{job.name}» salvato, ma: " + " ".join(notes))
        return back("/jobs", ok=f"Backup «{job.name}» salvato")

    def _check_sources(job: Job) -> tuple[Job, list[str]]:
        """Sostituisce i collegamenti (es. «Documenti» di compatibilità) con la cartella reale
        e segnala le cartelle che non esistono."""
        notes: list[str] = []
        sources: list[str] = []
        for src in job.sources:
            if is_link_or_junction(src):
                real = real_folder(src)
                notes.append(f"«{src}» è un collegamento di Windows: uso la cartella reale «{real}».")
                src = real
            elif not os.path.isdir(src):
                notes.append(f"La cartella «{src}» non esiste o non è raggiungibile.")
            if src not in sources:
                sources.append(src)
        return job.model_copy(update={"sources": sources}), notes

    @app.post("/jobs/{job_id}/delete")
    async def job_delete(request: Request, job_id: str) -> Response:
        s = session(request)
        await form_checked(request, s)
        try:
            service.remove_job(job_id)
        except KeyError:
            return back("/jobs", err="Job inesistente")
        return back("/jobs", ok="Job rimosso dalla configurazione (i dati sui dischi restano intatti)")

    # ------------------------------------------------------------------ destinazioni
    @app.get("/destinations", response_class=HTMLResponse)
    async def dests_page(request: Request) -> Response:
        s = session(request)
        infos = [service.dest_info(d, max_age=5) for d in service.config.destinations]
        return render(request, "destinations.html", s, infos=infos)

    @app.get("/destinations/new", response_class=HTMLResponse)
    async def dest_new(request: Request) -> Response:
        s = session(request)
        return render(request, "destinations_form.html", s, dest=Destination(name="", path="E:\\"), is_new=True)

    @app.get("/destinations/{dest_id}", response_class=HTMLResponse)
    async def dest_edit(request: Request, dest_id: str) -> Response:
        s = session(request)
        try:
            d = service.config.destination(dest_id)
        except KeyError:
            return back("/destinations", err="Destinazione inesistente")
        return render(request, "destinations_form.html", s, dest=d, is_new=False, info=service.dest_info(d, 5))

    @app.post("/destinations/save")
    async def dest_save(request: Request) -> Response:
        s = session(request)
        form = await form_checked(request, s)
        dest_id = str(form.get("id") or "")
        existing = None
        if dest_id:
            try:
                existing = service.config.destination(dest_id)
            except KeyError:
                existing = None
        try:
            d = dest_from_form({k: form.get(k) for k in form}, existing)
            service.save_destination(d)
            pw = str(form.get("network_password") or "")
            if pw:
                service.set_network_password(d.id, pw)
        except (ValidationError, ValueError) as e:
            return back(f"/destinations/{dest_id}" if existing else "/destinations/new", err=_err(e))
        if existing is None:
            return back(f"/destinations/{d.id}", ok="Destinazione salvata")
        return back("/destinations", ok="Destinazione salvata")

    @app.post("/destinations/{dest_id}/delete")
    async def dest_delete(request: Request, dest_id: str) -> Response:
        s = session(request)
        await form_checked(request, s)
        service.remove_destination(dest_id)
        return back("/destinations", ok="Destinazione rimossa dalla configurazione (il disco non è stato toccato)")

    @app.post("/destinations/{dest_id}/rebind")
    async def dest_rebind(request: Request, dest_id: str) -> Response:
        s = session(request)
        await form_checked(request, s)
        try:
            service.rebind_destination(dest_id)
        except (OSError, ValueError) as e:
            return back(f"/destinations/{dest_id}", err=str(e))
        return back(f"/destinations/{dest_id}", ok="Disco associato a questa destinazione")

    @app.post("/destinations/{dest_id}/encrypt")
    async def dest_encrypt(request: Request, dest_id: str) -> Response:
        s = session(request)
        form = await form_checked(request, s)
        pw, pw2 = str(form.get("passphrase", "")), str(form.get("passphrase2", ""))
        if pw != pw2:
            return back(f"/destinations/{dest_id}", err="Le passphrase non coincidono")
        try:
            code = service.setup_encryption(dest_id, pw)
        except (OSError, ValueError, crypto.DecryptionError) as e:
            return back(f"/destinations/{dest_id}", err=str(e))
        d = service.config.destination(dest_id)
        return render(request, "destinations_recovery.html", s, dest=d, code=code)

    @app.post("/destinations/{dest_id}/unlock")
    async def dest_unlock(request: Request, dest_id: str) -> Response:
        s = session(request)
        form = await form_checked(request, s)
        try:
            service.unlock_destination(dest_id, str(form.get("passphrase", "")))
        except (OSError, crypto.DecryptionError) as e:
            return back(f"/destinations/{dest_id}", err=str(e))
        return back(f"/destinations/{dest_id}", ok="Chiave sbloccata e salvata nel Credential Manager")

    # ------------------------------------------------------------------ storico
    @app.get("/runs", response_class=HTMLResponse)
    async def runs_page(request: Request) -> Response:
        s = session(request)
        job_id = request.query_params.get("job") or None
        return render(
            request, "runs.html", s, runs=service.catalog.runs(job_id, 200), jobs=service.config.jobs, job_id=job_id
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_detail(request: Request, run_id: int) -> Response:
        s = session(request)
        run = service.catalog.run(run_id)
        if run is None:
            return back("/runs", err="Esecuzione inesistente")
        job: Job | None
        try:
            job = service.config.job(run.job_id)
        except KeyError:
            job = None
        errors = service.catalog.run_errors(run_id)
        groups, loose = group_errors(errors, job)
        return render(
            request,
            "runs_detail.html",
            s,
            run=run,
            dests=service.catalog.run_dests(run_id),
            errors=errors,
            groups=groups,
            loose=loose,
        )

    @app.post("/runs/{run_id}/exclude")
    async def run_exclude(request: Request, run_id: int) -> Response:
        s = session(request)
        form = await form_checked(request, s)
        run = service.catalog.run(run_id)
        if run is None:
            return back("/runs", err="Esecuzione inesistente")
        try:
            path = service.exclude_path(run.job_id, str(form.get("path", "")))
        except KeyError:
            return back(f"/runs/{run_id}", err="Il backup di questa esecuzione non esiste più")
        except ValueError as e:
            return back(f"/runs/{run_id}", err=str(e))
        return back(
            f"/runs/{run_id}",
            ok=f"«{path}» verrà escluso dai prossimi backup. Le copie già salvate restano. "
            "Per annullare: Backup → Modifica → Opzioni avanzate.",
        )

    # ------------------------------------------------------------------ ripristino
    @app.get("/browse", response_class=HTMLResponse)
    async def browse(request: Request) -> Response:
        s = session(request)
        cfg = service.config
        qp = request.query_params
        job_id, dest_id = qp.get("job"), qp.get("dest")
        area, rel = qp.get("area", CURRENT), qp.get("path", "").strip("/")
        ctx: dict[str, Any] = {"jobs": cfg.jobs, "dests": cfg.destinations, "job_id": job_id, "dest_id": dest_id}
        if job_id and dest_id:
            try:
                job, dest = cfg.job(job_id), cfg.destination(dest_id)
            except KeyError:
                return back("/browse", err="Selezione non valida")
            info = service.dest_info(dest, 5)
            dest = info.dest  # percorso aggiornato se il disco ha cambiato lettera
            layout = restore_mod.job_layout(dest, job)
            ctx.update(job=job, dest=dest, info=info, area=area, rel=rel)
            if info.status.ready and layout.current.is_dir():
                try:
                    ctx["areas"] = restore_mod.areas(layout)
                    ctx["entries"] = (
                        restore_mod.browse(layout, area, rel) if rel else restore_mod.source_roots(layout, job, area)
                    )
                    ctx["crumbs"] = restore_mod.crumbs(job, rel)
                    ctx["original"] = str(restore_mod.original_location(job, rel) or "") if rel else ""
                except ValueError as e:
                    return back(f"/browse?job={job_id}&dest={dest_id}", err=str(e))
        return render(request, "browse.html", s, **ctx)

    @app.get("/browse/versions", response_class=HTMLResponse)
    async def browse_versions(request: Request) -> Response:
        s = session(request)
        cfg = service.config
        qp = request.query_params
        try:
            job, dest = cfg.job(qp.get("job", "")), cfg.destination(qp.get("dest", ""))
        except KeyError:
            return back("/browse", err="Selezione non valida")
        rel = qp.get("path", "")
        dest = service.resolved(dest.id)
        layout = restore_mod.job_layout(dest, job)
        try:
            history = restore_mod.file_history(layout, rel)
        except ValueError as e:
            return back("/browse", err=str(e))
        original = restore_mod.original_location(job, rel)
        return render(
            request,
            "browse_versions.html",
            s,
            job=job,
            dest=dest,
            rel=rel,
            versions=history.versions,
            renamed_to=history.renamed_to,
            original=original,
        )

    @app.post("/restore")
    async def do_restore(request: Request) -> Response:
        s = session(request)
        form = await form_checked(request, s)
        job_id, dest_id = str(form.get("job")), str(form.get("dest"))
        area, rel = str(form.get("area", CURRENT)), str(form.get("path", ""))
        target = str(form.get("target") or "").strip() if form.get("mode") == "other" else None
        if form.get("mode") == "other" and not target:
            return back(f"/browse?job={job_id}&dest={dest_id}", err="Indica la cartella in cui ripristinare")
        try:
            rep = service.restore(job_id, dest_id, area, rel, target)
        except (OSError, ValueError, KeyError) as e:
            return back(f"/browse?job={job_id}&dest={dest_id}", err=str(e))
        return render(request, "browse_restored.html", s, report=rep, job_id=job_id, dest_id=dest_id)

    # ------------------------------------------------------------------ sicurezza
    @app.get("/security", response_class=HTMLResponse)
    async def security_page(request: Request) -> Response:
        s = session(request)
        return render(
            request,
            "security.html",
            s,
            audit=service.audit.entries(300),
            chain=service.audit.verify(),
            verifies=service.catalog.verifies(30),
            jobs=service.config.jobs,
            dests={d.id: d for d in service.config.destinations},
            autostart=autostart.is_installed(),
        )

    @app.get("/security/verify/{verify_id}", response_class=HTMLResponse)
    async def verify_detail(request: Request, verify_id: int) -> Response:
        s = session(request)
        v = next((x for x in service.catalog.verifies(1000) if x["id"] == verify_id), None)
        if v is None:
            return back("/security", err="Verifica inesistente")
        return render(request, "security_verify.html", s, v=v, issues=service.catalog.verify_issues(verify_id))

    @app.post("/security/verify")
    async def verify_now(request: Request) -> Response:
        s = session(request)
        form = await form_checked(request, s)
        job_id = str(form.get("job"))
        cfg = service.config
        try:
            job = cfg.job(job_id)
        except KeyError:
            return back("/security", err="Job inesistente")
        for d in job.destinations:
            service.enqueue_verify(job_id, d)
        return back("/security", ok=f"Verifica di «{job.name}» avviata su {len(job.destinations)} dischi")

    @app.post("/security/password")
    async def change_password(request: Request) -> Response:
        s = session(request)
        form = await form_checked(request, s)
        if not auth.verify(str(form.get("old", ""))):
            service.audit.log("password_change_failed", actor="user")
            return back("/security", err="Password attuale errata")
        if form.get("new") != form.get("new2"):
            return back("/security", err="Le nuove password non coincidono")
        try:
            auth.set_password(str(form.get("new", "")))
        except ValueError as e:
            return back("/security", err=str(e))
        service.audit.log("password_changed", actor="user")
        sessions.revoke_all()
        return back("/login", ok="Password cambiata: accedi di nuovo")

    @app.post("/security/autostart")
    async def toggle_autostart(request: Request) -> Response:
        s = session(request)
        form = await form_checked(request, s)
        if form.get("enable") == "1":
            autostart.install()
            service.audit.log("autostart_enabled", actor="user")
            return back("/security", ok="backup-sys partirà automaticamente all'accesso a Windows")
        autostart.uninstall()
        service.audit.log("autostart_disabled", actor="user")
        return back("/security", ok="Avvio automatico disattivato")

    @app.get("/healthz")
    async def healthz() -> Response:
        return JSONResponse({"ok": True, "version": __version__, "pid": os.getpid()})

    return app
