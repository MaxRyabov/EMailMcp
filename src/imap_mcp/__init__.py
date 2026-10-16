"""Read-only multi-account IMAP MCP server."""


def main(argv: list[str] | None = None) -> int:
    # Imported on call: the attachment worker (_extract_worker.py) is a module of
    # this package, and importing the package must not pull in the IMAP layer.
    from .cli import main as run

    return run(argv)


__all__ = ["main"]
