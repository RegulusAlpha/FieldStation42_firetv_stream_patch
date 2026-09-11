import os

from fastapi import APIRouter, HTTPException, status

from fs42.station_manager import StationManager

router = APIRouter(prefix="/content", tags=["content"])


def _resolve_and_check(content_dir: str, rel_path: str) -> str:
    """
    Resolve rel_path against content_dir and make sure the result is still
    logically inside content_dir. Uses abspath (lexical "." / ".." collapsing
    only) rather than realpath deliberately: show folders are commonly
    symlinked in from entirely different real locations (e.g.
    catalog/Anime/G-Anime/Dragon Ball -> /tank/.../Dragon Ball), which is the
    whole point of this browser, so resolving symlinks before the containment
    check would reject those legitimate paths. This still blocks a rel_path
    that tries to walk out of content_dir with ".." segments; it does not
    (and, given symlinks are meant to point anywhere, cannot) stop a symlink
    placed inside content_dir from itself pointing somewhere unexpected - the
    web console already has no auth and lets content_dir/standby_image/etc.
    be set to any path directly, so that's an existing trust boundary, not
    one this endpoint introduces.
    """
    content_root = os.path.abspath(content_dir)
    candidate = os.path.abspath(os.path.join(content_dir, rel_path)) if rel_path else content_root

    if candidate != content_root and not candidate.startswith(content_root + os.sep):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid path")

    return candidate


@router.get("/{network_name}/tree")
async def get_content_tree(network_name: str, path: str = "", content_dir: str = None):
    """
    Lazily list the immediate subdirectories of a content_dir (or a
    subdirectory of it, via `path`). Used by the schedule editor to let a user
    pick a specific show folder (e.g. "G-Anime/Dragon Ball") as its own tag,
    instead of only ever being able to pick the top-level tag folder.

    `content_dir`, when given, is used as-is instead of looking the station up
    by name - this is what lets the folder browser work while creating a new
    station (before it has been saved once, so it doesn't exist in
    StationManager yet) and reflects in-progress, unsaved edits to
    content_dir made in the Form tab. When omitted, falls back to the
    persisted station's own content_dir, which still lets this endpoint be
    called directly (e.g. from a script) with just a station name.

    Does not touch the media catalog - this is a plain, fast directory
    listing, not a scan for playable media.
    """
    if not content_dir:
        station = StationManager().station_by_name(network_name)
        if station is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Station '{network_name}' not found")
        content_dir = station.get("content_dir")

    if not content_dir:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Station '{network_name}' has no content_dir configured")

    if not os.path.isdir(content_dir):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"content_dir does not exist: {content_dir}")

    rel_path = (path or "").strip("/")
    target_dir = _resolve_and_check(content_dir, rel_path)

    if not os.path.isdir(target_dir):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Path not found: {rel_path}")

    entries = []
    try:
        with os.scandir(target_dir) as scan:
            for entry in scan:
                if not entry.is_dir(follow_symlinks=True):
                    continue
                if entry.name.startswith("."):
                    continue

                child_tag_path = f"{rel_path}/{entry.name}" if rel_path else entry.name
                has_children = False
                try:
                    with os.scandir(entry.path) as child_scan:
                        has_children = any(child.is_dir(follow_symlinks=True) and not child.name.startswith(".") for child in child_scan)
                except OSError:
                    has_children = False

                entries.append({
                    "name": entry.name,
                    "tag_path": child_tag_path,
                    "has_children": has_children,
                })
    except OSError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    entries.sort(key=lambda e: e["name"].lower())

    return {
        "network_name": network_name,
        "content_dir": content_dir,
        "path": rel_path,
        "entries": entries,
    }
