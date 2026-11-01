# Immich tools

Small command-line and web tools for inspecting people and videos in an Immich
library. The scripts use Immich's API and share the configuration described
below.

## Tools

| Script | What it does |
| --- | --- |
| `people_comma_finder.py` | Finds people whose names contain a comma, lists their associated images, reports the people tagged in each image, and checks whether each comma-separated name is also tagged. By default it previews removing the comma-named person's face from images. |
| `people_comma_list.py` | Lists people whose names contain a comma, with their IDs. It does not search images. |
| `people_dot_finder.py` | Finds people whose names contain a period, shows exact or likely similar names (80% similarity or higher), recommends the top match, and prompts before merging. A confirmed merge keeps the recommended person's record. |
| `people_similar_name_finder.py` | Lists every person and names with a high similarity score as possible duplicates. It only reports matches and does not change Immich data. |
| `videos_without_people.py` | Lists videos with no people tagged, including a link to open each video in Immich. |
| `videos_without_people_web.py` | Runs a paginated web page with a thumbnail grid, hover video previews, durations, duration sorting (shortest first by default), and total count for videos that have no people tagged. It shows a loading indicator while counting. Each video link opens in a new tab. |

The comma finder can modify Immich data when run with `--no-dry-run`; it removes
the comma-named person's detected face entries from associated images. The dot
finder can merge people after you confirm each suggested merge. The other
scripts only read data.

## Setup

Requirements: Python 3.10 or newer and an Immich API key with permission to read
people and assets. Set the Immich URL and API key in the environment:

```sh
export IMMICH_BASE_URL="http://localhost:2283"
export IMMICH_API_KEY="your-api-key"
```

`IMMICH_BASE_URL` may include `/api`; the scripts handle either form. Image and
video output uses direct Immich links. When the configured URL uses localhost,
the links use `192.168.1.200` so they can be opened from another device.

## Usage

### People with commas

```sh
python3 people_comma_finder.py
python3 people_comma_list.py
```

The finder inspects all associated images by default. It logs each person,
image, Immich link, tagged people, and whether all individual names are tagged.
The tag check is informational; in live mode, the comma-named person's face is
removed from every selected associated image, including images missing one or
more individual-name tags.

Options for `people_comma_finder.py`:

| Option | Behavior |
| --- | --- |
| `--top N` | Inspect at most `N` images total. By default, inspect all images. |
| `--list-people` | List comma-named people and exit without searching images. |
| `--tag-status yes\|no\|all` | Show images where all individual names are tagged (`yes`), where one or more are missing (`no`), or both (`all`, default). This also limits which images are eligible for removal in live mode. |
| `--dry-run` / `--no-dry-run` | Preview removals without changing Immich (default), or apply removals. |
| `--confirm` / `--no-confirm` | Ask before each image change (default), or apply live changes without prompts. Confirmation only applies with `--no-dry-run`. |

Examples:

```sh
python3 people_comma_finder.py --top 10
python3 people_comma_finder.py --tag-status no --top 100
python3 people_comma_finder.py --top 2 --no-dry-run --confirm
```

### People with periods

```sh
python3 people_dot_finder.py
```

For each dotted name, the script lists exact matches after removing periods and
similar names ranked by similarity. It recommends the highest-ranked option
and asks whether to merge the dotted person into it. Enter `y` or `yes` to
confirm; any other response skips the merge. Merging changes Immich data.

### Similar people names

```sh
python3 people_similar_name_finder.py
```

This lists every person and possible duplicate names with a similarity score of
80% or higher. People without matches are listed too. Use `--threshold` to
change the cutoff; for example, `--threshold 0.9` shows only matches at 90% or
higher. Similarity scores are name comparisons, not calibrated probabilities.

### Videos without people tags

List matching videos in the terminal:

```sh
python3 videos_without_people.py
```

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

Press Ctrl+C in the terminal to stop the server.
