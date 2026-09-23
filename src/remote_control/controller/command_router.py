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
    HOSTS = "HOSTS"
    HELP = "HELP"


@dataclass(slots=True)
class Command:
    intent: Intent
    project_id: str | None = None
    host: str = "auto"
    job_id: str | None = None


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
        if command == "/stop":
            return Command(Intent.STOP)
        if command == "/job":
            if len(args) != 1:
                raise CommandParseError("usage: /job <job-id>")
            return Command(Intent.JOB, job_id=args[0])
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

    def _parse_natural(self, text: str) -> Command:
        project = self.projects.resolve_name(text)
        normalized = text.casefold()
        run_words = ("진행", "계속", "다음 작업", "실행", "continue", "next", "run")
        if project is not None and any(word in normalized for word in run_words):
            host = "auto"
            for candidate in project.allowed_hosts:
                short_name = candidate.split("-", 1)[0]
                if candidate.casefold() in normalized or short_name.casefold() in normalized:
                    host = candidate
                    break
            return Command(Intent.RUN_PROJECT, project_id=project.id, host=host)

        raise CommandParseError(
            "자연어 명령을 안전하게 해석하지 못했습니다. /run <project> 형식을 사용해 주세요."
        )
