"""Where Gaff reads and writes on disk.

One resolver for every on-disk location, so a shared install never writes
inside its own package directory and never depends on the caller's working
directory.

Two tiers
---------
* **Shipped warm cache** — read-only, travels with the package. Lets a new user
  answer questions about the seeded streets on first run, before they have
  fetched anything themselves.
* **User cache** — writable, per user. ``$GAFF_CACHE_DIR`` if set, else
  ``~/.gaff/cache``.

Reads check the user cache first and fall back to the shipped one. Writes
*always* land in the user cache. That ordering means a user's own freshly
fetched data shadows the shipped copy, and an install on a read-only or
system-owned path still works.

Set ``GAFF_CACHE_DIR`` to the repo's own ``data/`` to keep the development
behaviour of growing the in-repo warm cache::

    export GAFF_CACHE_DIR="$PWD/data"

Everything here is pure path resolution: no network, no imports from the rest
of the package, and nothing is created until something is actually written.
"""

from __future__ import annotations

import os
import subprocess
from typing import List, Optional

# The data kinds that live in the two-tier cache.
KINDS = ("comps", "epc", "hpi", "flips")

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PACKAGE_DIR)

#: Env var names, collected so they are greppable and documented in one place.
ENV_CACHE_DIR = "GAFF_CACHE_DIR"
ENV_DATA_DIR = "GAFF_DATA_DIR"
ENV_EPC_TOKEN = "GAFF_EPC_TOKEN"

#: Keychain service name used on macOS for the EPC token.
KEYCHAIN_SERVICE = "gaff-epc-token"


def _env(name: str) -> Optional[str]:
    """Return a non-empty environment value, else ``None``.

    An empty or whitespace-only variable is treated as unset. Exporting
    ``GAFF_CACHE_DIR=""`` should mean "I did not configure this", not "resolve
    every path against the filesystem root".
    """
    value = (os.environ.get(name) or "").strip()
    return value or None


# ---------------------------------------------------------------------------
# The two tiers.
# ---------------------------------------------------------------------------

def shipped_data_dir() -> str:
    """The read-only warm cache that travels with the package.

    Prefers ``gaff_engine/data`` (how the data sits once packaged) and falls
    back to ``<repo>/data`` (how it sits in a development checkout).
    """
    override = _env(ENV_DATA_DIR)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    packaged = os.path.join(_PACKAGE_DIR, "data")
    if os.path.isdir(packaged):
        return packaged
    return os.path.join(_REPO_ROOT, "data")


def user_cache_dir() -> str:
    """The writable per-user cache root. Not created until something writes."""
    override = _env(ENV_CACHE_DIR)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".gaff", "cache")


def shipped_dir(kind: str) -> str:
    """Read-only directory for one data ``kind`` (``comps`` / ``epc`` / ``hpi``)."""
    return os.path.join(shipped_data_dir(), kind)


def cache_dir(kind: str) -> str:
    """Writable directory for one data ``kind``. Not created until written to."""
    return os.path.join(user_cache_dir(), kind)


# ---------------------------------------------------------------------------
# Reading and writing.
# ---------------------------------------------------------------------------

def read_candidates(kind: str, *parts: str) -> List[str]:
    """Every path a read should try, best first: user cache, then shipped.

    Returned whether or not they exist, so a caller can report what it looked
    for when nothing is found.
    """
    rel = os.path.join(*parts) if parts else ""
    roots = [cache_dir(kind), shipped_dir(kind)]
    seen, out = set(), []
    for root in roots:                      # de-dup: the two tiers can coincide
        path = os.path.join(root, rel) if rel else root
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def read_path(kind: str, *parts: str) -> Optional[str]:
    """First existing path for ``parts`` across the two tiers, else ``None``."""
    for path in read_candidates(kind, *parts):
        if os.path.exists(path):
            return path
    return None


def write_path(kind: str, *parts: str) -> str:
    """Path to write ``parts`` into the user cache, parent directories created.

    Always the user cache: the shipped tier is treated as read-only even when
    it happens to be writable, so an install never mutates its own package.
    """
    path = os.path.join(cache_dir(kind), *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def data_candidates(*parts: str) -> List[str]:
    """Paths to try for a loose data file that sits at a data root, not under a
    kind — ``comps_enriched.json``, ``round1_scores.json``, ``profile.json``.

    Same precedence as :func:`read_candidates`: user cache first, then shipped.
    """
    rel = os.path.join(*parts)
    seen, out = set(), []
    for root in (user_cache_dir(), shipped_data_dir()):
        path = os.path.join(root, rel)
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def data_file(*parts: str) -> Optional[str]:
    """First existing loose data file for ``parts``, else ``None``."""
    for path in data_candidates(*parts):
        if os.path.exists(path):
            return path
    return None


# ---------------------------------------------------------------------------
# Secrets.
# ---------------------------------------------------------------------------

def _keychain_token() -> Optional[str]:
    """Read the EPC token from the macOS keychain, or ``None`` if unavailable.

    Absent tool, absent entry, non-macOS host and a hanging ``security`` call
    are all just "no token here" — the caller falls through to the next source.
    """
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def epc_token_sources() -> List[str]:
    """Human-readable description of each token source, in resolution order.

    Used to build the error message. Deliberately describes *where* the token
    would come from and never any value.
    """
    return [
        "the %s environment variable" % ENV_EPC_TOKEN,
        'the macOS keychain (service "%s")' % KEYCHAIN_SERVICE,
        "~/.gaff/epc_token",
        os.path.join(_REPO_ROOT, ".secrets", "epc_token"),
    ]


def epc_token() -> str:
    """Return the EPC API Bearer token from the first source that supplies one.

    Order: ``$GAFF_EPC_TOKEN``, the macOS keychain, ``~/.gaff/epc_token``, then
    the development ``.secrets/epc_token``. The value is returned and nothing
    else: it is never logged, printed, or written to disk by this module.

    Raises ``RuntimeError`` naming every source that was tried (never a value)
    when none supplies a token.
    """
    from_env = _env(ENV_EPC_TOKEN)
    if from_env:
        return from_env

    from_keychain = _keychain_token()
    if from_keychain:
        return from_keychain

    for path in (os.path.join(os.path.expanduser("~"), ".gaff", "epc_token"),
                 os.path.join(_REPO_ROOT, ".secrets", "epc_token")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                token = fh.read().strip()
        except (OSError, UnicodeDecodeError):
            continue
        if token:
            return token

    raise RuntimeError(
        "EPC API token not found. Tried, in order:\n"
        + "\n".join("  - %s" % s for s in epc_token_sources())
        + "\n\nSet one, for example:\n"
          "    export %s='YOUR_TOKEN'\n"
          "or store it in the keychain (macOS):\n"
          "    security add-generic-password -s %s -a \"$USER\" -w 'YOUR_TOKEN'\n"
          "Request a token at https://epc.opendatacommunities.org/."
        % (ENV_EPC_TOKEN, KEYCHAIN_SERVICE)
    )


__all__ = [
    "KINDS", "ENV_CACHE_DIR", "ENV_DATA_DIR", "ENV_EPC_TOKEN", "KEYCHAIN_SERVICE",
    "shipped_data_dir", "user_cache_dir", "shipped_dir", "cache_dir",
    "read_candidates", "read_path", "write_path", "data_candidates", "data_file", "epc_token", "epc_token_sources",
]
