#!/usr/bin/env python3
"""
swiftseek-lite.py — grep-style document search with ZERO dependencies.

Same idea and same agent interface as swiftseek.py (the MySQL version), but stripped
to the bone: no database server, nothing to `pip install`. The entire corpus lives
in one JSON file and the only imports are Python standard library.

WHY NOT TinyDB (or any lib)?
----------------------------
TinyDB would be one more dependency, and its query engine would sit unused: grep
full-scans the whole corpus anyway, which is trivially fast at the target scale
(~50 documents' worth of docx/pdf text, a few MB of JSON). A plain JSON file read
into memory is leaner, has zero install footprint, and is dead simple to inspect or
back up (it's just a file). If you later outgrow this — concurrent writers, hundreds
of MB, real query needs — that's the signal to move to swiftseek.py + MySQL, not to
bolt a query lib onto a flat file.

WHY THIS EXISTS (read this first, agent)
----------------------------------------
Reproduces the "grep beats vector RAG inside an agent loop" pattern for plain text
documents (insurance docs, policies, manuals — NOT code).
  * WHOLE document per record. No chunking, no overlap. grep finds a position; YOU
    expand context on demand.
  * LEXICAL search via real regex over raw content — the truest grep reproduction,
    and it never drops short codes like "HO3" the way tokenized indexes do.
  * Metadata (filepath, title, doc_type) is filtered FIRST to shrink the haystack,
    then the regex runs only over surviving records.
  * FAILS LOUDLY. No match => empty result + exit code 1, never plausible-but-wrong
    neighbours like a vector store.

HOW TO USE IT IN AN AGENT LOOP
------------------------------
Every command prints JSON to stdout. Exit codes (grep convention):
    0 = matches found / success
    1 = no matches (a SIGNAL to refine, not a crash)
    2 = error (bad args, store missing, etc.)

  1. SEARCH with your best literal/regex guess:
         python swiftseek-lite.py search "flood"
  2. If exit code == 1, expand the query — YOU supply the semantics the lexical
     layer lacks:
         python swiftseek-lite.py search "flood|water damage|discharge|seepage" -i
  3. Narrow with metadata when you know it (this is your WHERE clause):
         python swiftseek-lite.py search "deductible" --doc-type policy --filepath 2024/
  4. When a match looks relevant, READ AROUND IT:
         python swiftseek-lite.py search "burst pipe" -C 8
         python swiftseek-lite.py get 42
         python swiftseek-lite.py get 42 --line-range 80 140
  5. Answer ONLY from text you retrieved. If search stays empty after reasonable
     synonym expansion, the answer is likely conceptual, not lexical — say so rather
     than inventing it.

OUTPUT IS CAPPED so a broad first search can't blow your context window:
  * --max-docs (default 20) and --max-matches (default 5/doc) bound the COUNT.
  * --max-line-chars (default 300) bounds the SIZE of each line. Extracted PDF/DOCX
    text often has paragraph- or document-sized "lines", so each emitted line is
    snippeted around the match; shortened lines are flagged "truncated": true.
  * --max-output-chars (default 40000 ≈ ~10k tokens) is a hard budget for the whole
    response. On overflow it stops early and sets top-level "truncated": true + a
    hint — NARROW the query rather than re-running blindly, or raise the cap. Every
    cap is an override.

LIMITATION TO RESPECT: lexical search matches strings, not meaning. "Am I covered if
a pipe bursts?" won't match "sudden and accidental discharge of water" on tokens.
Query expansion narrows that gap but doesn't close it; for conceptual questions,
grep is the wrong tool — flag it.

SETUP
-----
    # nothing to install. optional: point at a store location
    export SWIFTSEEK_STORE=./swiftseek.json
    python swiftseek-lite.py init
    python swiftseek-lite.py add --file policy_001.pdf --doc-type policy
    python swiftseek-lite.py ingest ./docs --doc-type policy   # a whole folder
    python swiftseek-lite.py search "endorsement"

The store is a single JSON file (SWIFTSEEK_STORE, default ./swiftseek.json). Writes
are atomic (temp file + replace); this is single-writer, not built for concurrent
processes.

TEXT EXTRACTION (built in — few/zero dependencies)
--------------------------------------------------
`add --file` and `ingest` accept .txt/.md (and similar), .docx, and .pdf:
  * .txt/.md/etc — read as UTF-8.
  * .docx        — unzipped + parsed with the standard library only (zipfile + xml).
  * .pdf         — a built-in, pure-stdlib extractor (zlib) handles common text PDFs.
                   If `pypdf` is installed it is used automatically for higher
                   fidelity, but it is NOT required.
PDF extraction is best effort: scanned/image-only PDFs (no text layer) and exotic
font encodings can come back empty or garbled. `ingest` reports empty extraction as
a failure rather than swallowing it — treat that as a signal, not success.

COMMANDS
--------
    init                 create an empty store (idempotent)
    add                  insert one document (text/.docx/.pdf via --file)
    ingest <path>        recursively ingest a file or folder of documents
    list                 list document metadata (no content)
    get <id>             fetch one full document (or a line range)
    search <pattern>     regex grep over content, with metadata filters
"""

import argparse
import json
import os
import re
import sys

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

STORE = os.environ.get("SWIFTSEEK_STORE", "swiftseek.json")


# ----------------------------------------------------------------------------- #
# store + output helpers
# ----------------------------------------------------------------------------- #
def emit(payload, code=0):
    """Print JSON to stdout and exit with the given code. Single exit point."""
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    sys.exit(code)


def load():
    """Read the whole store into memory. Exits 2 if it doesn't exist."""
    if not os.path.exists(STORE):
        emit({"error": f"no store at {STORE!r}; run `init` first"}, code=2)
    try:
        with open(STORE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        emit({"error": f"cannot_read_store: {e}"}, code=2)


def save(data):
    """Atomic write: dump to a temp file, then replace the store."""
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, STORE)


def _snippet(text, max_chars, span=None):
    """Cap a line at max_chars. Returns (text, truncated_bool).

    Long extracted "lines" (whole paragraphs/documents) would otherwise drag huge
    blobs into context. When the line is too long and we know the match position
    (span), centre the window on it; otherwise truncate from the start. <=0 = off.
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


def _meta_match(doc, args):
    """Apply the metadata filters (the in-memory equivalent of a SQL WHERE)."""
    if getattr(args, "id_filter", None) is not None and doc["id"] != args.id_filter:
        return False
    if getattr(args, "doc_type", None) and doc.get("doc_type") != args.doc_type:
        return False
    if getattr(args, "filepath", None) and args.filepath not in (doc.get("filepath") or ""):
        return False
    return True


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

    def emit(text):
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
            emit("".join(out))
        elif c == "<" and i + 1 < n and s[i + 1] != "<":     # <hex string>
            j = s.find(">", i)
            if j == -1:
                break
            hexs = "".join(ch for ch in s[i + 1:j] if ch in "0123456789abcdefABCDEF")
            if len(hexs) % 2:
                hexs += "0"
            try:
                emit(bytes.fromhex(hexs).decode("latin-1", "replace"))
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
# init
# ----------------------------------------------------------------------------- #
def cmd_init(_args):
    if not os.path.exists(STORE):
        save({"next_id": 1, "documents": []})
    emit({"ok": True, "action": "init", "store": os.path.abspath(STORE)})


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

    data = load()
    doc_id = data.get("next_id", 1)
    data["next_id"] = doc_id + 1
    data["documents"].append({
        "id": doc_id,
        "filepath": filepath,
        "title": args.title or os.path.basename(filepath),
        "doc_type": args.doc_type,
        "content": content,
        "char_count": len(content),
    })
    save(data)
    emit({"ok": True, "action": "add", "id": doc_id,
          "filepath": filepath, "char_count": len(content)})


# ----------------------------------------------------------------------------- #
# ingest  (walk a file/folder, extract text, add each — idempotent by filepath)
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
    data = load()
    seen = {d["filepath"] for d in data["documents"]}
    added, skipped, failed = [], [], []
    for path in _iter_files(args.path, not args.no_recursive, SUPPORTED_EXTS):
        ap = os.path.abspath(path)
        if ap in seen:
            if not args.reindex:
                skipped.append(ap)
                continue
            data["documents"] = [d for d in data["documents"] if d["filepath"] != ap]
        try:
            content = extract_text(path)
        except ExtractError as e:
            failed.append({"filepath": ap, "error": str(e)})
            continue
        if not content.strip():
            failed.append({"filepath": ap,
                           "error": "no_text_extracted (empty / image-only PDF?)"})
            continue
        doc_id = data.get("next_id", 1)
        data["next_id"] = doc_id + 1
        data["documents"].append({
            "id": doc_id,
            "filepath": ap,
            "title": os.path.basename(ap),
            "doc_type": args.doc_type,
            "content": content,
            "char_count": len(content),
        })
        seen.add(ap)
        added.append({"id": doc_id, "filepath": ap, "char_count": len(content)})
    save(data)
    emit({"ok": True, "action": "ingest",
          "added": len(added), "skipped": len(skipped), "failed": len(failed),
          "added_docs": added, "skipped_paths": skipped, "failed_docs": failed},
         code=0 if (added or skipped) else 1)


# ----------------------------------------------------------------------------- #
# list
# ----------------------------------------------------------------------------- #
def cmd_list(args):
    data = load()
    rows = []
    for doc in data["documents"]:
        if not _meta_match(doc, args):
            continue
        rows.append({k: doc[k] for k in ("id", "filepath", "title", "doc_type",
                                         "char_count")})
        if len(rows) >= args.limit:
            break
    emit({"count": len(rows), "documents": rows}, code=0 if rows else 1)


# ----------------------------------------------------------------------------- #
# get
# ----------------------------------------------------------------------------- #
def cmd_get(args):
    data = load()
    doc = next((d for d in data["documents"] if d["id"] == args.id), None)
    if not doc:
        emit({"error": f"no document with id {args.id}"}, code=1)
    doc = dict(doc)
    if args.line_range:
        lines = doc["content"].splitlines()
        start, end = args.line_range  # 1-based inclusive
        doc["content"] = "\n".join(lines[max(0, start - 1):end])
        doc["line_range"] = [start, end]
        doc["total_lines"] = len(lines)
    emit({"document": doc})


# ----------------------------------------------------------------------------- #
# search  (metadata filter -> regex grep -> capped context windows)
# ----------------------------------------------------------------------------- #
def cmd_search(args):
    flags = re.IGNORECASE if args.ignore_case else 0
    pat = re.escape(args.pattern) if args.fixed else args.pattern
    try:
        rx = re.compile(pat, flags)
    except re.error as e:
        emit({"error": f"bad_regex: {e}"}, code=2)

    data = load()
    candidates = [d for d in data["documents"] if _meta_match(d, args)]

    results = []
    total_matches = 0
    budget_used = 0
    truncated = False
    for doc in candidates:
        lines = doc["content"].splitlines()
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
                "id": doc["id"],
                "filepath": doc["filepath"],
                "title": doc["title"],
                "match_count": len(doc_matches),
                "matches": doc_matches,
            })
        if truncated or len(results) >= args.max_docs:
            break

    payload = {
        "pattern": args.pattern,
        "ignore_case": args.ignore_case,
        "docs_searched": len(candidates),
        "docs_matched": len(results),
        "total_matches": total_matches,
        "approx_output_chars": budget_used,
        "truncated": truncated,
        "results": results,
    }
    if truncated:
        payload["hint"] = ("Output hit the ~max-output-chars budget and stopped "
                           "early; more matches likely exist. Narrow the query "
                           "(more specific pattern / metadata filters), or pass a "
                           "larger --max-output-chars if you really need it all.")
    elif not results:
        payload["hint"] = ("No lexical match. Expand with synonyms / regex "
                           "alternation (e.g. 'a|b|c'), add -i, or loosen metadata "
                           "filters. If still empty, the question may be conceptual "
                           "rather than lexical.")
    emit(payload, code=0 if results else 1)


# ----------------------------------------------------------------------------- #
# CLI
# ----------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        prog="swiftseek-lite.py",
        description="grep-style search over text documents in a JSON file "
                    "(zero-dependency agent tool).")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create an empty store")

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
                     help="re-extract files already in the store (replace them) "
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
                        "match (default 300, 0 = unlimited)")
    s.add_argument("--max-output-chars", type=int, default=40000,
                   dest="max_output_chars",
                   help="approx total char budget for the result; stops early and "
                        "sets truncated=true when exceeded (default 40000)")
    # metadata filters = the WHERE clause
    s.add_argument("--doc-type", dest="doc_type")
    s.add_argument("--filepath", help="substring filter on filepath")
    s.add_argument("--id", dest="id_filter", type=int,
                   help="restrict to a single document id")
    return p


DISPATCH = {
    "init": cmd_init,
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