# Google Drive `/pack` feature

## Bot command

```text
/pack https://drive.google.com/drive/folders/FOLDER_ID
```

The command is implemented in `main.py`. It calls the authenticated Worker endpoint, then sends the resulting per-season/release pack pages to Telegram. Existing single-file Google Drive handling remains on `/generate.aspx`.

## Cloudflare deployment

Deploy `google_drive_worker.js` as a module Worker. Create a Cloudflare KV namespace and bind it to the Worker as `PACKS`.

Set these Cloudflare secrets:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `BOT_API_KEY`
- `LINK_SIGNING_SECRET`

`BOT_API_KEY` and `LINK_SIGNING_SECRET` must be separate random secrets of at least 32 characters. The bot receives only `BOT_API_KEY`; never expose `LINK_SIGNING_SECRET` to the bot.

Optional Worker variables:

| Variable | Default | Purpose |
|---|---:|---|
| `FILE_LINK_EXPIRY_DAYS` | `7` | Expiry of each signed download URL |
| `PACK_PAGE_EXPIRY_DAYS` | `7` | KV retention of the pack page |
| `PACK_MAX_FILES` | `1000` | Maximum number of binary files scanned in a folder tree |
| `PACK_MAX_FOLDERS` | `500` | Maximum number of folders traversed |
| `PACK_MAX_DEPTH` | `20` | Maximum nested folder depth |

For service-account authentication, use `GOOGLE_SERVICE_ACCOUNT_JSON` instead of the three OAuth variables. Share the Drive folder with the service-account email.

## Bot host variables

```text
GOOGLE_DRIVE_WORKER_URL=https://your-google-drive-worker.workers.dev
GOOGLE_DRIVE_API_KEY=the-same-value-as-Worker-BOT_API_KEY
```

Use the base Worker URL, without a path suffix.

## How grouping works

The Worker recursively scans nested Drive folders. For TV files it reads the series prefix, `SxxEyy` episode code, and release suffix from each filename. It groups by show + season + release suffix, so randomly named subfolders do not affect grouping. Different releases of the same season become separate packs instead of being mixed.

Example files:

```text
Sawaranaide.Kotesashi-kun.S01E01.1080p.Blu-Ray.REMUX.AVC.LPCM.2.0-YOKAI.mkv
Sawaranaide.Kotesashi-kun.S01E02.1080p.Blu-Ray.REMUX.AVC.LPCM.2.0-YOKAI.mkv
```

Generated pack name:

```text
Sawaranaide.Kotesashi-kun.S01.1080p.Blu-Ray.REMUX.AVC.LPCM.2.0-YOKAI
```

The generated pack name excludes both the episode number and file extension. Original filenames remain unchanged on the pack page. For non-episodic files, the Worker creates a collection pack named after the root Drive folder.

## Pack page

Each pack page lists the original filenames with Download buttons. The underlying signed Worker URLs are not displayed as text. `Copy all links` copies the actual signed URLs, one per line. `Copy filenames` is also provided.

## Permissions and file types

The Google account configured in the Worker must be able to read the submitted folder and every nested folder. The current implementation includes downloadable binary files and skips Google Docs, Sheets, Slides, and Drive shortcuts.

## Security action required

The previously uploaded Worker source included exposed credentials and secrets. Revoke/rotate the Google OAuth refresh token and client secret, replace the Worker API key and signing secret, and deploy only the new values as secrets/environment variables. No credential values are embedded in this updated source.
