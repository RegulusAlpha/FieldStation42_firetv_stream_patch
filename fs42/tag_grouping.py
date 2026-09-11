import os


def expand_collection_tags(content_dir, tags):
    """
    Given a tag or list of tags (each relative to content_dir, e.g. "G-Anime"
    or "G-Anime/Dragon Ball"), expand any tag that is a folder containing
    multiple subfolders into one tag per subfolder.

    Example: "G-Anime" holding "Dragon Ball", "konosuba", "Pokemon", etc. as
    separate show folders expands to ["G-Anime/Dragon Ball",
    "G-Anime/konosuba", "G-Anime/Pokemon", ...]. A tag that is already a
    single show's own folder (zero or one subfolder, or episode files sitting
    directly in it - e.g. "G-Anime/Dragon Ball" itself, or "R-Anime/Black
    Lagoon") is returned unchanged, since there is nothing to split it into.

    This is what lets a slot configured with just a top-level tag like
    "G-Anime" (plus random_tags) behave the same as if every show under it
    had been listed out individually. It is called from both
    slot_reader.get_tag_from_slot (runtime tag selection) and
    sequence_api._scan_sequence_slot (sequence pre-building), so a slot's
    effective tag set is always identical between the two - the sequence
    each pick advances is always for the specific show that was picked, not
    the combined folder.
    """
    if isinstance(tags, str):
        tags = [tags]

    expanded = []
    for tag in tags:
        expanded.extend(_expand_one(content_dir, tag))
    return expanded


def _expand_one(content_dir, tag):
    tag_dir = tag if os.path.isabs(tag) else os.path.join(content_dir, tag)

    try:
        subfolders = sorted(
            entry.name for entry in os.scandir(tag_dir)
            if entry.is_dir(follow_symlinks=True) and not entry.name.startswith(".")
        )
    except OSError:
        return [tag]

    if len(subfolders) < 2:
        return [tag]

    return [f"{tag}/{name}" for name in subfolders]
