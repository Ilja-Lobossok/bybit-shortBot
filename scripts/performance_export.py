"""Read an existing journal read-only and export exact canonical Decimal JSON rows.

No network, no bot imports, no migrations. Output contains financial data: keep private.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def export(path, output, kind):
    path, output = Path(path).resolve(strict=True), Path(output)
    if output.resolve() == path or output.exists():
        raise ValueError("Output must be a new separate private file")
    if kind not in ("orders", "executions", "closed_pnl", "transactions", "equity"):
        raise ValueError("Unknown export kind")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1)) as db:
        if kind == "equity":
            rows = db.execute("SELECT received_ms,exchange_ms,payload FROM account_snapshots ORDER BY received_ms")
        else:
            rows = db.execute("SELECT payload FROM facts WHERE kind=? ORDER BY event_ms,id", (kind,))
        # JSON Lines: original decimals retained, constant-memory streaming.
        import os
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            for row in rows:
                value = json.loads(row[-1])
                if kind == "equity":
                    value.update(received_ms=row[0], exchange_ms=row[1])
                stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--kind", required=True, choices=("orders", "executions", "closed_pnl", "transactions", "equity"))
    args = parser.parse_args()
    export(args.journal, args.output, args.kind)
    print("Private export complete; contents not displayed.")


if __name__ == "__main__":
    main()
