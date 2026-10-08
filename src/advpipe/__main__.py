"""`python -m advpipe`: the same CLI as the `advpipe` command (used by `run --detach`)."""

from advpipe.cli import app

app(prog_name="advpipe")
