#!/usr/bin/env python3
"""
swiftseek-mysql.py — a lean, grep-style search client for text documents stored in MySQL.

WHY THIS EXISTS (read this first, agent)
----------------------------------------
This reproduces the "grep beats vector RAG inside an agent loop" pattern for plain
text/paragraph documents (insurance docs, policies, manuals — NOT code).

Design choices that matter:
  * WHOLE document per row. No chunking, no overlap. Chunking is an embedding-era
    tax; grep locates a position and lets YOU (the agent) expand context on demand.
  * LEXICAL search via real regex (Python `re`), run over the raw content. This is
    the truest reproduction of grep and avoids MySQL FULLTEXT's min-token-length
    quirk (which silently drops short codes like "HO3" or state abbreviations).
  * Metadata columns (filepath, title, doc_type) are filtered in SQL FIRST to shrink
    the haystack, then the regex runs only over the surviving rows.
  * FAILS LOUDLY. No match => empty result + exit code 1. It never returns
    plausible-but-wrong neighbours the way a vector store does.

HOW TO USE IT IN AN AGENT LOOP
------------------------------
Every command prints JSON to stdout. Exit codes (grep convention):
    0 = matches found / success
    1 = no matches (this is a SIGNAL, not a crash — refine and retry)
    2 = error (bad args, DB down, etc.)

The intended loop for answering a question about the documents:

  1. SEARCH with your best literal/regex guess:
         python swiftseek-mysql.py search "flood"
  2. If exit code == 1 (no matches), DO NOT give up. Expand the query the way a
     human would — you supply the semantics the lexical layer lacks:
         python swiftseek-mysql.py search "flood|water damage|discharge|seepage" -i
  3. Narrow with metadata when you know it (this is your WHERE clause):
         python swiftseek-mysql.py search "deductible" --doc-type policy --filepath "2024/"
  4. When a match looks relevant, READ AROUND IT. Either bump context...
         python swiftseek-mysql.py search "burst pipe" -C 8
     ...or pull the whole document / a line range:
         python swiftseek-mysql.py get 42
         python swiftseek-mysql.py get 42 --line-range 80 140
  5. Answer ONLY from text you actually retrieved. If search keeps returning exit
     code 1 after reasonable synonym expansion, the answer is likely not lexical —
     say so rather than inventing it.

OUTPUT IS CAPPED so a broad first search can't blow your context window:
  * --max-docs (default 20) and --max-matches (default 5 per doc) bound the COUNT.
  * --max-line-chars (default 300) bounds the SIZE of each line. Extracted PDF/DOCX
    text often has paragraph- or whole-document-sized "lines", so each emitted line
    is snippeted around the match rather than dumped whole; shortened lines are
    flagged "truncated": true.
  * --max-output-chars (default 40000, ~10k tokens) is a hard budget for the whole
    response. If a search would exceed it, it stops early and sets top-level
    "truncated": true with a hint. When you see that, NARROW the query (more specific
    pattern or metadata filters) instead of re-running blindly — or pass a larger
    --max-output-chars if you genuinely need the volume. Every cap is an override.

LIMITATION TO RESPECT: lexical search matches strings, not meaning. "Am I covered
if a pipe bursts?" will not match "sudden and accidental discharge of water" on
tokens. Query expansion (step 2) narrows that gap but does not fully close it. For
genuinely conceptual questions, lexical grep is the wrong tool — flag it.

SETUP
-----
    pip install pymysql
    export SWIFTSEEK_HOST=localhost SWIFTSEEK_USER=app SWIFTSEEK_PASSWORD=secret SWIFTSEEK_DB=docs
    python swiftseek-mysql.py migrate                       # create the table once
    python swiftseek-mysql.py add --file policy_001.txt --doc-type policy
    python swiftseek-mysql.py search "endorsement"

Credentials come ONLY from env vars (never hard-code them, never pass secrets on the
CLI). This script issues plain SELECT/INSERT; run it under a DB user scoped to just
this table. Content ingested here must already be extracted plain text — PDF/DOCX
text extraction is the caller's job, not this script's.

COMMANDS
--------
    migrate                         create the documents table (idempotent)
    add                             insert one document
    list                            list document metadata (no content)
    get <id>                        fetch one full document (or a line range)
    search <pattern>                regex grep over content, with metadata filters
"""

import argparse
import json
import os
import re
import sys

try:
    import pymysql
except ImportError:
    sys.stderr.write("Missing dependency. Run: pip install pymysql\n")
    sys.exit(2)


# ----------------------------------------------------------------------------- #
# Config & connection
# ----------------------------------------------------------------------------- #
def connect():
    """Open a MySQL connection from SWIFTSEEK_* env vars. Exits 2 on failure."""
    try:
        return pymysql.connect(
            host=os.environ.get("SWIFTSEEK_HOST", "localhost"),
            port=int(os.environ.get("SWIFTSEEK_PORT", "3306")),
            user=os.environ.get("SWIFTSEEK_USER", "root"),
            password=os.environ.get("SWIFTSEEK_PASSWORD", ""),
            database=os.environ.get("SWIFTSEEK_DB", "docs"),
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )
    except Exception as e:  # noqa: BLE001 - report any connection problem uniformly
        emit({"error": f"db_connection_failed: {e}"}, code=2)


def emit(payload, code=0):
    """Print JSON to stdout and exit with the given code. Single exit point."""
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    sys.exit(code)


# ----------------------------------------------------------------------------- #
# migrate
# ----------------------------------------------------------------------------- #
DDL = """
CREATE TABLE IF NOT EXISTS documents (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    filepath    VARCHAR(1024) NOT NULL,
    title       VARCHAR(512)  NULL,
    doc_type    VARCHAR(64)   NULL,
    content     MEDIUMTEXT    NOT NULL,
    char_count  INT           NOT NULL DEFAULT 0,
    created_at  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_doc_type (doc_type),
    KEY idx_filepath (filepath(255))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def cmd_migrate(_args):
    conn = connect()
    with conn.cursor() as cur:
        cur.execute(DDL)
    emit({"ok": True, "action": "migrate", "detail": "documents table ready"})


# ----------------------------------------------------------------------------- #
# add
# ----------------------------------------------------------------------------- #
def cmd_add(args):
    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError as e:
            emit({"error": f"cannot_read_file: {e}"}, code=2)
        filepath = args.filepath or os.path.abspath(args.file)
    elif args.content is not None:
        content = args.content
        if not args.filepath:
            emit({"error": "--filepath is required when using --content"}, code=2)
        filepath = args.filepath
    else:
        emit({"error": "provide either --file or --content"}, code=2)

    title = args.title or os.path.basename(filepath)
    conn = connect()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO documents (filepath, title, doc_type, content, char_count) "
            "VALUES (%s, %s, %s, %s, %s)",
            (filepath, title, args.doc_type, content, len(content)),
        )
        doc_id = cur.lastrowid
    emit({"ok": True, "action": "add", "id": doc_id,
          "filepath": filepath, "char_count": len(content)})


# ----------------------------------------------------------------------------- #
# list
# ----------------------------------------------------------------------------- #
def cmd_list(args):
    where, params = _metadata_filters(args)
    sql = ("SELECT id, filepath, title, doc_type, char_count, created_at "
           "FROM documents")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id LIMIT %s"
    params.append(args.limit)

    conn = connect()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    for r in rows:  # JSON-safe timestamps
        r["created_at"] = str(r["created_at"])
    emit({"count": len(rows), "documents": rows}, code=0 if rows else 1)


# ----------------------------------------------------------------------------- #
# get
# ----------------------------------------------------------------------------- #
def cmd_get(args):
    conn = connect()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, filepath, title, doc_type, content, char_count, created_at "
            "FROM documents WHERE id = %s", (args.id,))
        row = cur.fetchone()
    if not row:
        emit({"error": f"no document with id {args.id}"}, code=1)

    row["created_at"] = str(row["created_at"])
    if args.line_range:
        lines = row["content"].splitlines()
        start, end = args.line_range  # 1-based inclusive
        sliced = lines[max(0, start - 1):end]
        row["content"] = "\n".join(sliced)
        row["line_range"] = [start, end]
        row["total_lines"] = len(lines)
    emit({"document": row})


# ----------------------------------------------------------------------------- #
# search  (the core: SQL metadata filter -> regex grep -> context windows)
# ----------------------------------------------------------------------------- #
def _snippet(text, max_chars, span=None):
    """Cap a line at max_chars. Returns (text, truncated_bool).

    Extracted PDF/DOCX text often has paragraph- or document-sized "lines", so an
    uncapped match can drag a huge blob into the agent's context. When the line is
    too long and we know where the match is (span), centre the window on it (like
    ripgrep --max-columns); otherwise truncate from the start. max_chars <= 0 means
    no limit.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    if span is None:
        return text[:max_chars] + " …", True
    s, e = span
    pad = max(0, (max_chars - (e - s)) // 2)
    hi = min(len(text), max(e, s + max_chars) if (e - s) >= max_chars else e + pad)
    lo = max(0, hi - max_chars)
    snip = text[lo:hi]
    if lo > 0:
        snip = "… " + snip
    if hi < len(text):
        snip = snip + " …"
    return snip, True


def cmd_search(args):
    where, params = _metadata_filters(args)
    sql = "SELECT id, filepath, title, content FROM documents"
    if where:
        sql += " WHERE " + " AND ".join(where)

    # Build the matcher. -F treats the pattern as a literal string.
    flags = re.IGNORECASE if args.ignore_case else 0
    pat = re.escape(args.pattern) if args.fixed else args.pattern
    try:
        rx = re.compile(pat, flags)
    except re.error as e:
        emit({"error": f"bad_regex: {e}"}, code=2)

    conn = connect()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    results = []
    total_matches = 0
    budget_used = 0
    truncated = False
    for row in rows:
        lines = row["content"].splitlines()
        doc_matches = []
        for i, line in enumerate(lines):
            m = rx.search(line)
            if not m:
                continue
            lo = max(0, i - args.context)
            hi = min(len(lines), i + args.context + 1)
            context = []
            for j in range(lo, hi):
                span = m.span() if j == i else None
                txt, trunc = _snippet(lines[j], args.max_line_chars, span)
                entry = {"line_no": j + 1, "text": txt, "match": j == i}
                if trunc:
                    entry["truncated"] = True
                context.append(entry)
                budget_used += len(txt)
            line_txt, _ = _snippet(line, args.max_line_chars, m.span())
            doc_matches.append({"line_no": i + 1, "line": line_txt,
                                "context": context})
            budget_used += len(line_txt)
            if budget_used >= args.max_output_chars:
                truncated = True
                break
            if len(doc_matches) >= args.max_matches:
                break
        if doc_matches:
            total_matches += len(doc_matches)
            results.append({
                "id": row["id"],
                "filepath": row["filepath"],
                "title": row["title"],
                "match_count": len(doc_matches),
                "matches": doc_matches,
            })
        if truncated or len(results) >= args.max_docs:
            break

    payload = {
        "pattern": args.pattern,
        "ignore_case": args.ignore_case,
        "docs_searched": len(rows),
        "docs_matched": len(results),
        "total_matches": total_matches,
        "approx_output_chars": budget_used,
        "truncated": truncated,
        "results": results,
    }
    if truncated:
        payload["hint"] = ("Output hit the ~max-output-chars budget and stopped "
                           "early; more matches likely exist. Narrow the query "
                           "(more specific pattern, add metadata filters), or pass a "
                           "larger --max-output-chars if you really need it all.")
    elif not results:
        payload["hint"] = ("No lexical match. Expand with synonyms / regex "
                           "alternation (e.g. 'a|b|c'), add -i, or loosen metadata "
                           "filters. If still empty, the question may be conceptual "
                           "rather than lexical.")
    emit(payload, code=0 if results else 1)


# ----------------------------------------------------------------------------- #
# shared: parameterised metadata WHERE builder (no string interpolation -> no SQLi)
# ----------------------------------------------------------------------------- #
def _metadata_filters(args):
    where, params = [], []
    if getattr(args, "id_filter", None) is not None:
        where.append("id = %s")
        params.append(args.id_filter)
    if getattr(args, "doc_type", None):
        where.append("doc_type = %s")
        params.append(args.doc_type)
    if getattr(args, "filepath", None):
        where.append("filepath LIKE %s")
        params.append(f"%{args.filepath}%")
    return where, params


# ----------------------------------------------------------------------------- #
# CLI
# ----------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        prog="swiftseek-mysql.py",
        description="grep-style search over text documents in MySQL (agent tool).")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="create the documents table")

    a = sub.add_parser("add", help="insert one document")
    a.add_argument("--file", help="path to a UTF-8 text file to ingest")
    a.add_argument("--content", help="raw text content (alternative to --file)")
    a.add_argument("--filepath", help="original source path to record as metadata")
    a.add_argument("--title", help="document title (defaults to file basename)")
    a.add_argument("--doc-type", dest="doc_type", help="e.g. policy, claim, manual")

    ls = sub.add_parser("list", help="list document metadata")
    ls.add_argument("--doc-type", dest="doc_type")
    ls.add_argument("--filepath", help="substring filter on filepath")
    ls.add_argument("--limit", type=int, default=100)

    g = sub.add_parser("get", help="fetch one full document by id")
    g.add_argument("id", type=int)
    g.add_argument("--line-range", nargs=2, type=int, metavar=("START", "END"),
                   help="1-based inclusive line range to return instead of full doc")

    s = sub.add_parser("search", help="regex grep over content")
    s.add_argument("pattern", help="regex (or literal with -F)")
    s.add_argument("-i", "--ignore-case", action="store_true", dest="ignore_case")
    s.add_argument("-F", "--fixed", action="store_true",
                   help="treat pattern as a literal string, not regex")
    s.add_argument("-C", "--context", type=int, default=2,
                   help="lines of context around each match (default 2)")
    s.add_argument("--max-matches", type=int, default=5,
                   help="max matches reported per document (default 5)")
    s.add_argument("--max-docs", type=int, default=20,
                   help="max documents reported (default 20)")
    s.add_argument("--max-line-chars", type=int, default=300, dest="max_line_chars",
                   help="cap per emitted line; long lines are snippeted around the "
                        "match (default 300, 0 = unlimited). Guards against giant "
                        "single-line PDF/DOCX paragraphs.")
    s.add_argument("--max-output-chars", type=int, default=40000,
                   dest="max_output_chars",
                   help="approx total char budget for the whole result; stops early "
                        "and sets truncated=true when exceeded (default 40000, "
                        "~10k tokens). Raise it if you deliberately want more.")
    # metadata filters = the SQL WHERE clause
    s.add_argument("--doc-type", dest="doc_type")
    s.add_argument("--filepath", help="substring filter on filepath")
    s.add_argument("--id", dest="id_filter", type=int,
                   help="restrict to a single document id")
    return p


DISPATCH = {
    "migrate": cmd_migrate,
    "add": cmd_add,
    "list": cmd_list,
    "get": cmd_get,
    "search": cmd_search,
}


def main():
    args = build_parser().parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()