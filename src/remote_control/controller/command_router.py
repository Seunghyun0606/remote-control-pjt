from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import StrEnum

from remote_control.projects.registry import ProjectRegistry


class Intent(StrEnum):
    PROJECTS = "PROJECTS"
    STATUS = "STATUS"
    RUN_PROJECT = "RUN_PROJECT"
    JOBS = "JOBS"
    JOB = "JOB"
    STOP = "STOP"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    STEER = "STEER"
    REDIRECT = "REDIRECT"
    HOSTS = "HOSTS"
    RETRY = "RETRY"
    SESSIONS = "SESSIONS"
    SESSION = "SESSION"
    NEW_SESSION = "NEW_SESSION"
    USE_SESSION = "USE_SESSION"
    DOCTOR = "DOCTOR"
    HELP = "HELP"


@dataclass(slots=True)
class Command:
    intent: Intent
    project_id: str | None = None
    host: str = "auto"
    job_id: str | None = None
    instruction: str | None = None


class CommandParseError(ValueError):
    pass


class CommandRouter:
    def __init__(self, projects: ProjectRegistry) -> None:
        self.projects = projects

    def parse(self, text: str) -> Command:
        text = text.strip()
        if not text:
            raise CommandParseError("empty command")
        if text.startswith("/"):
            return self._parse_slash(text)
        return self._parse_natural(text)

    def _parse_slash(self, text: str) -> Command:
        try:
            parts = shlex.split(text)
        except ValueError as exc:
            raise CommandParseError(str(exc)) from exc

        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]

        if command in {"/start", "/help"}:
            return Command(Intent.HELP)
        if command == "/projects":
            return Command(Intent.PROJECTS)
        if command == "/status":
            return Command(Intent.STATUS)
        if command == "/jobs":
            return Command(Intent.JOBS)
        if command == "/hosts":
            return Command(Intent.HOSTS)
        if command == "/doctor":
            return Command(Intent.DOCTOR)
        if command == "/sessions":
            if args:
                raise CommandParseError("usage: /sessions")
            return Command(Intent.SESSIONS)
        if command == "/session":
            if not args:
                return Command(Intent.SESSION)
            if len(args) == 1 and args[0].casefold() == "new":
                return Command(Intent.NEW_SESSION)
            if len(args) == 2 and args[0].casefold() == "use":
                return Command(Intent.USE_SESSION, job_id=args[1])
            if len(args) != 1:
                raise CommandParseError(
                    "usage: /session [new|use <session-id>|<session-id>]"
                )
            return Command(Intent.SESSION, job_id=args[0])
        if command == "/retry":
            if len(args) != 1:
                raise CommandParseError("usage: /retry <job-id>")
            return Command(Intent.RETRY, job_id=args[0])
        if command == "/stop":
            return Command(Intent.STOP, job_id=_optional_job(args, "/stop"))
        if command == "/pause":
            return Command(Intent.PAUSE, job_id=_optional_job(args, "/pause"))
        if command == "/resume":
            return Command(Intent.RESUME, job_id=_optional_job(args, "/resume"))
        if command == "/job":
            if len(args) != 1:
                raise CommandParseError("usage: /job <job-id>")
            return Command(Intent.JOB, job_id=args[0])
        if command in {"/steer", "/send"}:
            return self._parse_steer(args)
        if command == "/redirect":
            return self._parse_redirect(args)
        if command == "/run":
            if not args:
                raise CommandParseError("usage: /run <project> [--host <host-id>]")
            project_id = args[0]
            self.projects.get(project_id)
            host = "auto"
            index = 1
            while index < len(args):
                if args[index] == "--host" and index + 1 < len(args):
                    host = args[index + 1]
                    index += 2
                    continue
                raise CommandParseError(f"unsupported argument: {args[index]}")
            return Command(Intent.RUN_PROJECT, project_id=project_id, host=host)

        raise CommandParseError(f"unknown command: {command}")

    def _parse_steer(self, args: list[str]) -> Command:
        job_id: str | None = None
        remaining: list[str] = []
        index = 0
        while index < len(args):
            if args[index] == "--job":
                if index + 1 >= len(args):
                    raise CommandParseError("usage: /steer [--job <job-id>] <instruction>")
                job_id = args[index + 1]
                index += 2
                continue
            remaining.append(args[index])
            index += 1
        instruction = " ".join(remaining).strip()
        if not instruction:
            raise CommandParseError("usage: /steer [--job <job-id>] <instruction>")
        return Command(Intent.STEER, job_id=job_id, instruction=instruction)

    def _parse_redirect(self, args: list[str]) -> Command:
        job_id: str | None = None
        remaining: list[str] = []
        index = 0
        while index < len(args):
            if args[index] == "--job":
                if index + 1 >= len(args):
                    raise CommandParseError(
                        "usage: /redirect [--job <job-id>] <instruction>"
                    )
                job_id = args[index + 1]
                index += 2
                continue
            remaining.append(args[index])
            index += 1
        instruction = " ".join(remaining).strip()
        if not instruction:
            raise CommandParseError(
                "usage: /redirect [--job <job-id>] <instruction>"
            )
        return Command(
            Intent.REDIRECT,
            job_id=job_id,
            instruction=instruction,
        )

    def _parse_natural(self, text: str) -> Command:
        normalized = text.casefold().strip()
        if normalized in {"일시정지", "잠깐 멈춰", "pause"}:
            return Command(Intent.PAUSE)
        if normalized in {"재개", "다시 시작", "resume"}:
            return Command(Intent.RESUME)

        project = self.projects.resolve_name(text)
        run_words = ("진행", "계속", "다음 작업", "실행", "continue", "next", "run")
        if project is not None and any(word in normalized for word in run_words):
            host = "auto"
            for candidate in project.allowed_hosts:
                short_name = candidate.split("-", 1)[0]
                if candidate.casefold() in normalized or short_name.casefold() in normalized:
                    host = candidate
                    break
            return Command(Intent.RUN_PROJECT, project_id=project.id, host=host)

        return Command(Intent.STEER, instruction=text)


def _optional_job(args: list[str], command: str) -> str | None:
    if len(args) > 1:
        raise CommandParseError(f"usage: {command} [job-id]")
    return args[0] if args else None
