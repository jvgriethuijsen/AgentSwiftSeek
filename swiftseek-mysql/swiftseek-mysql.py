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
    pip install -r requirements.txt          # just pymysql (pypdf is optional)
    cp .env.example .env  &&  edit .env       # credentials auto-load from .env
    python swiftseek-mysql.py migrate         # create the table once
    python swiftseek-mysql.py add --file policy_001.pdf --doc-type policy
    python swiftseek-mysql.py ingest ./docs --doc-type policy   # a whole folder
    python swiftseek-mysql.py search "endorsement"

Credentials come ONLY from the environment (SWIFTSEEK_HOST/PORT/USER/PASSWORD/DB) —
never hard-coded, never passed as CLI args. A `.env` file next to this script (or in
the cwd, or pointed to by SWIFTSEEK_ENV) is auto-loaded at startup; real environment
variables always win over it. This script issues plain SELECT/INSERT — run it under
a DB user scoped to just this table.

TEXT EXTRACTION (built in — few dependencies)
---------------------------------------------
`add --file` and `ingest` accept .txt/.md (and similar), .docx, and .pdf:
  * .docx — unzipped + parsed with the standard library only (zipfile + xml).
  * .pdf  — a built-in, pure-stdlib extractor (zlib) handles common text PDFs; if
            `pypdf` is installed it is used automatically for higher fidelity, but
            it is NOT required.
PDF extraction is best effort: scanned/image-only PDFs and exotic font encodings can
come back empty or garbled. `ingest` reports empty extraction as a failure rather
than swallowing it — treat that as a signal, not success.

COMMANDS
--------
    migrate                         create the documents table (idempotent)
    add                             insert one document (text/.docx/.pdf via --file)
    ingest <path>                   recursively ingest a file or folder of documents
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
    sys.stderr.write("Missing dependency. Run: pip install -r requirements.txt "
                     "(or: pip install pymysql)\n")
    sys.exit(2)

# JSON is emitted to stdout; force UTF-8 so non-ASCII text (accents, em-dashes, CJK)
# never raises UnicodeEncodeError on a Windows cp1252 console or a non-UTF-8 locale.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


# ----------------------------------------------------------------------------- #
# .env loading  (best practice, stdlib only: real env always wins over the file)
# ----------------------------------------------------------------------------- #
def _load_dotenv():
    """Load a .env file into os.environ without overriding real environment vars.

    Search order (first existing file wins): $SWIFTSEEK_ENV, ./.env, then a .env next
    to this script. Each line is KEY=VALUE (optional `export ` prefix; #-comments and
    blank lines ignored; surrounding single/double quotes stripped). Values are set
    with setdefault, so anything already exported in the real environment is left
    untouched — the environment overrides the file, never the other way round.
    """
    here = (os.path.dirname(os.path.abspath(__file__))
            if "__file__" in globals() else os.getcwd())
    candidates = [p for p in (os.environ.get("SWIFTSEEK_ENV"),
                              os.path.join(os.getcwd(), ".env"),
                              os.path.join(here, ".env")) if p]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    if line.startswith("export "):
                        line = line[len("export "):].lstrip()
                    key, _, val = line.partition("=")
                    key, val = key.strip(), val.strip()
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                        val = val[1:-1]
                    if key and key not in os.environ:
                        os.environ[key] = val
        except OSError:
            pass  # unreadable config -> fall back to real env / defaults
        return  # only the first existing file is loaded


_load_dotenv()


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
# text extraction  (PDF + DOCX, as few dependencies as possible — 1 file, no installs)
# ----------------------------------------------------------------------------- #
# .docx is just a ZIP of XML, so it is parsed with the standard library alone.
# .pdf has no stdlib reader; a built-in, pure-stdlib extractor (zlib) handles the
# common text case (FlateDecode / uncompressed content streams). If `pypdf` is
# installed it is used instead for higher fidelity, but it is NOT required. Either
# way PDF text is best effort: scanned/image-only PDFs and exotic font encodings
# can yield empty or garbled text — empty output is surfaced, never silently passed.
TEXT_EXTS = {".txt", ".text", ".md", ".markdown", ".rst", ".log", ".csv"}
SUPPORTED_EXTS = TEXT_EXTS | {".pdf", ".docx"}


class ExtractError(Exception):
    """Raised when a file cannot be turned into plain text."""


def extract_text(path):
    """Plain text for a .txt/.md/.docx/.pdf file. Raises ExtractError on failure."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _extract_pdf(path)
    if ext == ".docx":
        return _extract_docx(path)
    if ext == ".doc":
        raise ExtractError("legacy .doc (binary Word) is not supported; convert to "
                           ".docx or .txt first")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as e:
        raise ExtractError(f"cannot_read_file: {e}")


def _extract_docx(path):
    """Text from a Word .docx using only the standard library (zipfile + XML)."""
    import zipfile
    from xml.etree import ElementTree as ET
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml")
    except KeyError:
        raise ExtractError("not a Word .docx (no word/document.xml)")
    except (zipfile.BadZipFile, OSError) as e:
        raise ExtractError(f"docx_read_failed: {e}")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise ExtractError(f"docx_xml_parse_failed: {e}")
    paras = []
    for p in root.iter(w + "p"):
        buf = []
        for node in p.iter():
            if node.tag == w + "t":
                buf.append(node.text or "")
            elif node.tag == w + "tab":
                buf.append("\t")
            elif node.tag in (w + "br", w + "cr"):
                buf.append("\n")
        paras.append("".join(buf))
    return "\n".join(paras)


def _extract_pdf(path):
    """Text from a PDF: prefer pypdf if importable, else the built-in extractor."""
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None
    if PdfReader is not None:
        try:
            reader = PdfReader(path)
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
            if text.strip():
                return text
        except Exception:  # noqa: BLE001 - any pypdf failure falls back to built-in
            pass
    return _extract_pdf_builtin(path)


def _extract_pdf_builtin(path):
    """Pure-stdlib PDF text extraction (zlib). Best effort; see module notes."""
    import zlib
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        raise ExtractError(f"cannot_read_file: {e}")
    chunks = []
    pos = 0
    while True:
        s = data.find(b"stream", pos)
        if s == -1:
            break
        if data[s - 3:s] == b"end":            # matched the tail of 'endstream'
            pos = s + 6
            continue
        start = s + 6                           # skip past 'stream'
        if data[start:start + 2] == b"\r\n":
            start += 2
        elif data[start:start + 1] in (b"\n", b"\r"):
            start += 1
        end = data.find(b"endstream", start)
        if end == -1:
            break
        raw = data[start:end]
        pos = end + 9
        try:
            blob = zlib.decompress(raw)
        except zlib.error:
            try:
                blob = zlib.decompressobj().decompress(raw)
            except zlib.error:
                blob = raw                      # likely already uncompressed
        chunks.append(_pdf_text_from_content(blob))
    return "\n".join(c for c in chunks if c and c.strip())


def _pdf_text_from_content(blob):
    """Pull visible text out of one decompressed PDF content stream (best effort)."""
    s = blob.decode("latin-1", "replace") if isinstance(blob, bytes) else blob
    n = len(s)
    lines, line = [], []
    in_array = False
    space_pending = False

    def emit_str(text):
        nonlocal space_pending
        if space_pending and line:
            line.append(" ")
        space_pending = False
        line.append(text)

    def newline():
        lines.append("".join(line))
        line.clear()

    digits = "0123456789"
    i = 0
    while i < n:
        c = s[i]
        if c == "(":                            # literal (string)
            i += 1
            depth = 1
            out = []
            while i < n and depth > 0:
                ch = s[i]
                if ch == "\\":
                    i += 1
                    if i >= n:
                        break
                    esc = s[i]
                    simple = {"n": "\n", "r": "\r", "t": "\t", "b": "\b",
                              "f": "\f", "(": "(", ")": ")", "\\": "\\"}
                    if esc in simple:
                        out.append(simple[esc]); i += 1
                    elif esc in "01234567":
                        od = esc; i += 1
                        for _ in range(2):
                            if i < n and s[i] in "01234567":
                                od += s[i]; i += 1
                            else:
                                break
                        out.append(chr(int(od, 8) & 0xFF))
                    elif esc == "\r":
                        i += 1
                        if i < n and s[i] == "\n":
                            i += 1
                    elif esc == "\n":
                        i += 1
                    else:
                        out.append(esc); i += 1
                elif ch == "(":
                    depth += 1; out.append(ch); i += 1
                elif ch == ")":
                    depth -= 1
                    if depth > 0:
                        out.append(ch)
                    i += 1
                else:
                    out.append(ch); i += 1
            emit_str("".join(out))
        elif c == "<" and i + 1 < n and s[i + 1] != "<":     # <hex string>
            j = s.find(">", i)
            if j == -1:
                break
            hexs = "".join(ch for ch in s[i + 1:j] if ch in "0123456789abcdefABCDEF")
            if len(hexs) % 2:
                hexs += "0"
            try:
                emit_str(bytes.fromhex(hexs).decode("latin-1", "replace"))
            except ValueError:
                pass
            i = j + 1
        elif c == "<" and i + 1 < n and s[i + 1] == "<":     # << dict >> -> skip
            i += 2
        elif c == "[":
            in_array = True; i += 1
        elif c == "]":
            in_array = False; space_pending = False; i += 1
        elif c == "-" or c in digits:
            j = i
            while j < n and (s[j] in digits or s[j] in "-."):
                j += 1
            if in_array:
                try:
                    if float(s[i:j]) <= -100:     # wide negative kern ~ a space
                        space_pending = True
                except ValueError:
                    pass
            i = j
        elif s[i:i + 2] in ("Td", "TD", "T*", "Tm"):
            newline(); i += 2
        elif c in ("'", '"'):
            newline(); i += 1
        else:
            i += 1
    if line:
        newline()
    return "\n".join(ln for ln in lines if ln.strip())


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
            content = extract_text(args.file)
        except ExtractError as e:
            emit({"error": str(e)}, code=2)
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
# ingest  (walk a file/folder, extract text, insert each — idempotent by filepath)
# ----------------------------------------------------------------------------- #
def _iter_files(root, recursive, exts):
    """Yield ingestible file paths under `root` (a file or a directory)."""
    if os.path.isfile(root):
        yield root
        return
    if recursive:
        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                if os.path.splitext(name)[1].lower() in exts:
                    yield os.path.join(dirpath, name)
    else:
        for name in sorted(os.listdir(root)):
            full = os.path.join(root, name)
            if os.path.isfile(full) and os.path.splitext(name)[1].lower() in exts:
                yield full


def cmd_ingest(args):
    if not os.path.exists(args.path):
        emit({"error": f"no such path: {args.path!r}"}, code=2)
    conn = connect()
    added, skipped, failed = [], [], []
    with conn.cursor() as cur:
        for path in _iter_files(args.path, not args.no_recursive, SUPPORTED_EXTS):
            ap = os.path.abspath(path)
            cur.execute("SELECT id FROM documents WHERE filepath = %s LIMIT 1", (ap,))
            if cur.fetchone():
                if not args.reindex:
                    skipped.append(ap)
                    continue
                cur.execute("DELETE FROM documents WHERE filepath = %s", (ap,))
            try:
                content = extract_text(path)
            except ExtractError as e:
                failed.append({"filepath": ap, "error": str(e)})
                continue
            if not content.strip():
                failed.append({"filepath": ap,
                               "error": "no_text_extracted (empty / image-only PDF?)"})
                continue
            cur.execute(
                "INSERT INTO documents (filepath, title, doc_type, content, char_count) "
                "VALUES (%s, %s, %s, %s, %s)",
                (ap, os.path.basename(ap), args.doc_type, content, len(content)),
            )
            added.append({"id": cur.lastrowid, "filepath": ap,
                          "char_count": len(content)})
    emit({"ok": True, "action": "ingest",
          "added": len(added), "skipped": len(skipped), "failed": len(failed),
          "added_docs": added, "skipped_paths": skipped, "failed_docs": failed},
         code=0 if (added or skipped) else 1)


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

    a = sub.add_parser("add", help="insert one document (text/.docx/.pdf via --file)")
    a.add_argument("--file", help="path to a .txt/.md/.docx/.pdf file to ingest")
    a.add_argument("--content", help="raw text content (alternative to --file)")
    a.add_argument("--filepath", help="original source path to record as metadata")
    a.add_argument("--title", help="document title (defaults to file basename)")
    a.add_argument("--doc-type", dest="doc_type", help="e.g. policy, claim, manual")

    ing = sub.add_parser("ingest",
                         help="recursively ingest a file or folder of documents")
    ing.add_argument("path", help="file or directory (.txt/.md/.docx/.pdf)")
    ing.add_argument("--doc-type", dest="doc_type",
                     help="tag every ingested document with this type")
    ing.add_argument("--no-recursive", action="store_true",
                     help="do not descend into subdirectories")
    ing.add_argument("--reindex", action="store_true",
                     help="re-extract files already in the table (replace them) "
                          "instead of skipping")

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
    "ingest": cmd_ingest,
    "list": cmd_list,
    "get": cmd_get,
    "search": cmd_search,
}


def main():
    args = build_parser().parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()