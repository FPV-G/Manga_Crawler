# manga_crawler

A multi-source manga batch downloader: reads a name list, aggregates search results from multiple manga sites, automatically picks the best source, and downloads the whole series with resume, cross-source continuation, page-gap filling, chapter caching, cover download, and an interactive control menu.

## Features

- Aggregated search across multiple sources: CopyManga, ZaiManHua, BaoZiMh, RuManHua
- Automatic source selection: configured priority + readable chapter count + total chapters + title match score
- Chapter-gap correction: if the top-priority source has far fewer chapters than the richest source, it is treated as a wrong doujinshi/derivative work and the richest source is chosen instead
- Multi-chapter / multi-threaded downloads; pages named 001, 002, ...
- Resume support: already-downloaded chapters are skipped on rerun
- Cross-source continuation: on failure it switches to the next source and continues from the failed chapters
- Page-gap filling: every run verifies page counts against the source and fills missing pages
- Chapter cache: image lists are cached locally so switching sources does not require refetching
- Cover download: saves the cover into the series folder
- Stall protection: a chapter stuck beyond the configured timeout triggers a source switch; per-image timeouts are configurable
- Interactive menu: press Ctrl+F for skip comic/chapter, re-download previous chapter, pause, status, exit

## Installation

Requires Python 3.10+.

```bash
pip install requests zhconv beautifulsoup4 pycryptodome curl_cffi
```

## Quick Start

1. Edit `名单.txt` (manga name list), one name per line.
2. Copy `config.json` and adjust as needed (the script reads `config.json` from the current directory).
3. Run:

```bash
python kaobei_downloader.py
```

## Configuration

Key options are in `config.json` (each item has an inline comment):

| Key | Description |
| --- | --- |
| `sources` | Enabled sources, comma separated |
| `source_priority` | Source priority, earlier means higher priority |
| `chapter_gap_threshold` | Auto-pick the richest source when the chapter gap reaches this value |
| `chapter_workers` | Concurrent chapters |
| `image_workers` | Concurrent image downloads per chapter |
| `image_rate` | Image requests per minute, 0 = unlimited |
| `timeout` | Per-image download timeout in seconds |
| `stall_timeout` | Seconds before a stalled chapter triggers source switch |
| `cache_ttl_hours` | Chapter cache validity in hours |
| `match_threshold` | Title fuzzy-match threshold |
| `zmh_account` / `zmh_password` | Optional ZaiManHua login |

CLI arguments override the config file, for example:

```bash
python kaobei_downloader.py --range 1-50
python kaobei_downloader.py --refresh
python kaobei_downloader.py --source-priority rumanhua,copy,zmh
```

## Interactive Controls

| Key | Action |
| --- | --- |
| `Ctrl+C` | First press shows a hint, second press exits |
| `Ctrl+F` | Open the control menu |

Menu actions: skip current comic, skip current chapter, re-download previous chapter, pause/resume, show status, exit.

## Output Layout

```
downloads/
  Comic Name/       # named after the entry in the name list
    cover.jpg
    第1话/
      001.jpg
      002.jpg
      ...
    第2话/
      ...
```

## Logs

| File | Description |
| --- | --- |
| `失败.log` | Failure records with date and reason |
| `result.log` | Full text log |
| `result.jsonl` | Structured log |

## Supported Sources

| Source | Notes |
| --- | --- |
| CopyManga | Default API endpoint, requires network access |
| ZaiManHua | Some chapters require login/membership, credentials in config |
| BaoZiMh | May block non-local network nodes |
| RuManHua | Long titles are automatically shortened for search |

## Disclaimer

This project is for learning and technical communication only. All content belongs to the original authors and resource sites. Do not use it commercially. Delete downloaded content promptly and support the official releases.