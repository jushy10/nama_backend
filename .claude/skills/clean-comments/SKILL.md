---
name: clean-comments
description: Clean up code comments — shrink massive block comments and docstrings into concise single-line comments, keep only comments that carry real signal (constraints, non-obvious why, gotchas), and delete narration and noise. Use whenever the user asks to clean, trim, prune, shrink, or simplify comments, complains that comments are too long or noisy, or asks to make a file's comments readable. Also use after large refactors when the user asks to tidy the comments left behind.
---

# Clean Comments

Prune a file's comments down to the ones a future reader — human or AI agent —
actually needs. The test for every comment: **does this tell the reader something
the code cannot?** If not, it dies.

## Why this matters

Comments compete with code for attention. A 15-line block comment above a
function gets skimmed or skipped; a single sharp line gets read. Long comments
also rot: they describe the code as it was when written, drift from the truth,
and then actively mislead. AI agents reading the file burn context on prose that
restates the code. Short, high-signal comments serve everyone; essays serve no one.

## What to KEEP (and usually shorten to one line)

- **Constraints the code can't show** — rate limits, vendor quirks, ordering
  requirements, thread-safety notes. `# Yahoo IP-gates this endpoint; fetch is best-effort.`
- **Non-obvious why** — a decision that looks wrong or arbitrary without context.
  `# bool is an int in Python — check it first or "limit": true becomes 1.`
- **Contracts with other files** — invariants a change here would silently break
  elsewhere. `# Field names mirror SearchStocks.execute kwargs so run() can splat asdict().`
- **Real warnings** — security notes, "do not reorder", known footguns.
- **TODOs that are actionable** — keep only if specific; delete vague ones.

## What to DELETE

- **Narration** — comments saying what the next line does. `# loop over the results`
- **Restating the name** — `# The user repository` above `class UserRepository`.
- **History and justification** — "this replaced X", "we used to do Y", "this is
  correct because..." — that's PR-description material, not code.
- **Section banners** that separate two things — unless the file is very long.
- **Commented-out code** — delete it; git remembers.

## How to shrink what stays

- Prefer a **single `#` line** directly above (or beside) the code it describes.
- A multi-paragraph comment almost always contains one load-bearing sentence —
  find it, sharpen it, delete the rest.
- **Docstrings**: compress to 1–3 lines. Keep the one-sentence purpose; keep
  args/returns/raises notes **only when non-obvious** from names and types.
  A docstring that restates the signature is noise.
- Keep the codebase's comment idiom (marker style, capitalization, punctuation).

## Rules of engagement

- **Never change code behavior.** Comments and docstrings only — no renames, no
  reformatting of code lines, no import changes (except a docstring-only module).
- When a long comment holds a genuinely important constraint, do not delete the
  constraint — shrink the prose around it.
- If unsure whether a comment is load-bearing, keep a one-line version. Deleting
  a true constraint costs far more than one surviving line.
- After editing, run the project's test suite if one exists; comment edits should
  never break it — a failure means code was touched by mistake.

## Scope

- If the user names files or a directory, clean those.
- Otherwise default to the files changed on the current branch (`git diff` +
  `git diff main...HEAD --name-only`), not the whole repo.
- Report at the end: files touched, roughly how many lines of comments removed,
  and any comment you deliberately kept long (with why).

## Examples

**Narration + history → one constraint line:**

```python
# Before
# Imported here, not at module load: the SDK is an optional heavyweight dependency (it
# pulls boto3) that neither the app's other endpoints nor the offline tests need. A
# missing extra raises ImportError, which the wiring turns into a 503.
from anthropic import AnthropicBedrock

# After
# Lazy import: boto3 is heavy and optional; ImportError -> 503 in the wiring.
from anthropic import AnthropicBedrock
```

**Docstring compression:**

```python
# Before
def get(self, name: str) -> AgentRecipe | None:
    """Return the stored recipe for ``name``.

    Looks the row up by its unique name column and maps it onto the frozen
    AgentRecipe entity. If no row with that name exists in the table, returns
    None so the caller can decide how to handle the missing configuration.
    """

# After
def get(self, name: str) -> AgentRecipe | None:
    """Return the stored recipe for ``name``, or None when none is configured."""
```

**Delete entirely (restates code):**

```python
# Before
# Build the header string with the total count and the number shown
header = f"{page.total} match(es); showing {len(page.results)}:"

# After
header = f"{page.total} match(es); showing {len(page.results)}:"
```
