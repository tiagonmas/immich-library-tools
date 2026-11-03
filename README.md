# Immich tools

Python command-line and web tools for auditing and maintaining an Immich
library. They find people and tags with comma or period names, review possible
duplicate people, inspect and optionally remove face assignments, and find
videos without people tags. Some tools can also hide, delete, or merge people
after confirmation. All tools use Immich's API and share the configuration
described below.

## Tools

| Script | What it does |
| --- | --- |
| `people_comma_finder.py` | Finds people whose names contain a comma, lists their associated images, reports the people tagged in each image, and checks whether each comma-separated name is also tagged. By default it previews removing the comma-named person's face from images. |
| `people_comma_list.py` | Lists people whose names contain a comma, with their IDs. It does not search images. |
| `tags_comma_list.py` | Loads all Immich tags and lists those whose names contain a comma. |
| `people_dot_finder.py` | Finds people whose names contain a period, shows exact or likely similar names (80% similarity or higher), recommends the top match, and prompts before merging. A confirmed merge keeps the recommended person's record. |
| `people_similar_name_finder.py` | Lists people and possible duplicate names. For pairs scoring above 90%, it asks whether to merge; a merge happens only after an affirmative answer. |
| `videos_without_people.py` | Lists videos with no people tagged, including a link to open each video in Immich. |
| `videos_without_people_web.py` | Runs a paginated web page with a thumbnail grid, hover video previews, durations, duration sorting (shortest first by default), and total count for videos that have no people tagged. It shows a loading indicator while counting. Each video link opens in a new tab. |

The comma finder can modify Immich data when run with `--no-dry-run`; it removes
the comma-named person's detected face entries from associated images. The dot
finder and similar-name finder can merge people after you confirm a suggested
merge. The remaining scripts only read data.

## Setup

Requirements: Python 3.10 or newer and an Immich API key with permission to read
people, assets, and tags. Set the Immich URL and API key in the environment:

```sh
export IMMICH_BASE_URL="http://localhost:2283"
export IMMICH_API_KEY="your-api-key"
```

`IMMICH_BASE_URL` may include `/api`; the scripts handle either form. Image and
video output uses direct Immich links. When the configured URL uses localhost,
the links use `192.168.1.200` so they can be opened from another device.
People-listing tools fetch every page from Immich, including hidden people.

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

# Preview deleting all identified comma-named people.
python3 people_comma_finder.py --list-people --delete-people

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
```

This read-only script prints each matching tag and its ID. It loads the full
tag collection and follows pagination when the API response provides pages.
Immich's current `GET /tags` endpoint returns the complete tag list in one
response.

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
