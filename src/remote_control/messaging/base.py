from __future__ import annotations

from abc import ABC, abstractmethod

from remote_control.approvals.registry import ApprovalPrompt


class MessagingProvider(ABC):
    @abstractmethod
    async def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def send_message(self, user_id: str, text: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def send_approval(self, user_id: str, approval: ApprovalPrompt) -> None:
        raise NotImplementedError
