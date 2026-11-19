"""Console output and safe diagnostics for the journal and notifications."""

import sys


class DiagnosticError(RuntimeError):
    """An operational error whose message is safe to report."""


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, DiagnosticError):
        return str(exc)
    # Unexpected errors may carry external output, URLs, or other private data.
    return type(exc).__name__


def log(message: str) -> None:
    print(f"\n\033[1;34m==>\033[0m {message}", flush=True)


def warning(message: str) -> None:
    print(f"\n\033[1;33mWARNING:\033[0m {message}", flush=True)


def error(message: str) -> None:
    print(f"\n\033[1;31mERROR:\033[0m {message}", file=sys.stderr, flush=True)
