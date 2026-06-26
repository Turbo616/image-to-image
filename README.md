# Image To Image

Local image similarity search panel for team use.

## Features

- Web panel, no command line required for normal users.
- Supports jpg, jpeg, png and webp.
- Supports Synology Drive folder search.
- Uses a shared fingerprint database to speed up repeated searches.
- Does not keep massive source-image caches in the shared folder.
- Exports CSV and HTML preview results.

## Start On Windows

Double-click:

```text
open_panel.vbs
```

The launcher starts the local panel and opens:

```text
http://127.0.0.1:5000/
```

## Dependencies

```bash
pip install -r requirements.txt
```

For team ZIP distribution, you can include `vendor/wheels` as offline Python packages. The `vendor` directory is intentionally ignored by Git because it is large.

## Shared Fingerprint Database

Default shared index directory:

```text
\\Ouyeegx002\business-operation-shared-folder\image_matcher_cloud_index
```

In the actual local code this path points to the Chinese-named company shared folder. Keep `hash_cache.sqlite3`; old `images` source-image cache folders can be removed after the new version is confirmed working.
