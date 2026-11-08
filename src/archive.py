"""Archive step: move landing page files to archived/."""
from .config import resolve_path
from .errors import FileCollisionError
from .ingest import LANDING_RE, _scan_landing, move_request_files


def archive_landing(cfg, indicator_code):
    landing = resolve_path(cfg, "landing", indicator_code)
    stray = sorted(p for p in landing.glob("*.json")
                   if not (LANDING_RE.match(p.name) and LANDING_RE.match(p.name).group("I") == indicator_code)) \
        if landing.is_dir() else []
    if stray:
        raise FileCollisionError(f"unrecognised files in landing, nothing archived: {[str(p) for p in stray]}")
    moved = []
    rids = sorted({rid for g in _scan_landing(cfg, indicator_code).values() for rid in g})
    # check every target up front so that nothing moves if any collides
    dest = resolve_path(cfg, "archived", indicator_code)
    for g in _scan_landing(cfg, indicator_code).values():
        for paths in g.values():
            for p in paths:
                if (dest / p.name).exists():
                    raise FileCollisionError(f"archive target already exists: {dest / p.name}")
    for rid in rids:
        moved.extend(move_request_files(cfg, indicator_code, rid, "archived"))
    left = [p for p in resolve_path(cfg, "landing", indicator_code).glob("*.json")] \
        if resolve_path(cfg, "landing", indicator_code).is_dir() else []
    if left:
        raise FileCollisionError(f"landing not empty after archive: {[str(p) for p in left]}")
    return moved
