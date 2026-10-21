"""Errors for the daily production pipeline."""


class DailyError(Exception):
    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind
