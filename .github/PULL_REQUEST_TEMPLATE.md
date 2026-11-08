## What and why

<!-- What does this change, and why is it needed? Link the issue if there is one. -->

## How it was tested

- [ ] `pytest` passes
- [ ] `ruff check .` is clean
- [ ] New behaviour has tests

## Checklist

- [ ] Verdicts stay deterministic (no LLM in `app/verification`)
- [ ] Missing evidence still leads to INCONCLUSIVE, never to FAILED or VERIFIED
- [ ] Docs updated if behaviour or settings changed
- [ ] No secrets, `.env`, `data/` or real data committed
