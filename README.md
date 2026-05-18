# imap-move

> Vibe coded with [Claude](https://claude.ai).

Migrates email from Proton Mail to Gmail via IMAP, preserving original timestamps and read/unread status. Fully resumable — interrupted runs pick up where they left off. Skips messages already present in Gmail, so it's safe to run alongside or after a partial migration done with another tool (e.g. Thunderbird).

## Requirements

- A **paid Proton Mail account** (Proton Bridge requires a paid plan)
- [Proton Bridge](https://proton.me/mail/bridge) installed and running
- A **Gmail account** with IMAP enabled (Settings → See all settings → Forwarding and POP/IMAP)
- A Gmail **App Password** — [generate one here](https://myaccount.google.com/apppasswords) (requires 2-Step Verification)
- Python 3.10+

## Setup

### On Linux / WSL (fish shell)

```fish
bash setup.sh
source .venv/bin/activate.fish
```

### On Linux / WSL (bash/zsh)

```bash
bash setup.sh
source .venv/bin/activate
```

### Manually

```bash
python3 -m venv .venv
source .venv/bin/activate  # or activate.fish for fish shell
pip install -r requirements.txt
```

## Configuration

Copy the example config and fill in your credentials:

```bash
cp config.example.ini config.ini
```

Open `config.ini` and set:

| Key | Where to find it |
|-----|-----------------|
| `[proton] username` | Your Proton Mail address |
| `[proton] password` | The Bridge-specific password shown in the Proton Bridge app |
| `[gmail] username` | Your Gmail address |
| `[gmail] app_password` | The App Password generated at myaccount.google.com/apppasswords |

`config.ini` is listed in `.gitignore` and will never be committed.

## Usage

```bash
python3 migrate.py
```

The script will:

1. List all folders in your Proton mailbox
2. Discover Gmail's special folders automatically using IMAP special-use attributes (RFC 6154)
3. Count messages across all folders and report how many remain to copy
4. For each folder, scan Gmail for messages already present (matched by `Message-ID` header) and skip them
5. Copy remaining messages to Gmail, preserving timestamps and read/unread status
6. Record each migrated message in `progress.db` so re-runs skip it instantly

Example output:

```
Connecting to Proton Bridge…
  Connected.
Connecting to Gmail…
  Connected.
Discovering Gmail special folders…
  Gmail special folders discovered:
    'Sent' → '[Gmail]/Sent Mail'
    'Drafts' → '[Gmail]/Drafts'
    'Trash' → '[Gmail]/Bin'
    'Spam' → '[Gmail]/Spam'
    'Junk' → '[Gmail]/Spam'
    'Archive' → '[Gmail]/All Mail'
Listing Proton folders…
  Found 4 folder(s): INBOX, Sent, Drafts, Work
Counting messages…
  'INBOX': 12,400
  'Sent': 8,203
  'Drafts': 45
  'Work': 1,102
Total: 21,750 messages, 0 already in progress DB.

── Folder: 'INBOX' → 'INBOX'
  12,400 messages in Proton folder.
  Scanning 3,100 existing messages in Gmail 'INBOX'…
  Found 3,100 messages with a Message-ID in Gmail 'INBOX'.
  Done: 9,300 copied, 3,100 skipped (already in Gmail)

Overall:  43%|████████████           | 9,300/21,750 msg [1:02:14<1:24:18, 2.47 msg/s]
```

## What is and isn't preserved

| Preserved | Not preserved |
|-----------|--------------|
| Original send/receive date | Folder hierarchy (Gmail labels are flat) |
| Read/unread status | Proton-specific labels beyond folder mapping |
| Folder structure (mapped to Gmail labels) | |

### Proton → Gmail folder mapping

Gmail's special folder names vary by locale (e.g. `[Gmail]/Bin` vs `[Gmail]/Trash`). The script discovers the correct names automatically at startup using IMAP special-use attributes, so no manual configuration is needed.

| Proton | Gmail (discovered at runtime) |
|--------|-------------------------------|
| INBOX | INBOX |
| Sent | the folder with `\Sent` attribute |
| Drafts | the folder with `\Drafts` attribute |
| Trash | the folder with `\Trash` attribute |
| Spam / Junk | the folder with `\Junk` attribute |
| Archive | the folder with `\All` attribute |
| Any other folder | Gmail label of the same name (created if absent) |

## Resuming after interruption

Just re-run `python3 migrate.py`. The script records every migrated message in `progress.db` (SQLite) and skips it on subsequent runs. It also re-scans Gmail for duplicates at the start of each folder, so it remains safe to run multiple times.

## Error handling

| Situation | Behaviour |
|-----------|-----------|
| Gmail drops the SSL connection mid-transfer | Reconnects automatically and retries, up to 5 times |
| Other transient Gmail errors | Exponential backoff (2s, 4s, 8s…), up to 5 retries |
| Message with no body | Logged as a warning, skipped, marked done |
| Script interrupted at any point | Re-run resumes from where it left off |

## Tuning

Two options in `config.ini` under `[options]`:

| Option | Default | Notes |
|--------|---------|-------|
| `batch_size` | 25 | Messages fetched from Proton per IMAP request. Lower if you hit timeout or memory errors. |
| `append_delay_seconds` | 0.1 | Pause between Gmail APPEND calls. Increase to 0.5–1.0 if Gmail returns rate-limit errors. |

## License

[MIT](LICENSE)
