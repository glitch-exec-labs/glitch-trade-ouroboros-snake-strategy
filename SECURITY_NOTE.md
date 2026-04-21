# SECURITY NOTE

**Last updated:** 2026-04-21

## Leaked Brave Search API key — dead, no action needed

A `BRAVE_API_KEY` literal was previously hardcoded in `mt5/bots/news_guard.py`
and committed to this public repo. It was removed from HEAD in commit
`69623cd` (2026-04-17) and the Brave Search account itself has since been
**suspended** — the key is a dead credential.

| Secret | Where it lived | Current status |
|---|---|---|
| Brave Search `BRAVE_API_KEY` | `mt5/bots/news_guard.py` (pre-`69623cd`) | **Dead** — Brave account suspended. Key literal remains in git history but is unusable. |

## Why we did NOT rewrite history

`git filter-repo` + force-push would scrub the key literal from historical
commits, but:

- The credential is already dead, so the literal has no value to anyone who
  finds it.
- Force-pushing breaks every existing clone and fork of a public repo — the
  SHAs change, bookmarked commits stop resolving, and downstream consumers
  must re-clone.
- GitHub caches historical objects for up to 90 days regardless.

Rotation/revocation is what actually closes an API-key leak. In this case
the account was suspended, so the leak is closed. Rewriting history would
be cosmetic at the cost of real disruption.

## If the Brave integration is ever revived

1. Create a fresh Brave Search account and key at
   <https://api.search.brave.com/app/keys>.
2. Put the new key in a `.env` file outside the repo (`.env` is gitignored).
3. Set `BRAVE_API_KEY` in the environment that runs `news_guard.py`.
4. Do NOT copy any value from git history into a live configuration.

## General secret hygiene in this repo

- `.env` files are gitignored and have never been committed.
- `ctrader/ensemble/models/*.params.json` is gitignored — production model
  tuning lives only on the deployment server. Only `*.params.example.json`
  (neutral demo values) is tracked.
- OAuth access tokens and broker credentials live in runtime `.env` only;
  no credential has ever been committed besides the now-dead Brave key.

Responsible-disclosure contact: `support@glitchexecutor.com`.
