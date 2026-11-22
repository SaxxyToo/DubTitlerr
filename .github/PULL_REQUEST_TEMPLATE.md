## What

<!-- One paragraph: what this change does, in the reader's terms. -->

## Why

<!-- The problem or need. Link the issue if one exists. -->

## How

<!-- The approach, and any decision a reviewer would want to question. -->

## Testing

<!-- What was run, and what proved it works. "Tests pass" needs the command and its result. -->

## Verified

- [ ] `scripts/check.sh` green (lint + full suite); `--dash` if a shell loop changed
- [ ] `scripts/check.sh --cov` reviewed if the change adds branches worth covering
- [ ] Web checks green (`npm run format:check && npm run check && npm test && npm run build`) if `web/` changed
- [ ] Browser suite green if the review UI changed
- [ ] Docs updated (`README.md` and the matching `docs/wiki/` page) if behaviour or configuration changed
- [ ] No secrets, media, generated artifacts or `graphify-out/` in the diff

## Notes for the reviewer

<!-- Anything that saves the reviewer time: where to start, what to skip, known trade-offs. -->
