# Contributing

Thanks for considering it. This is a small library with a deliberately narrow
scope, so contributions that stay inside that scope are much more likely to be
useful.

## Scope

chunkforge is a dependency-free, pure-Python implementation of content-defined
chunking with deduplication, integrity checking, and resumable ingest. See
[docs/PROJECT_SPEC.md](docs/PROJECT_SPEC.md) for the goals and the explicit
non-goals.

Out of scope, and unlikely to be accepted: encryption, compression, remote
backends, a multi-writer locking protocol, or a scheduling daemon. If you want
those, `borg`, `restic`, or `rclone` are better tools and already exist.

In scope and welcome: correctness fixes, performance work on the chunking hot
loop, better failure messages, more tests, and documentation improvements.

## Development

No dependencies to install, at runtime or for tests.

```bash
git clone https://github.com/Wsh7Ash/chunkforge
cd chunkforge

python run_tests.py              # the whole suite
python run_tests.py -v           # verbose
python run_tests.py forge        # one module
python demo.py                   # regenerate results/
python demo.py --quick           # faster, smaller inputs
```

To use the CLI from a checkout without installing:

```bash
export PYTHONPATH=src
python -m chunkforge --help
```

Requires Python 3.9 or later. CI runs 3.9 through 3.13.

## Making a change

1. **Open an issue first** for anything beyond a small fix, so nobody spends a
   weekend on something that will not land.
2. **Keep the standard library restriction.** Not "prefer" — the guarantee is
   part of the product. A dependency-free tool is the reason someone can still
   restore a file after their environment breaks.
3. **Add a test that fails before the change.** For a bug fix, a test that
   reproduces the bug. If you cannot write one, that is worth understanding
   before continuing.
4. **Keep the suite fast.** It runs in about two and a half minutes. Most of that
   is the chunker and the CLI subprocess tests. A test that takes seconds is
   fine; one that takes a minute is not.
5. **Update the docs you invalidate.** A behaviour change means README,
   `USAGE.md`, `CHANGELOG.md`, and possibly `ARCHITECTURE.md`.
6. **Regenerate `results/`** if you change chunking, and say so in the pull
   request. A behaviour change that quietly invalidates the published numbers is
   worse than no change.

## Code style

Match the surrounding code. Specifically:

- Type hints on public functions. `from __future__ import annotations` is already
  in every module.
- Docstrings explain *why*, not what the next line does. If a design decision is
  non-obvious, say what the alternative was and why it lost. The existing
  modules are the reference; `forge.py` in particular documents its trade-offs
  inline.
- Comments earn their place by explaining something the code cannot. Delete
  comments that merely restate the code.
- No clever code. This is meant to be read by someone debugging their archive.
- Standard library only.

## Commit messages

One logical change per commit, in the imperative:

```
Refuse a resume when the chunker config changed

Resuming under a different config produced a manifest whose early chunks
came from the first config and whose later ones came from the second, while
claiming a single config. That manifest cannot be reproduced, so it is now
refused.
```

Explain what the change does and why it was needed. The diff already shows what
changed.

## Pull requests

- One topic per pull request.
- CI green: the suite on 3.9–3.13, and the demo.
- Say which Python versions you tested on locally.
- If you changed chunking, include the before/after figures from `demo.py`.

## Reporting bugs

Please include:

- what you did, and what you expected;
- what happened instead;
- the smallest input that reproduces it — ideally under a kilobyte, or a script
  that generates one;
- your Python version and platform.

A `demo.py`-style script using a fixed seed is ideal. Deduplication bugs are
much easier to diagnose with a deterministic input.

## Security

Do not open a public issue for a security problem. Use GitHub's private
vulnerability reporting for this repository.

Note that chunkforge does not encrypt. If that matters, put the archive on an
encrypted filesystem.
