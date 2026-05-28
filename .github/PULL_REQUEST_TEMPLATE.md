## Summary

## Verification

- [ ] `uv run ruff format --check .`
- [ ] `uv run ruff check .`
- [ ] `uv run pyright`
- [ ] `uv run pytest -q`
- [ ] `cd analyzers/typescript && npm run build`
- [ ] `uv build --sdist --wheel && uv run twine check dist/*`
- [ ] `git diff --check`

## Notes

List breaking changes, migration steps, or skipped checks here.
