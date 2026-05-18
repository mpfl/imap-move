#!/usr/bin/env python3
"""
Migrate email from Proton Mail (via Bridge) to Gmail.
Preserves original timestamps and read/unread status.
Fully resumable — already-migrated messages are tracked in a local SQLite DB.
Skips messages already present in Gmail (e.g. copied via Thunderbird) by
matching on Message-ID header.
"""

import configparser
import email
import imaplib
import sqlite3
import sys
import time
from pathlib import Path

import imapclient
from tqdm import tqdm

CONFIG_FILE = Path("config.ini")
DB_FILE = Path("progress.db")

# RFC 6154 special-use attributes → Proton folder name(s) that map to them
SPECIAL_USE_MAP = {
    b"\\Sent":      ["Sent"],
    b"\\Drafts":    ["Drafts"],
    b"\\Trash":     ["Trash"],
    b"\\Junk":      ["Spam", "Junk"],
    b"\\All":       ["Archive"],
    b"\\Flagged":   [],   # [Gmail]/Starred — no Proton equivalent
    b"\\Important": [],   # [Gmail]/Important — no Proton equivalent
}

FLAGS_TO_PRESERVE = {b"\\Seen"}

# Batch size for fetching Message-IDs from Gmail
GMAIL_SCAN_BATCH = 200


def log(msg: str) -> None:
    """Print a status line, safely interleaving with tqdm if active."""
    tqdm.write(msg)


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if not CONFIG_FILE.exists():
        sys.exit(
            f"Config file '{CONFIG_FILE}' not found. "
            "Copy config.example.ini, fill in your credentials, and save it as config.ini."
        )
    cfg.read(CONFIG_FILE)
    return cfg


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS migrated (
            src_folder TEXT    NOT NULL,
            src_uid    INTEGER NOT NULL,
            PRIMARY KEY (src_folder, src_uid)
        )
    """)
    conn.commit()
    return conn


def is_migrated(conn: sqlite3.Connection, folder: str, uid: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM migrated WHERE src_folder=? AND src_uid=?", (folder, uid)
    ).fetchone() is not None


def mark_migrated(conn: sqlite3.Connection, folder: str, uid: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO migrated (src_folder, src_uid) VALUES (?, ?)", (folder, uid)
    )
    conn.commit()


def connect_proton(cfg: configparser.ConfigParser) -> imapclient.IMAPClient:
    host = cfg.get("proton", "host", fallback="127.0.0.1")
    port = cfg.getint("proton", "port", fallback=1143)
    use_ssl = cfg.getboolean("proton", "ssl", fallback=False)
    log(f"  host={host}  port={port}  ssl={use_ssl}")
    client = imapclient.IMAPClient(host, port=port, ssl=use_ssl, use_uid=True)
    client.login(cfg["proton"]["username"], cfg["proton"]["password"])
    return client


def connect_gmail(cfg: configparser.ConfigParser) -> imapclient.IMAPClient:
    log("  host=imap.gmail.com  port=993  ssl=true")
    client = imapclient.IMAPClient("imap.gmail.com", port=993, ssl=True, use_uid=True)
    client.login(cfg["gmail"]["username"], cfg["gmail"]["app_password"])
    return client


def reconnect_proton(cfg: configparser.ConfigParser) -> imapclient.IMAPClient:
    log("  Proton Bridge connection lost — reconnecting…")
    src = connect_proton(cfg)
    log("  Reconnected.")
    return src


def reconnect_gmail(cfg: configparser.ConfigParser) -> imapclient.IMAPClient:
    log("  Gmail connection lost — reconnecting…")
    dst = connect_gmail(cfg)
    log("  Reconnected.")
    return dst


def discover_gmail_folders(dst: imapclient.IMAPClient) -> tuple[dict[str, str], set[str]]:
    """
    Discover Gmail's special-use folders via RFC 6154 IMAP attributes.
    Returns (folder_map, system_folders) where folder_map maps Proton folder
    names to their Gmail equivalents, and system_folders is the set of Gmail
    folders that cannot be created via IMAP CREATE.
    """
    folder_map: dict[str, str] = {}
    system_folders: set[str] = set()

    for flags, _delim, name in dst.list_folders():
        for attr, proton_names in SPECIAL_USE_MAP.items():
            if attr in flags:
                system_folders.add(name)
                for proton_name in proton_names:
                    folder_map[proton_name] = name

    log("  Gmail special folders discovered:")
    for proton, gmail in folder_map.items():
        log(f"    {proton!r} → {gmail!r}")

    return folder_map, system_folders


def list_source_folders(src: imapclient.IMAPClient) -> list[str]:
    return [
        name
        for flags, _delim, name in src.list_folders()
        if b"\\Noselect" not in flags
    ]


def gmail_folder_for(proton_folder: str, folder_map: dict[str, str]) -> str:
    return folder_map.get(proton_folder, proton_folder)


def ensure_folder(dst: imapclient.IMAPClient, folder: str, system_folders: set[str]) -> None:
    if folder in system_folders:
        return
    if not dst.folder_exists(folder):
        log(f"  Creating Gmail label: {folder!r}")
        dst.create_folder(folder)


def count_messages(src: imapclient.IMAPClient, folder: str) -> int:
    try:
        info = src.select_folder(folder, readonly=True)
        return int(info.get(b"EXISTS", 0))
    except Exception:
        return 0


def extract_message_id(raw_headers: bytes) -> str | None:
    """Parse a Message-ID from raw header bytes. Returns None if absent."""
    msg = email.message_from_bytes(raw_headers)
    mid = msg.get("Message-ID", "").strip()
    return mid or None


def fetch_gmail_message_ids(dst: imapclient.IMAPClient, folder: str) -> set[str]:
    """
    Return the set of Message-IDs already present in a Gmail folder.
    Fetches headers only, in batches, to keep memory usage low.
    """
    dst.select_folder(folder, readonly=True)
    uids = dst.search("ALL")
    if not uids:
        return set()

    log(f"  Scanning {len(uids):,} existing messages in Gmail {folder!r}…")
    known: set[str] = set()
    for i in range(0, len(uids), GMAIL_SCAN_BATCH):
        batch = uids[i : i + GMAIL_SCAN_BATCH]
        data = dst.fetch(batch, ["BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"])
        for msg_data in data.values():
            raw = msg_data.get(b"BODY[HEADER.FIELDS (MESSAGE-ID)]", b"")
            mid = extract_message_id(raw)
            if mid:
                known.add(mid)
    log(f"  Found {len(known):,} messages with a Message-ID in Gmail {folder!r}.")
    return known


def migrate_folder(
    src: imapclient.IMAPClient,
    dst: imapclient.IMAPClient,
    cfg: configparser.ConfigParser,
    conn: sqlite3.Connection,
    src_folder: str,
    dst_folder: str,
    pbar: tqdm,
    batch_size: int,
    append_delay: float,
) -> tuple[imapclient.IMAPClient, imapclient.IMAPClient]:
    log(f"\n── Folder: {src_folder!r} → {dst_folder!r}")

    for attempt in range(6):
        try:
            src.select_folder(src_folder, readonly=True)
            all_uids = src.search("ALL")
            break
        except imaplib.IMAP4.abort:
            if attempt == 5:
                raise
            src = reconnect_proton(cfg)
    log(f"  {len(all_uids):,} messages in Proton folder.")

    # Filter out messages already recorded in our progress DB
    db_pending = [uid for uid in all_uids if not is_migrated(conn, src_folder, uid)]
    db_skipped = len(all_uids) - len(db_pending)
    if db_skipped:
        log(f"  {db_skipped:,} already in progress DB — skipping.")

    if not db_pending:
        log("  Nothing to do.")
        return src, dst

    # Scan Gmail for Message-IDs to catch messages copied by other means
    gmail_known = fetch_gmail_message_ids(dst, dst_folder)

    n_new = n_dupe = n_empty = 0

    for i in range(0, len(db_pending), batch_size):
        batch = db_pending[i : i + batch_size]
        # Re-select source folder in case Gmail scan changed the selected folder
        for attempt in range(6):
            try:
                src.select_folder(src_folder, readonly=True)
                messages = src.fetch(batch, ["RFC822", "INTERNALDATE", "FLAGS"])
                break
            except imaplib.IMAP4.abort:
                if attempt == 5:
                    raise
                src = reconnect_proton(cfg)

        for uid, data in messages.items():
            raw = data.get(b"RFC822")

            if not raw:
                log(f"  Warning: empty body for UID {uid} — skipping.")
                mark_migrated(conn, src_folder, uid)
                n_empty += 1
                pbar.update(1)
                continue

            # Duplicate check against messages already in Gmail
            mid = extract_message_id(raw)
            if mid and mid in gmail_known:
                mark_migrated(conn, src_folder, uid)
                n_dupe += 1
                pbar.update(1)
                continue

            date = data.get(b"INTERNALDATE")
            src_flags = data.get(b"FLAGS", [])
            dst_flags = [f for f in src_flags if f in FLAGS_TO_PRESERVE]

            for attempt in range(6):
                try:
                    dst.append(dst_folder, raw, flags=dst_flags, msg_time=date)
                    break
                except imaplib.IMAP4.abort:
                    if attempt == 5:
                        raise
                    dst = reconnect_gmail(cfg)
                except Exception as exc:
                    if attempt == 5:
                        raise
                    wait = 2 ** (attempt + 1)
                    log(f"  Retry {attempt + 1}/5 for UID {uid} ({exc}), waiting {wait}s…")
                    time.sleep(wait)

            if mid:
                gmail_known.add(mid)
            mark_migrated(conn, src_folder, uid)
            n_new += 1
            pbar.update(1)

            if append_delay:
                time.sleep(append_delay)

    log(
        f"  Done: {n_new:,} copied, {n_dupe:,} skipped (already in Gmail)"
        + (f", {n_empty:,} empty" if n_empty else "")
    )
    return src, dst


def main() -> None:
    cfg = load_config()
    conn = init_db()

    print("Connecting to Proton Bridge…")
    src = connect_proton(cfg)
    print("  Connected.")

    print("Connecting to Gmail…")
    dst = connect_gmail(cfg)
    print("  Connected.")

    print("Discovering Gmail special folders…")
    folder_map, system_folders = discover_gmail_folders(dst)

    batch_size = cfg.getint("options", "batch_size", fallback=25)
    append_delay = cfg.getfloat("options", "append_delay_seconds", fallback=0.1)

    print("Listing Proton folders…")
    src_folders = list_source_folders(src)
    print(f"  Found {len(src_folders)} folder(s): {', '.join(src_folders)}")

    print("Counting messages…")
    folder_counts: dict[str, int] = {}
    for folder in src_folders:
        n = count_messages(src, folder)
        folder_counts[folder] = n
        print(f"  {folder!r}: {n:,}")
    total = sum(folder_counts.values())
    already_done = conn.execute("SELECT COUNT(*) FROM migrated").fetchone()[0]
    print(f"Total: {total:,} messages, {already_done:,} already in progress DB.")

    with tqdm(
        total=total,
        initial=already_done,
        unit="msg",
        desc="Overall",
        dynamic_ncols=True,
    ) as pbar:
        for src_folder in src_folders:
            dst_folder = gmail_folder_for(src_folder, folder_map)
            pbar.set_postfix(folder=src_folder[:30], refresh=False)
            ensure_folder(dst, dst_folder, system_folders)
            src, dst = migrate_folder(
                src, dst, cfg,
                conn, src_folder, dst_folder,
                pbar, batch_size, append_delay,
            )

    print("\nMigration complete.")
    src.logout()
    dst.logout()
    conn.close()


if __name__ == "__main__":
    main()
