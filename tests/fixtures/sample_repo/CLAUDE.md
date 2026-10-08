# mathutils

Small numeric helpers.

- Gates: `pytest -q`, `mypy .`, `ruff check .`
- Every public function is fully type-annotated and has a one-line docstring.
- Public functions are re-exported from `mathutils/__init__.py` and listed in `__all__`.
- Invalid arguments raise `ValueError` with a message naming the argument.
