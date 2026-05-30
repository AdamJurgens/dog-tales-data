# dog-tales-data

Public data feed for the **Dog Tales** companion app.

`adoptable-pets.json` is generated from the public [dogtales.ca](https://www.dogtales.ca)
adoption pages by `scrape.py`, refreshed daily by a GitHub Action. The app fetches
this file at launch and falls back to its bundled copy when offline.

```
{ "updatedAt": "YYYY-MM-DD", "pets": [ { "id", "name", "species", "status", "imageUrl", "photos", "traits", ... } ] }
```

Feed URL: `https://raw.githubusercontent.com/AdamJurgens/dog-tales-data/main/adoptable-pets.json`

Run locally: `DOG_TALES_FEED_ONLY=1 DOG_TALES_FEED_JSON=adoptable-pets.json python scrape.py`

Data is sourced from Dog Tales Rescue & Sanctuary's public website for the companion app.
