"""Export emails from IMAP to .eml files for the import script to consume."""

import argparse
import imaplib
import os
import re
import sys
from pathlib import Path


def sanitize_filename(subject: str) -> str:
    """Convert subject to a safe filename."""
    clean = re.sub(r"[^\w\s-]", "", subject)
    clean = re.sub(r"\s+", "_", clean.strip())
    return clean[:80] if clean else "untitled"


def export_emails(
    host: str,
    port: int,
    username: str,
    password: str,
    search_subject: str = "Mirror Test",
    output_dir: Path | None = None,
    use_ssl: bool = True,
) -> int:
    """Connect to IMAP and export matching emails as .eml files.

    Returns the number of emails exported.
    """
    if output_dir is None:
        output_dir = Path(__file__).parent / "data"
    output_dir.mkdir(parents=True, exist_ok=True)

    if use_ssl:
        imap = imaplib.IMAP4_SSL(host, port)
    else:
        imap = imaplib.IMAP4(host, port)

    imap.login(username, password)
    imap.select("INBOX")

    _, message_ids = imap.search(None, "SUBJECT", f'"{search_subject}"')
    ids = message_ids[0].split()

    exported = 0
    for msg_id in ids:
        _, msg_data = imap.fetch(msg_id, "(RFC822)")
        if msg_data[0] is None:
            continue

        raw_email = msg_data[0][1]
        filename = f"{msg_id.decode()}_{exported:04d}.eml"
        (output_dir / filename).write_bytes(raw_email)
        exported += 1
        print(f"  Exported: {filename}")

    imap.logout()
    print(f"\nExported {exported} emails to {output_dir}")
    return exported


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Mirror Test emails from IMAP")
    parser.add_argument("--host", default=os.environ.get("IMAP_HOST", ""), help="IMAP host")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("IMAP_PORT", "993")), help="IMAP port"
    )
    parser.add_argument("--username", default=os.environ.get("IMAP_USER", ""), help="IMAP username")
    parser.add_argument("--password", default=os.environ.get("IMAP_PASS", ""), help="IMAP password")
    parser.add_argument("--subject", default="Mirror Test", help="Subject filter")
    parser.add_argument("--output-dir", type=Path, default=None, help="Output directory")
    parser.add_argument("--no-ssl", action="store_true", help="Disable SSL")
    args = parser.parse_args()

    if not args.host or not args.username or not args.password:
        print("Error: IMAP credentials required.", file=sys.stderr)
        print(
            "Set IMAP_HOST, IMAP_USER, IMAP_PASS env vars or use --host/--username/--password",
            file=sys.stderr,
        )
        sys.exit(1)

    export_emails(
        host=args.host,
        port=args.port,
        username=args.username,
        password=args.password,
        search_subject=args.subject,
        output_dir=args.output_dir,
        use_ssl=not args.no_ssl,
    )


if __name__ == "__main__":
    main()
