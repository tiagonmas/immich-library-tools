# Immich tools

Python command-line and web tools for auditing and maintaining an Immich
library. They find people and tags with comma or period names, review possible
duplicate people, inspect and optionally remove face assignments, and find
videos without people tags. Some tools can also hide, delete, or merge people
after confirmation. Most tools use Immich's API; the safe backup finder compares
local files and does not connect to Immich.

## Tools

| Script | What it does |
| --- | --- |
| `people_comma_finder.py` | Finds people whose names contain a comma, lists their associated images, reports the people tagged in each image, and checks whether each comma-separated name is also tagged. By default it previews removing the comma-named person's face from images. |
| `people_comma_list.py` | Lists people whose names contain a comma, with their IDs. It does not search images. |
| `tags_comma_list.py` | Lists comma-named tags and can replace those tag assignments on images with separate tags. |
| `people_dot_finder.py` | Finds people whose names contain a period, shows exact or likely similar names (80% similarity or higher), recommends the top match, and prompts before merging. A confirmed merge keeps the recommended person's record. |
| `people_similar_name_finder.py` | Lists people and possible duplicate names. For pairs scoring above 90%, it asks whether to merge; a merge happens only after an affirmative answer. |
| `image_folder_date_validator.py` | Finds images whose Immich timeline dates conflict with the year or date implied by their folder paths. |
| `tags_similar_name_finder.py` | Lists similar tag names, automatically merges exact dot/space or first-letter capitalization variants, and asks before other replacements above 90%. |
| `videos_without_people.py` | Lists videos with no people tagged, including a link to open each video in Immich. |
| `videos_without_people_web.py` | Runs a paginated web page with a thumbnail grid, hover video previews, durations, duration sorting (shortest first by default), and total count for videos that have no people tagged. It shows a loading indicator while counting. Each video link opens in a new tab. |
| `onedrive_safe_backup_finder.py` | Finds OneDrive `safeBackup` copies, compares them with matching originals and XMP sidecars, optionally shows metadata differences, reports image-content differences, and can delete confirmed backup copies. |

The comma finder can modify Immich data when run with `--no-dry-run`; it removes
the comma-named person's detected face entries from associated images. The dot
finder and similar-name finder can merge people after you confirm a suggested
merge. The tag list script can update image tags with `--replace`; it defaults
to dry-run mode and only applies changes with `--no-dry-run`.

## Setup

Immich API tools require Python 3.10 or newer and an Immich API key with
permission to read people, assets, and tags. Using tag replacement additionally
requires permissions to create tags, update tag assignments and assets, and
delete tags.
Set the Immich URL and API key in the environment:

```sh
export IMMICH_BASE_URL="http://localhost:2283"
export IMMICH_API_KEY="your-api-key"
```

`IMMICH_BASE_URL` may include `/api`; the scripts handle either form. Image and
video output uses direct Immich links. When the configured URL uses localhost,
the links use `192.168.1.200` so they can be opened from another device.
People-listing tools fetch every page from Immich, including hidden people.
The OneDrive safe backup finder does not need Immich credentials; it requires
ExifTool to compare metadata. It compares JPEG image streams without common
metadata segments. For pixel-level comparison of supported image files, install
and configure `ffmpeg`.

## Usage

### People with commas

Use these examples for common tasks such as listing comma-named people,
reviewing a small number of images, or previewing changes:

```sh
# List comma-named people and their IDs; do not inspect images.
python3 people_comma_finder.py --list-people

# Include a known person by ID if regular discovery missed it.
python3 people_comma_finder.py --list-people --include-person-id 5e5cc059-2027-452b-a90f-f662f3a53ad2

# Inspect one image. Dry-run is the default.
python3 people_comma_finder.py --top 1

# Inspect up to 10 images where all individual names are tagged.
python3 people_comma_finder.py --top 10 --tag-status yes

# Preview images where one or more individual names are missing.
python3 people_comma_finder.py --tag-status no

# Apply changes to the first two eligible images, asking before each change.
python3 people_comma_finder.py --top 2 --no-dry-run --confirm

# Apply changes without prompts.
python3 people_comma_finder.py --no-dry-run --no-confirm

# Use the separate read-only comma-name listing script.
python3 people_comma_list.py
```

The finder inspects all associated images by default. It logs each person,
image, Immich link, tagged people, and whether all individual names are tagged.
The tag check is informational; in live mode, the comma-named person's face is
removed from every selected associated image, including images missing one or
more individual-name tags. To find comma-named people omitted from the regular
people list, it searches similar names for each distinct named person and
deduplicates the returned people before filtering for commas. This requires one
name-search API request per distinct name and may take time for a large library.
For each comma-named result, it also reports the associated asset count; rows
with one or more assets are printed in red.

Options for `people_comma_finder.py`:

| Option | Behavior |
| --- | --- |
| `--top N` | Inspect at most `N` images total. By default, inspect all images. |
| `--list-people` | List comma-named people and exit without searching images. |
| `--include-person-id ID` | With `--list-people`, also fetch a known person by ID if Immich omits it from `/people`. Repeat for multiple IDs. |
| `--hide-people` | With `--list-people`, hide comma-named people with zero associated assets. Respects dry-run and confirmation settings. |
| `--delete-people` | With `--list-people`, delete all identified comma-named people and unassign their faces. Cannot be combined with `--hide-people`. Respects dry-run and confirmation settings. |
| `--tag-status yes\|no\|all` | Show images where all individual names are tagged (`yes`), where one or more are missing (`no`), or both (`all`, default). In live mode, comma-person face entries are removed from selected images whether or not every individual name is tagged. |
| `--dry-run` / `--no-dry-run` | Preview changes without updating Immich (default), or apply changes. |
| `--confirm` / `--no-confirm` | Ask before each image or person change (default), or apply live changes without prompts. Confirmation only applies with `--no-dry-run`. |

To apply hides or deletes, use `--no-dry-run`; the default dry-run mode only
reports planned changes. With the default confirmation setting, the script
asks before each eligible person change. Hiding is limited to people with zero
associated assets. Deleting removes the selected person record and unassigns its
faces, while leaving the assets in Immich. Review the output in dry-run mode
before using the delete option.

```sh
# Preview hiding comma-named people that have zero associated assets.
python3 people_comma_finder.py --list-people --hide-people

# Hide those zero-asset people, asking before each person.
python3 people_comma_finder.py --list-people --hide-people --no-dry-run

# Hide them without a prompt for each person.
python3 people_comma_finder.py --list-people --hide-people --no-dry-run --no-confirm

# Preview deleting all identified comma-named people, including people with assets.
python3 people_comma_finder.py --list-people --delete-people

# Delete all identified comma-named people, asking before each person.
python3 people_comma_finder.py --list-people --delete-people --no-dry-run

# Delete them without asking for confirmation for each person.
python3 people_comma_finder.py --list-people --delete-people --no-dry-run --no-confirm
```

`--hide-people` and `--delete-people` require `--list-people` and cannot be
combined. `--include-person-id ID` can be repeated for multiple known IDs. Use
`python3 people_comma_finder.py --help` to see the full command usage.

### Tags with commas

List all tags whose names contain a comma:

```sh
python3 tags_comma_list.py

# Sort matching tags by creation time, oldest first.
python3 tags_comma_list.py --sort-created-at asc

# Sort all matching tags by creation time, newest first.
python3 tags_comma_list.py --sort-created-at desc

# Show comma-named tags created after October 31, 2026, newest first.
python3 tags_comma_list.py --created-after 2026-10-31 --sort-created-at desc

# Filter after an exact UTC timestamp instead of the start of a date.
python3 tags_comma_list.py --created-after 2026-10-31T22:12:43Z

# Preview replacing comma tags on the first matching tag (dry-run is the default).
python3 tags_comma_list.py --replace --top 1

# Apply the replacement to the first matching tag after reviewing the dry-run.
python3 tags_comma_list.py --replace --top 1 --no-dry-run --confirm

# Apply replacements without pausing for confirmation after each tag.
python3 tags_comma_list.py --replace --no-dry-run --no-confirm
```

Example output:

```text
Found 1 tags with a comma in their name (out of 284 tags loaded).
Vasco Sousa Pinto, Tiago (tag ID: dbadf41c-ed89-4500-a8bd-9a89ff4f37f6; createdAt: 2026-10-31T22:12:43.231Z)
```

This read-only script prints each matching tag, its ID, and its creation date.
It loads the full tag collection and follows pagination when the API response
provides pages.
Immich's current `GET /tags` endpoint returns the complete tag list in one
response. By default, results are sorted alphabetically by tag name. Available
options:

| Option | Behavior |
| --- | --- |
| `--sort-created-at asc\|desc` | Sort by the tag's `createdAt` timestamp, oldest first (`asc`) or newest first (`desc`). Tags without a valid creation date appear last. |
| `--created-after DATE` | Include only tags created strictly after the given ISO date or datetime. Date-only values such as `2026-10-31` mean midnight UTC; timestamps may include `Z` or a timezone offset. |
| `--top N` | List or process only the first `N` matching tags after date filtering and sorting. Immich currently returns the full tag collection in one response, so this limits processing and output, not the API response size. |
| `--replace` | For each selected comma-named tag, add its comma-separated parts as tags on every matching image, then delete the original tag from Immich. Missing individual tags are created. |
| `--dry-run` / `--no-dry-run` | Preview planned replacement changes without making API updates (default), or apply them. `--no-dry-run` requires `--replace`. |
| `--confirm` / `--no-confirm` | Ask whether to apply each selected tag replacement in live mode (default), or apply all without prompts. A declined tag is skipped and processing continues. Dry-run never prompts. |

Passing `--confirm` with `--replace` while in dry-run mode prints a warning;
use `--no-dry-run` to enable live changes and confirmation prompts.

The script applies the date filter after loading the complete tag collection.
Tags without a valid `createdAt` value are excluded when `--created-after` is
used. Replacement searches all pages of images associated with each selected
tag and adds the individual tags to those images. Deleting the original tag
removes it from the entire library, including any videos that use it; the
individual replacement tags are added to images only. In dry-run mode the
script logs image counts, tags it would create, and the planned additions and
tag deletion without calling any update API. In live mode it reloads each
changed image and reports whether the original tag remains in Immich's asset
tag associations. This API check cannot confirm whether the web detail panel
also displays a matching tag imported from EXIF `TagList`/`Keywords` or an XMP
sidecar; Immich reads tags from those metadata sources as well.

### Similar tag names

```sh
# List tag names that are at least 80% similar to another tag name.
# Exact dot/space and first-letter capitalization variants are merged
# automatically. Other pairs scoring above 90% prompt before replacement.
python3 tags_similar_name_finder.py

# Use a 90% similarity cutoff.
python3 tags_similar_name_finder.py --threshold 90

# Include tags that have no similar names at the selected cutoff.
python3 tags_similar_name_finder.py --include-unmatched
```

The script loads every tag, following pagination when the API returns pages,
then lists each matching tag with its similar names and similarity scores.
Comparisons ignore case, accents, and punctuation. Similarity scores are string
comparisons, not calibrated probabilities. For pairs scoring above 90%, an
exact dot/space variant such as `Ana.Monjardino` and `Ana Monjardino` is merged
automatically into the undotted tag. A pair differing only in the first
letter's case, such as `joao` and `Joao`, is merged automatically into the
capitalized tag. Any tag in a high-similarity pair with zero assets is deleted
automatically; if both have zero assets, both are deleted. Other
dotted/undotted pairs ask whether to merge the dotted tag into the undotted
one. Other pairs show two numbered directions; enter `0` or press Enter to
skip. For these numbered choices, option 1 replaces the tag used by more
assets; ties use alphabetical order. A confirmed or automatic merge adds the
kept tag to every image and video using the replaced tag, then deletes the
replaced tag. The script continues to the next pair after each merge or prompt.
These changes require tag update, asset update, and tag delete permissions.
Available options:

| Option | Default | Behavior |
| --- | --- | --- |
| `--threshold PERCENT` | `80` | Minimum similarity percentage to report (0–100). |
| `--include-unmatched` | Off | Also list tags with no similar names at the selected threshold. |

### People with periods

```sh
python3 people_dot_finder.py
```

For each dotted name, the script lists exact matches after removing periods and
similar names ranked by similarity (80% or higher). It recommends the
highest-ranked option and asks whether to merge the dotted person into it. Enter
`y` or `yes` to confirm; any other response skips the merge. Merging changes
Immich data. This script has no command-line options.

```sh
# Review dotted names and possible duplicates; answer No or press Enter to skip a merge.
python3 people_dot_finder.py
```

### Similar people names

```sh
python3 people_similar_name_finder.py
```

This lists every person and possible duplicate names with a similarity score of
80% or higher. People without matches are listed too. Use `--threshold` to
change the cutoff using a fraction from 0 to 1. Use `--min-similarity` with a
percentage from 0 to 100 to show only people with at least one match at or
above that score; for example, `--min-similarity 90` shows people with a match
of 90% or higher and implies `--only-matches`. Add `--only-matches` by itself
to hide people without matches at the regular `--threshold`. Similarity scores
are name comparisons, not calibrated probabilities. For each distinct pair
scoring above 90%, the script asks whether to merge; confirming keeps the
alphabetically first person's record and merges the other person into it. IDs
involved in a confirmed merge are saved in
`~/.local/state/immich-library-tools/people_similar_name_merges.json` (or under
`$XDG_STATE_HOME`) so the script won't ask about them again on later runs.

To use it as a review-only pass, answer `n` or press Enter at each merge prompt.
There is no option to disable the prompts. Options for
`people_similar_name_finder.py`:

| Option | Default | Behavior |
| --- | --- | --- |
| `--threshold 0..1` | `0.8` | Minimum name similarity to report (for example, `0.9` is 90%). |
| `--min-similarity PERCENT` | None | Show only people with at least one match at or above this percentage (0–100); implies `--only-matches`. For example, `90` means 90% or higher. |
| `--only-matches` | Off | Hide people who have no names meeting the threshold. |

```sh
# List everyone and possible matches, including people with no matches.
python3 people_similar_name_finder.py

# Show only matches at 90% similarity or higher.
python3 people_similar_name_finder.py --only-matches --min-similarity 90

# Use an 85% cutoff; answer No at any merge prompt to continue without changes.
python3 people_similar_name_finder.py --threshold 0.85
```

The dot and similar-name tools continue to the next person after a declined
merge. Similarity scores are name comparisons, not calibrated probabilities.

### Image dates and folder dates

Compare each image's Immich timeline date with the date implied by its folder
path. The script reads `fileCreatedAt` and `originalPath` from Immich and does
not make changes.

```sh
# List all images whose timeline dates disagree with their folders.
python3 image_folder_date_validator.py

# List at most the first 20 mismatches.
python3 image_folder_date_validator.py --top 20

# Ignore backup copies whose filenames contain "safeBackup".
python3 image_folder_date_validator.py --ignore safeBackup
```

A folder named with just a year, such as `2005`, checks only the image year. A
folder beginning with `YYYY.MM` or `YYYY.MM.DD` also provides a target date,
using the first day when only a year and month are present. Images more than 60
days from that target date are listed. Images without a valid Immich date are
also listed when their folder provides a date. Folders without a recognizable
year or date are skipped.

Use `--ignore TEXT` to skip filenames containing a case-insensitive substring;
repeat the option to ignore multiple filename patterns.

### OneDrive safeBackup files

Scan the current directory and its subdirectories for files with a
`-safeBackup-####` suffix. The script pairs each backup with an existing
same-folder original, compares metadata and XMP sidecars, and reports whether
the file bytes or JPEG image stream differ. It leaves unmatched or ambiguous
backups untouched. The default is a dry run.

Run these commands from the repository directory. Replace
`/path/to/your/photo-library` with the folder to scan.

```sh
# Scan the current folder recursively and preview all matching backups.
python3 onedrive_safe_backup_finder.py

# Preview the first 10 backups under a specific library folder.
python3 onedrive_safe_backup_finder.py --folder "/path/to/your/photo-library" --top 10

# Only inspect safeBackup filenames containing "TiagoPC".
python3 onedrive_safe_backup_finder.py --folder "/path/to/your/photo-library" --search "TiagoPC"

# Include metadata differences in the comparison output.
python3 onedrive_safe_backup_finder.py --folder "/path/to/your/photo-library" --show-metadata

# Delete matched backups and their XMP sidecars, confirming each one.
python3 onedrive_safe_backup_finder.py --folder "/path/to/your/photo-library" --no-dry-run

# Auto-delete metadata-only differences; still confirm image-content differences.
python3 onedrive_safe_backup_finder.py --folder "/path/to/your/photo-library" --no-dry-run --no-confirm
```

The script recognizes the `filename-hostname-safeBackup-0001.ext` form used by
the `abraunegg/onedrive` client, as well as names without a hostname segment.
It only deletes a backup when exactly one likely original exists. `--top N`
limits the number of matching backup files processed. `--search TEXT` filters
safeBackup filenames by a case-insensitive substring and prefers a unique
candidate original containing that text. In live mode, `--no-confirm`
skips prompts for exact duplicates or verified metadata-only changes. The
script always asks before deleting when image content differs or cannot be
verified, even with `--no-confirm`. Metadata differences are hidden unless
`--show-metadata` is passed. ExifTool is required for metadata comparison. If
`ffmpeg` is available, the script also reports first-frame pixel differences;
without it, JPEG image streams are compared after omitting common metadata
segments. At the end, it reports how many safeBackup files it processed and
how many backup and XMP files it deleted.

### Videos without people tags
<img width="882" height="591" alt="image" src="https://github.com/user-attachments/assets/128db1e0-9deb-4626-8e47-1226f6fc84ae" />

List matching videos in the terminal:

```sh
python3 videos_without_people.py
```

The terminal script has no command-line options. It prints the total, each
matching video's filename, and its Immich link.

Start the paginated web view:

```sh
python3 videos_without_people_web.py
```

The web server defaults to `http://127.0.0.1:8765/` and 20 videos per page.
It binds to localhost by default. To open it from another device on your local
network, bind to all interfaces and browse to the computer's LAN address:

```sh
python3 videos_without_people_web.py --host 0.0.0.0
```

For example, serve 50 videos per page and filter to videos between one and ten
minutes long:

```sh
python3 videos_without_people_web.py --host 0.0.0.0 --port 8765 --page-size 50 --min-duration 1:00 --max-duration 10:00
```

Duration filters are optional. For example, to show videos between 30 seconds
and 5 minutes long:

```sh
python3 videos_without_people_web.py --min-duration 30 --max-duration 5:00
```

The server fetches thumbnails from Immich using the configured API key and
serves them through the local web page; the key is not sent to the browser.
Hover over a video thumbnail to play a muted preview; the preview pauses when
the pointer leaves the card.
Options:

| Option | Default | Behavior |
| --- | --- | --- |
| `--host HOST` | `127.0.0.1` | Address to bind the web server to. |
| `--port PORT` | `8765` | Port for the web server. |
| `--page-size N` | `20` | Matching videos per page (1–100). |
| `--min-duration DURATION` | None | Include videos at least this long. |
| `--max-duration DURATION` | None | Include videos no longer than this. |

Durations accept seconds (such as `30`), `MM:SS` (such as `5:00`), or
`HH:MM:SS` (such as `1:05:00`). Bounds are inclusive. When either duration
filter is set, videos with unknown durations are excluded.

The page sorts by duration, shortest first by default. Sorting is controlled in
the page and has no command-line option. Web server options:

| Option | Default | Behavior |
| --- | --- | --- |
| `--host HOST` | `127.0.0.1` | Address to bind to. Use `0.0.0.0` for access from other devices on the LAN. |
| `--port PORT` | `8765` | Web server port (1–65535). |
| `--page-size N` | `20` | Videos per page (1–100). |
| `--min-duration DURATION` | None | Include videos at least this long. |
| `--max-duration DURATION` | None | Include videos no longer than this. |

To connect from another device, start with `--host 0.0.0.0` and browse to
`http://<computer-LAN-address>:8765/`. Without `--host`, the server is only
reachable from the computer running it.

Press Ctrl+C in the terminal to stop the server.
