"""Import Mirror Test .eml archive into Stoa posts table."""

import argparse
import email
import email.message
import email.utils
import html
import sqlite3
import sys
from datetime import UTC, datetime
from email.header import decode_header
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from stoa.db import init_db


def decode_mime_header(value: str) -> str:
    """Decode a MIME-encoded header value."""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def extract_author(from_header: str) -> str:
    """Extract display name from From header, falling back to email address."""
    name, addr = email.utils.parseaddr(from_header)
    if name:
        return decode_mime_header(name) if "=?" in name else name
    return addr


def extract_body(msg: email.message.Message) -> str:
    """Extract plain text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def generate_tldr(body: str) -> str:
    """Generate a TLDR from email body: strip quotes and whitespace, max 280 chars."""
    lines = body.splitlines()
    non_quoted = [line for line in lines if not line.startswith(">")]
    text = " ".join(non_quoted).strip()
    # Collapse multiple spaces
    while "  " in text:
        text = text.replace("  ", " ")
    if len(text) > 280:
        text = text[:277] + "..."
    return text


def body_to_html(body: str) -> str:
    """Convert plain text body to basic HTML."""
    paragraphs = body.split("\n\n")
    html_parts = []
    for para in paragraphs:
        escaped = html.escape(para.strip())
        if escaped:
            html_parts.append(f"<p>{escaped}</p>")
    return "\n".join(html_parts) if html_parts else "<p></p>"


def classify_space(subject: str) -> str:
    """Classify post into a space based on subject."""
    if "dream journal" in subject.lower():
        return "dreams"
    return "inbox"


def parse_date(date_str: str | None) -> str:
    """Parse email date header into ISO format."""
    if not date_str:
        return datetime.now(UTC).isoformat()
    parsed = email.utils.parsedate_to_datetime(date_str)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def import_emails(
    db_path: Path | None = None,
    data_dir: Path | None = None,
) -> dict[str, int]:
    """Import .eml files from data_dir into the posts table.

    Returns a dict with 'imported' and 'skipped' counts.
    """
    if db_path is None:
        db_path = Path("stoa.db")
    if data_dir is None:
        data_dir = Path(__file__).parent / "data"

    conn = init_db(db_path)
    imported = 0
    skipped = 0

    eml_files = sorted(data_dir.glob("*.eml"))

    for eml_path in eml_files:
        raw = eml_path.read_text(errors="replace")
        msg = email.message_from_string(raw)

        message_id = msg.get("Message-ID", "")
        if not message_id:
            message_id = f"<generated-{eml_path.stem}@stoa>"

        # Check for duplicate
        existing = conn.execute(
            "SELECT id FROM posts WHERE message_id = ?", (message_id,)
        ).fetchone()
        if existing:
            skipped += 1
            print(f"  SKIP (duplicate): {eml_path.name}")
            continue

        from_header = msg.get("From", "Unknown")
        author = extract_author(from_header)
        subject_raw = msg.get("Subject", "(no subject)")
        subject = decode_mime_header(subject_raw) if "=?" in subject_raw else subject_raw
        body = extract_body(msg)
        date_str = msg.get("Date")
        in_reply_to = msg.get("In-Reply-To")

        tldr = generate_tldr(body)
        body_html = body_to_html(body)
        space = classify_space(subject)
        timestamp = parse_date(date_str)

        try:
            conn.execute(
                "INSERT INTO posts (message_id, author, subject, tldr, body_markdown, body_html, token_cost, space, timestamp, in_reply_to) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    author,
                    subject,
                    tldr,
                    body,
                    body_html,
                    0,
                    space,
                    timestamp,
                    in_reply_to,
                ),
            )
            conn.commit()
            imported += 1
            print(f"  OK: {eml_path.name} -> [{space}] {subject[:50]}")
        except sqlite3.IntegrityError:
            skipped += 1
            print(f"  SKIP (integrity): {eml_path.name}")

    conn.close()
    print(f"\nDone: {imported} imported, {skipped} skipped, {len(eml_files)} total files")
    return {"imported": imported, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description="Import Mirror Test .eml archive")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path("stoa.db"),
        help="Path to SQLite database (default: stoa.db)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).parent / "data",
        help="Directory containing .eml files (default: scripts/data/)",
    )
    args = parser.parse_args()

    print(f"Importing from: {args.data_dir}")
    print(f"Database: {args.db_path}")
    print()

    import_emails(db_path=args.db_path, data_dir=args.data_dir)


if __name__ == "__main__":
    main()
