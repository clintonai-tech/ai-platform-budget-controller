"""Command-line interface for the budget controller."""

import typer

app = typer.Typer(
    help="Evaluate team LLM spend against tiered budget policy.",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """Budget controller CLI entry point."""
