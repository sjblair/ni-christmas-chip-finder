# Christmas Chip Finder

A map of Just Eat shops in Northern Ireland that sell chips, fries, roasties, or roast potatoes under a Christmas or festive name. Stuffing, turkey, pigs in blankets, and cranberry count too, when the dish is still one of those sides.

The site is the files in `static/`. `results.json` is what the map draws, with Leaflet. `analytics.html` uses the same file for the price spread, the dearest and cheapest postcodes, and a few comparisons that are not very scientific. Filters are postcode, shop name, and item name. A filled box narrows the list, an empty one does not, and the three combine. The postcode filter uses the shop's own address: BT1 matches BT1 and leaves BT14 out.

## Updating the data

`python3 scrape.py` searches every BT postcode district once and then exits. It needs Firefox (`firefox` on `PATH`, or Firefox.app on macOS) and uses only the Python standard library. Just Eat rejects direct requests, so the script opens the site in headless Firefox and reads the search API from that page.

`data/outcodes.json` is the 80 real BT districts from postcodes.io. Unused numbers are left out: BT50, BT58, BT59, BT72, BT73, and BT83 through BT91.

Recent failures go first, then districts that have never been searched, then previous successes, oldest `lastSuccessfulRun` first. Chip and chips share one request. Fry and fries do too, and so do roastie and roasties. The queries are christmas, xmas, and festive, each with chips, fries, roasties, and roast potatoes. Stuffing, turkey, pigs in blankets, and cranberry are searched with chips, fries, and roasties.

A dish matches when a festive word and a potato word both appear in the name, in either order. `Chips Christmas` counts, and `X-mas` is treated as xmas. Only modifier words can sit between them, such as loaded, large, style, special, dinner, or garlic. Festive tortilla chips and chocolate-chip shortbread do not match. A festive feast or a Christmas dinner does not match either, unless the name also says chips, fries, roasties, or roast potatoes.

The script writes `static/results.json` to a temp file and renames it over the old one. Per-district stores are kept in `data/hits/`. A failed district keeps that hits file and its previous `lastSuccessfulRun`, and the manifest status becomes `failed`. The published file is rebuilt from the hit files, so the district still shows its last good stores.

`data/manifest.json` has one row per district, with status, `searchedAt`, `lastSuccessfulRun`, and any error. `data/run-status.json` is written at the end with the duration, the success and failure counts, which districts failed, and a line per district.

The first retry waits 5 minutes, then 10, 20, and 40. That is 4 retries. Requests are limited to 0.4 per second. The workflow job is allowed 3 hours, enough for a full pass and those waits.

`data/search/` and `data/restaurants/` are local API caches, listed in `.gitignore`.

## GitHub Actions

[`.github/workflows/search.yml`](.github/workflows/search.yml) runs the scraper on GitHub-hosted Ubuntu at 06:00 UTC every other day of the month (`0 6 */2 * *`). GitHub's scheduler has no exact 48-hour interval, so a 31-day month can run on the 31st and again on the 1st. The workflow can also be started by hand.

The job installs Python 3.12 and Firefox, runs `python3 scrape.py`, and commits these paths:

- `static/results.json`
- `data/manifest.json`
- `data/hits/`
- `data/outcodes.json`
- `data/run-status.json`

`data/run-status.json` is also uploaded as the `run-status` artifact if the job stops early. The workflow sets `contents: write` on the default token, which is what lets Actions push the commit. The repository still has to allow Actions to write to the branch.

## Serving the site

From the repo root:

```
python3 -m http.server 8765 --directory static
```

Open http://127.0.0.1:8765. The page fetches `results.json`, so serve the folder over HTTP.

GitHub Pages' "deploy from a branch" option publishes the repository root or `/docs` only. The site is in `static/`, so Pages needs a workflow that uploads that directory, or some other host that serves the folder as the site root. Links in the page are relative, so a project site such as `https://<user>.github.io/<repo>/` works when `static` is the published root.
