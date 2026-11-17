import hashlib
import re
from pathlib import PurePosixPath

from bed.core.models import Candidate


def normalize_path(path: str) -> str:
    normalized = PurePosixPath(path.replace("\\", "/")).as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def fingerprint(candidate: Candidate) -> str:
    path = normalize_path(candidate.files[0]) if candidate.files else ""
    symbol = candidate.symbol.strip()
    kind = re.sub(r"[^a-z0-9]+", "-", candidate.issue_kind.lower()).strip("-")
    raw = f"{candidate.category}|{path}|{symbol}|{kind}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
