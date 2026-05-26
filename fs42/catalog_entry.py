import os
import json

from fs42 import schedule_hint


class MatchingContentNotFound(Exception):
    pass


class NoFillerContentFound(Exception):
    pass


class CatalogEntry:
    # CatalogEntry(row[2], row[3], float(row[4]), json.loads(row[6]) if row[6] else [])
    def __init__(self, path, duration, tag, hints=[], count=0, content_type="feature", media_type="video"):
        self.path = path
        self.realpath = None

        self.duration = duration
        self.tag = tag

        # Guide/schedule title.
        #
        # For folder layouts like:
        #   <tag folder>/<show folder>/s01e01...
        #
        # This produces:
        #   <show folder> - <video filename>
        #
        # Example:
        #   am-shows/Dragon Ball/S01E01.mkv
        # becomes:
        #   Dragon Ball - S01E01
        self.title = self._make_folder_episode_title(path)

        self.count = count
        self.hints = hints
        self.content_type = content_type
        self.media_type = media_type
        self.station = None
        self.dbid = None
        self.created_at = None
        self.updated_at = None

    @staticmethod
    def _clean_title_part(value):
        """
        Make folder/file names look nicer in guide titles.
        """
        if not value:
            return ""

        value = os.path.splitext(os.path.basename(str(value)))[0]
        value = value.replace("_", " ")
        value = value.replace(".", " ")
        value = value.replace("-", " ")
        value = " ".join(value.split())
        return value.strip()

    @classmethod
    def _make_folder_episode_title(cls, path):
        """
        Build a title from the immediate parent folder and the file name.

        Target folder layout:

            <tag folder>/<show folder>/<episode file>

        Example:

            catalog/Anime/am-shows/Dragon Ball/S01E01.mkv

        becomes:

            Dragon Ball - S01E01

        This intentionally ignores the tag value and simply uses the direct
        parent folder of the video file as the show name.
        """
        file_title = cls._clean_title_part(path)

        try:
            norm_path = os.path.normpath(str(path))
            parts = norm_path.split(os.sep)

            # Need at least:
            #   parent/file
            if len(parts) < 2:
                return file_title

            show_folder = cls._clean_title_part(parts[-2])

            if not show_folder:
                return file_title

            # Avoid duplicates like:
            #   Dragon Ball - Dragon Ball
            if show_folder.lower() == file_title.lower():
                return file_title

            return f"{show_folder} - {file_title}"

        except Exception:
            return file_title

    def __str__(self):
        hints = list(map(str, self.hints))
        return f"{self.title:<20.20} | {self.tag:<10.10} | {self.duration:<8.1f} | {hints} | {self.path}"

    def toJSON(self):
        # Convert the entry to a JSON serializable dictionary
        return {
            "dbid": self.dbid,
            "path": self.path,
            "title": self.title,
            "duration": self.duration,
            "tag": self.tag,
            "count": self.count,
            "content_type": self.content_type,
            "media_type": self.media_type,
            "hints": [hint.toJSON() for hint in self.hints],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @staticmethod
    def from_json_dict(json_data):
        # Create an entry from a JSON serializable dictionary
        tup = (
            json_data["dbid"],
            json_data["station"],
            json_data["path"],
            json_data["title"],
            json_data["duration"],
            json_data["tag"],
            json_data["count"],
            json_data["hints"],
            json_data.get("created_at", None),
            json_data.get("updated_at", None),
            json_data.get("realpath", None),
            json_data.get("content_type", "feature"),
            json_data.get("media_type", "video"),
        )
        return CatalogEntry.from_db_row(tup)

    @staticmethod
    def from_db_row(row):
        if len(row) == 13:
            # New schema with realpath, content_type, and media_type
            (
                dbid,
                station,
                path,
                title,
                duration,
                tag,
                count,
                hints_str,
                created,
                updated,
                realpath,
                content_type,
                media_type,
            ) = row
        elif len(row) == 12:
            # Schema with realpath and content_type but no media_type
            (
                dbid,
                station,
                path,
                title,
                duration,
                tag,
                count,
                hints_str,
                created,
                updated,
                realpath,
                content_type,
            ) = row
            media_type = "video"
        elif len(row) == 11:
            # Schema with realpath but no content_type or media_type
            (
                dbid,
                station,
                path,
                title,
                duration,
                tag,
                count,
                hints_str,
                created,
                updated,
                realpath,
            ) = row
            content_type = "feature"
            media_type = "video"
        else:
            # Old schema without realpath
            (
                dbid,
                station,
                path,
                title,
                duration,
                tag,
                count,
                hints_str,
                created,
                updated,
            ) = row
            realpath = None
            content_type = "feature"
            media_type = "video"

        entry = CatalogEntry(path, duration, tag, None, count, content_type, media_type)

        entry.realpath = realpath
        entry.count = count
        entry.dbid = dbid
        entry.station = station
        entry.created_at = created
        entry.updated_at = updated

        # Important:
        #
        # Ignore the DB title and rebuild from path every time.
        # This lets old catalog rows display the new:
        #   Show Folder - Filename
        # format after reload.
        entry.title = CatalogEntry._make_folder_episode_title(path)

        hints = []

        # Load hints from JSON
        if hints_str:
            try:
                loaded_hints = json.loads(hints_str)

                if not isinstance(loaded_hints, list):
                    loaded_hints = []

                for hint_str in loaded_hints:
                    hint = json.loads(hint_str)

                    if isinstance(hint, dict) and "type" in hint:
                        if hint["type"] == "day_part":
                            hints.append(schedule_hint.DayPartHint(hint["part"]))
                        elif hint["type"] == "bump":
                            hints.append(schedule_hint.BumpHint(hint["where"]))
                        elif hint["type"] == "range":
                            hints.append(schedule_hint.RangeHint(hint["range_string"]))
                        elif hint["type"] == "quarter":
                            hints.append(schedule_hint.QuarterHint(hint["quarter"]))
                        elif hint["type"] == "month":
                            hints.append(schedule_hint.MonthHint(hint["month"]))
                        elif hint["type"] == "day_of_week":
                            hints.append(schedule_hint.DayofWeekHint(hint["day"]))
                        else:
                            print(f"Warning: Unknown hint type {hint['type']}. Skipping.")
                    else:
                        print(f"Warning: Invalid hint format {hint}. Skipping.")

            except (json.JSONDecodeError, TypeError) as e:
                print(f"Warning: Failed to decode hints from string '{hints_str}'. Using empty hints list.")
                print(f"Error: {e}")
                hints = []

        entry.hints = hints
        return entry