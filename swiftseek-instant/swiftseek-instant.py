#!/usr/bin/env python3
"""
swiftseek-instant.py — grep-style document search with NO ingest and NO cache.

Same agent interface and output shape as swiftseek-lite.py, but there is no store to
build and nothing to keep in sync: you point it at a directory and every `search`
walks the files LIVE on disk, extracts their text on the fly, and greps them. There is
no swiftseek.json, no database, no `add`/`ingest`/`migrate` step.

WHEN TO USE THIS (vs lite / mysql)
----------------------------------
  * Use INSTANT when the corpus is small-ish and/or changes often and you always want
    fresh results with zero bookkeeping — edit a file, search again, done. Nothing can
    go stale because nothing is cached.
  * Use LITE/MYSQL when the corpus is larger or searched frequently: they extract each
    document ONCE at ingest time, so repeated searches don't re-parse every PDF/DOCX.
    INSTANT re-extracts everything it scans on EVERY search, which is simple and always
    correct but does more work per query (PDF parsing especially).

WHY THIS EXISTS (read this first, agent)
----------------------------------------
Reproduces the "grep beats vector RAG inside an agent loop" pattern, pointed straight
at a folder of files.
  * WHOLE document. No chunking. grep finds a position; YOU expand context on demand.
  * LEXICAL search via real regex over the extracted text — never drops short codes
    like "HO3" the way tokenized indexes do.
  * FAILS LOUDLY. No match => empty result + exit code 1, never plausible-but-wrong
    neighbours like a vector store.

HOW TO USE IT IN AN AGENT LOOP
------------------------------
Set the directory once (env var), then search. Every command prints JSON to stdout.
Exit codes (grep convention): 0 = matches, 1 = no matches (a SIGNAL to refine), 2 = error.

    export SWIFTSEEK_DIR=./docs            # bash/macOS/Linux (or pass --dir to each command)
    # Windows PowerShell:  $env:SWIFTSEEK_DIR = "./docs"
    python swiftseek-instant.py search "flood"

Search is CASE-INSENSITIVE by default; pass -s/--case-sensitive for exact case.

  1. SEARCH with your best literal/regex guess:
         python swiftseek-instant.py search "flood" --dir ./docs
  2. If exit code == 1, expand the query — YOU supply the semantics the lexical layer
     lacks:
         python swiftseek-instant.py search "flood|water damage|discharge|seepage"
  3. Narrow to a subtree/filename with --filepath (substring) or a file type with --ext:
         python swiftseek-instant.py search "deductible" --filepath 2024/ --ext pdf,docx
  4. When a match looks relevant, READ AROUND IT (results identify files by PATH, not id):
         python swiftseek-instant.py search "burst pipe" -C 8
         python swiftseek-instant.py get ./docs/policy_001.pdf
         python swiftseek-instant.py get ./docs/policy_001.pdf --line-range 80 140
  5. Answer ONLY from text you retrieved. If search stays empty after reasonable synonym
     expansion, the answer is likely conceptual, not lexical — say so rather than inventing it.

OUTPUT IS CAPPED so a broad first search can't blow your context window (identical to the
other builds): --max-docs (20), --max-matches (5/doc), --max-line-chars (900, snippeted
around the match), --max-output-chars (40000 ≈ ~10k tokens, a hard budget that stops early
and sets "truncated": true). Every cap is an override.

TEXT EXTRACTION (built in — few/zero dependencies)
--------------------------------------------------
Reads .txt/.md (and similar), .docx, and .pdf:
  * .docx — unzipped + parsed with the standard library only (zipfile + xml).
  * .pdf  — a built-in, pure-stdlib extractor (zlib) handles common text PDFs. If
            `pypdf` is installed it is used automatically for higher fidelity, but it
            is NOT required.
PDF extraction is best effort: scanned/image-only PDFs and exotic font encodings can
come back empty or garbled. Files that fail extraction are listed under "unreadable".

LIMITATION TO RESPECT: lexical search matches strings, not meaning. Query expansion
narrows that gap but doesn't close it; for conceptual questions grep is the wrong tool.

COMMANDS
--------
    search <pattern>     regex grep over files live on disk, under the search dir
    list                 list the files that would be searched (no content)
    get <path>           extract one file's full text (or a line range)
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
    to this script. KEY=VALUE lines (optional `export ` prefix; #-comments and blank
    lines ignored; surrounding quotes stripped). Real environment variables win.
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
            pass
        return


_load_dotenv()


# ----------------------------------------------------------------------------- #
# output helper
# ----------------------------------------------------------------------------- #
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
# shared helpers
# ----------------------------------------------------------------------------- #
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


def _safe_mtime(path):
    """Source file modification time (epoch float), or None if it can't be read."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _exts(args):
    """The set of extensions to scan: --ext overrides the default supported set."""
    if getattr(args, "ext", None):
        return {"." + e.strip().lstrip(".").lower()
                for e in args.ext.split(",") if e.strip()}
    return SUPPORTED_EXTS


def _iter_files(root, recursive, exts):
    """Yield searchable file paths under `root` (a file or a directory)."""
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


def _resolve_dir(args):
    """The directory to search: --dir, else $SWIFTSEEK_DIR, else the cwd."""
    root = args.dir or os.environ.get("SWIFTSEEK_DIR") or "."
    if not os.path.exists(root):
        emit({"error": f"search dir not found: {root!r} "
                       "(set --dir or SWIFTSEEK_DIR)"}, code=2)
    return root


# ----------------------------------------------------------------------------- #
# search  (walk the dir -> extract each file live -> regex grep -> capped windows)
# ----------------------------------------------------------------------------- #
def cmd_search(args):
    root = _resolve_dir(args)
    ignore_case = not args.case_sensitive
    flags = re.IGNORECASE if ignore_case else 0
    pat = re.escape(args.pattern) if args.fixed else args.pattern
    try:
        rx = re.compile(pat, flags)
    except re.error as e:
        emit({"error": f"bad_regex: {e}"}, code=2)

    results = []
    files_searched = 0
    total_matches = 0
    budget_used = 0
    truncated = False
    unreadable = []
    for path in _iter_files(root, not args.no_recursive, _exts(args)):
        ap = os.path.abspath(path)
        if args.filepath and args.filepath not in ap:
            continue
        files_searched += 1
        try:
            content = extract_text(path)
        except ExtractError:
            unreadable.append(ap)
            continue

        lines = content.splitlines()
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
                "filepath": ap,
                "title": os.path.basename(ap),
                "match_count": len(doc_matches),
                "matches": doc_matches,
            })
        if truncated or len(results) >= args.max_docs:
            break

    payload = {
        "pattern": args.pattern,
        "dir": os.path.abspath(root),
        "ignore_case": ignore_case,
        "files_searched": files_searched,
        "files_matched": len(results),
        "total_matches": total_matches,
        "approx_output_chars": budget_used,
        "truncated": truncated,
        "results": results,
    }
    if unreadable:
        payload["unreadable"] = unreadable
    if truncated:
        payload["hint"] = ("Output hit the ~max-output-chars budget and stopped early; "
                           "more matches likely exist. Narrow the query (more specific "
                           "pattern, --filepath / --ext), or pass a larger "
                           "--max-output-chars if you really need it all.")
    elif not results:
        payload["hint"] = ("No lexical match. Expand with synonyms / regex alternation "
                           "(e.g. 'a|b|c') or loosen --filepath/--ext (matching is "
                           "already case-insensitive). If still empty, the question may "
                           "be conceptual rather than lexical.")
    emit(payload, code=0 if results else 1)


# ----------------------------------------------------------------------------- #
# list  (the files that would be searched, no content)
# ----------------------------------------------------------------------------- #
def cmd_list(args):
    root = _resolve_dir(args)
    rows = []
    for path in _iter_files(root, not args.no_recursive, _exts(args)):
        ap = os.path.abspath(path)
        if args.filepath and args.filepath not in ap:
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            size = None
        rows.append({"filepath": ap, "title": os.path.basename(ap),
                     "size_bytes": size, "mtime": _safe_mtime(path)})
        if len(rows) >= args.limit:
            break
    emit({"dir": os.path.abspath(root), "count": len(rows), "files": rows},
         code=0 if rows else 1)


# ----------------------------------------------------------------------------- #
# get  (one file's full extracted text, or a line range)
# ----------------------------------------------------------------------------- #
def cmd_get(args):
    if not os.path.isfile(args.path):
        emit({"error": f"no such file: {args.path!r}"}, code=1)
    try:
        content = extract_text(args.path)
    except ExtractError as e:
        emit({"error": str(e)}, code=2)
    doc = {
        "filepath": os.path.abspath(args.path),
        "title": os.path.basename(args.path),
        "char_count": len(content),
        "content": content,
    }
    if args.line_range:
        lines = content.splitlines()
        start, end = args.line_range  # 1-based inclusive
        doc["content"] = "\n".join(lines[max(0, start - 1):end])
        doc["line_range"] = [start, end]
        doc["total_lines"] = len(lines)
    emit({"document": doc})


# ----------------------------------------------------------------------------- #
# CLI
# ----------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        prog="swiftseek-instant.py",
        description="grep-style search over files live on disk, no ingest/cache "
                    "(zero-dependency agent tool).")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="regex grep over files live on disk")
    s.add_argument("pattern", help="regex (or literal with -F)")
    s.add_argument("--dir", help="directory to search (default: $SWIFTSEEK_DIR or .)")
    s.add_argument("-i", "--ignore-case", action="store_true", dest="ignore_case",
                   help=argparse.SUPPRESS)  # back-compat; insensitive is the default
    s.add_argument("-s", "--case-sensitive", action="store_true", dest="case_sensitive",
                   help="case-sensitive matching (default: case-insensitive)")
    s.add_argument("-F", "--fixed", action="store_true",
                   help="treat pattern as a literal string, not regex")
    s.add_argument("-C", "--context", type=int, default=2,
                   help="lines of context around each match (default 2)")
    s.add_argument("--max-matches", type=int, default=5,
                   help="max matches reported per file (default 5)")
    s.add_argument("--max-docs", type=int, default=20,
                   help="max files reported (default 20)")
    s.add_argument("--max-line-chars", type=int, default=900, dest="max_line_chars",
                   help="cap per emitted line; long lines are snippeted around the "
                        "match (default 900, 0 = unlimited)")
    s.add_argument("--max-output-chars", type=int, default=40000,
                   dest="max_output_chars",
                   help="approx total char budget for the result; stops early and "
                        "sets truncated=true when exceeded (default 40000)")
    s.add_argument("--filepath", help="substring filter on the file path")
    s.add_argument("--ext", help="comma-separated extensions to scan (e.g. pdf,docx); "
                                 "default: txt/md/.../pdf/docx")
    s.add_argument("--no-recursive", action="store_true",
                   help="do not descend into subdirectories")

    ls = sub.add_parser("list", help="list the files that would be searched")
    ls.add_argument("--dir", help="directory to search (default: $SWIFTSEEK_DIR or .)")
    ls.add_argument("--filepath", help="substring filter on the file path")
    ls.add_argument("--ext", help="comma-separated extensions to list")
    ls.add_argument("--no-recursive", action="store_true")
    ls.add_argument("--limit", type=int, default=1000)

    g = sub.add_parser("get", help="extract one file's full text by path")
    g.add_argument("path", help="path to a .txt/.md/.docx/.pdf file")
    g.add_argument("--line-range", nargs=2, type=int, metavar=("START", "END"),
                   help="1-based inclusive line range to return instead of full text")
    return p


DISPATCH = {
    "search": cmd_search,
    "list": cmd_list,
    "get": cmd_get,
}


def main():
    args = build_parser().parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
