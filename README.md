# AgentSwiftSeek

**Give an AI agent a search tool, not a search engine.**

AgentSwiftSeek is a tiny, ultra fast search tool for your documents, built for AI agents. Instead of the usual "embed everything into a vector database" setup, it lets an agent search your documents the way a person would search a folder of files: look for a word, read around the matches, refine the search, repeat. It's deliberately small, cheap to run, and easy to understand.

It comes in four flavors — same idea, same behavior, different home for your data:

| Flavor | Folder | Use it when |
|---|---|---|
| **Lite** (zero dependencies) | [`swiftseek-lite/`](swiftseek-lite/) | You want a quick start, a prototype, or a small private collection. Ingests once into a single JSON file; nothing to install. |
| **Instant** (no ingest) | [`swiftseek-instant/`](swiftseek-instant/) | You want zero setup and always-fresh results: it searches files **live on disk** on every query — no store to build or keep in sync. Best for small or frequently-changing folders. Zero dependencies. |
| **MySQL** | [`swiftseek-mysql/`](swiftseek-mysql/) | Your documents already live in (or belong in) a database with searchable metadata. |
| **PHP** | [`swiftseek-php/`](swiftseek-php/) | You're on PHP shared hosting (e.g. Mijndomein) and want to search against the server's native MySQL. It's an includable function library — drop it in, call its functions, no extra services. |

Lite and MySQL share an identical command-line interface (`init`/`migrate`, `add`, `ingest`, `list`, `get`, `search`), so you can start on Lite and graduate to MySQL later without changing how your agent drives it. **Instant** uses the same `search` / `list` / `get` but skips ingestion entirely — it reads the disk live, identifying files by path instead of an id. The PHP build offers the operations as plain PHP functions you `include`.

**Cached (Lite/MySQL/PHP) vs live (Instant):** the cached builds extract each document's text once at ingest time, so repeated searches are fast and nothing re-parses your PDFs; the trade-off is keeping the store in sync (`ingest --update`). Instant re-extracts whatever it scans on every search — always fresh, zero bookkeeping, but more work per query. Pick by corpus size and how often it changes.

All of them read your documents directly: **`.txt`/`.md`, `.docx`, and `.pdf` text extraction is built in** — you no longer have to extract the text yourself.

---

## Why this exists

For the last few years, the default way to let an AI answer questions about your documents has been **RAG with a vector database**: you chop every document into chunks, run each chunk through an embedding model to turn it into a list of numbers, and store those numbers in a specialized database. At query time you embed the question, find the "nearest" chunks, and hand them to the model.

It works — but it's a surprising amount of machinery, and in 2025–2026 a lot of teams building AI agents quietly discovered something uncomfortable: for many real tasks, **giving the agent a plain text-search tool and letting it search in a loop beats the whole vector-database stack.** Several well-known coding assistants ripped out their vector search and replaced it with `grep`, and reported it worked *better*, not just simpler. Research has since backed this up for document and memory search too.

The reason is subtle and worth understanding, because it tells you when this approach fits:

A vector search is **one shot**. It embeds your question, returns its best guesses, and that's it. If the guesses are wrong, it doesn't know they're wrong — embeddings always return *something* that looks plausible.

A search **tool in an agent loop** is different. The agent searches, looks at what came back, and decides what to do next: try a synonym, narrow to a date range, read the surrounding paragraph, give up and say "not found." That loop — the agent reasoning about its own search results — turns out to do most of the heavy lifting. The retrieval method matters less than people assumed.

AgentSwiftSeek is that search tool, packaged so an agent can drive it well.

---

## How it works, in plain terms

1. You load your documents in **whole** — no chopping into chunks. Each document keeps a bit of metadata: where it came from, a title, a type ("policy", "claim", "manual", whatever you like).
2. The agent searches for text using ordinary patterns (and can filter by that metadata first — "only search 2024 policies").
3. AgentSwiftSeek returns the matching lines **with the surrounding context**, as clean structured data, so the agent can read around a hit without pulling in the whole document.
4. If nothing matches, it says so clearly. The agent treats that as a cue to rephrase and try again — exactly like a person would.

That's the whole idea. Point the tool at a folder and it will pull the text out of your `.docx` and `.pdf` files for you (see [Documents & formats](#documents--formats)).

---

## How it compares

### vs. a regular RAG / vector-database setup

| | Vector RAG | AgentSwiftSeek |
|---|---|---|
| **Setup** | Embedding model + vector database + chunking strategy to tune | One file per flavor. Lite installs nothing. |
| **Cost** | Pay to embed everything, pay to host the vector DB | Effectively free to run |
| **Freshness** | Index goes stale until you re-embed | Always searches the current text |
| **Exact details** | Often fumbles codes, IDs, names, dollar amounts | Nails them — it's exact-match by nature |
| **When it's wrong** | Returns confident, plausible-looking wrong answers | Returns "no match" — honest and obvious |
| **Debuggability** | Hard to see *why* something was retrieved | You can see exactly what matched and where |

The headline: AgentSwiftSeek removes an entire layer of infrastructure (and its bill) while being *more* reliable for the kinds of facts business documents are full of — policy numbers, clause references, names, amounts, dates.

### vs. plain Linux `grep`

AgentSwiftSeek keeps everything that makes `grep` great — exact matching, speed, the "no match means no match" honesty — and adds the things `grep` lacks for this job:

- **Metadata to narrow by.** Filter to a document type or source folder *before* searching the text, so the agent isn't grepping the whole world.
- **A real home for your documents.** A queryable database (or one tidy file) instead of text scattered across a disk.
- **It reads your documents for you.** Hand it `.docx` and `.pdf` files directly; it extracts the text. Plain `grep` only sees bytes.
- **Output built for an agent, not a terminal.** Results come back as structured data with clear success/no-match signals the agent can act on.
- **Guardrails so it can't overwhelm the AI.** Real documents — especially text pulled out of PDFs and Word files — sometimes arrive as one gigantic unbroken paragraph. Plain `grep` would dump all of it. AgentSwiftSeek caps how much it returns and trims long lines to a snippet around the match, so a broad first search can never flood the agent's limited memory.

In short: AgentSwiftSeek is `grep`'s philosophy, dressed for working alongside an AI.

---

## When this is the right tool — and when it isn't

Be honest with yourself about your documents. AgentSwiftSeek shines when answers live in **specific words**: identifiers, named clauses, terms that actually appear in the text.

It is **not** a good fit when answers require understanding **meaning across different wording**. If someone asks *"am I covered if a pipe bursts?"* and the policy says *"sudden and accidental discharge of water"* — sharing not a single word — text search will miss it. A smart agent narrows that gap by trying synonyms, but it can't fully close it.

For those genuinely meaning-based questions, semantic/vector search still earns its place. Many of the best systems use **both**: AgentSwiftSeek-style search to find exact things and to filter down to the relevant documents, and semantic search for the fuzzy conceptual questions. The two aren't enemies — they cover different gaps.

If your documents are mostly structured, factual, and full of specific terms (contracts, policies, manuals, knowledge bases, logs, support docs), AgentSwiftSeek alone may be all you need.

---

## Repository layout

```
AgentSwiftSeek/
├── swiftseek-lite/        # zero-dependency, single JSON file — just the one script
│   └── swiftseek-lite.py
├── swiftseek-instant/     # zero-dependency, no store — searches the disk live
│   └── swiftseek-instant.py
├── swiftseek-mysql/       # MySQL-backed, Python CLI
│   ├── swiftseek-mysql.py
│   ├── requirements.txt   # pymysql (pypdf optional)
│   └── .env.example
├── swiftseek-php/         # PHP function library for shared hosting
│   ├── swiftseek-php.php
│   └── config.example.php # copy to config.php (git-ignored); PHP has no requirements.txt
└── README.md
```

Each script stays **one self-contained file**. Copy the folder you need; ignore the rest.

---

## Quick start

### Lite — the zero-setup version

```bash
cd swiftseek-lite
# nothing to install — Python's standard library only (Python 3.7+)
python swiftseek-lite.py init
python swiftseek-lite.py ingest ./docs --doc-type tag1   # a whole folder of pdf/docx/txt
python swiftseek-lite.py search "flood"
```

### Instant — no ingest, search the disk live

```bash
cd swiftseek-instant
# nothing to install — Python's standard library only (Python 3.7+)
export SWIFTSEEK_DIR=./docs                 # bash/macOS/Linux  (or pass --dir to each command)
python swiftseek-instant.py search "flood"  # walks ./docs live, extracts + greps
python swiftseek-instant.py list            # the files it would search
python swiftseek-instant.py get ./docs/policy_001.pdf   # results identify files by PATH
```

```powershell
# Windows PowerShell — same thing, PowerShell sets env vars differently:
cd swiftseek-instant
$env:SWIFTSEEK_DIR = "./docs"                # or pass --dir to each command
python swiftseek-instant.py search "flood"
```

No `init`, no `ingest` — there's no store. Each search re-reads the files on disk, so results are always current. Narrow with `--filepath <substring>` and `--ext pdf,docx`.

### MySQL — the database version

```bash
cd swiftseek-mysql
pip install -r requirements.txt            # just pymysql (pypdf optional)
cp .env.example .env                       # then edit .env: SWIFTSEEK_HOST/USER/PASSWORD/DB
python swiftseek-mysql.py migrate          # create the table once
python swiftseek-mysql.py ingest ./docs --doc-type tag1
python swiftseek-mysql.py search "flood"
```

> Commands above are shown for bash/macOS/Linux. They work as-is in **Windows PowerShell** too — `cp` is an alias for `Copy-Item` — with one exception: PowerShell sets environment variables as `$env:NAME = "value"` (not `export NAME=value`), and chains commands with `;` rather than `&&`. Credentials here come from the `.env` file, so no `export` is needed either way.

Each Python script carries full instructions for an AI agent right at the top of the file — how to search, refine, and read results — so you can point your agent at the script and it will know how to use it. Exit codes follow the `grep` convention: `0` = matches, `1` = no matches (a signal to refine), `2` = error.

Commands: Lite and MySQL share `init`/`migrate`, `add`, `ingest`, `list`, `get`, `search`; Instant has `search`, `list`, `get` (no ingest — it reads the disk live).

Search is **case-insensitive by default** — add `-s`/`--case-sensitive` (Python) or `['case_sensitive' => true]` (PHP) when you need exact case (e.g. to tell a code like `IT` apart from the word `it`). Each match is returned with a snippet of up to ~900 characters around it (`--max-line-chars`), bounded by a total response budget (`--max-output-chars`).

### PHP — the shared-hosting library

`swiftseek-php.php` is **not a CLI and not an API** — it's a library of functions you include from your own PHP. It defines functions and nothing else: it reads no `$_GET`/`$_POST`, prints nothing, and exposes no endpoint. If someone requests the file directly over HTTP it answers `404` and exits, so it's safe to drop into a public web root.

```php
<?php
require_once __DIR__ . '/swiftseek-php.php';

// Credentials: copy config.example.php -> config.php and fill it in (git-ignored).
$db = swiftseek_connect();                  // native mysqli, auto-loads ./config.php
// ...or pass them directly instead:
// $db = swiftseek_connect(['host'=>'localhost','user'=>'app','password'=>'secret','database'=>'docs']);

swiftseek_migrate($db);                                         // create the table once
swiftseek_ingest($db, __DIR__ . '/docs', ['doc_type' => 'tag1']);

$hits = swiftseek_search($db, 'flood|water damage|discharge');   // case-insensitive by default
header('Content-Type: application/json');
echo json_encode($hits, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE);
```

The functions return ordinary PHP arrays (`json_encode` them to get the same shape the Python CLIs print). A "no match" is not an error — `swiftseek_search()` returns an empty `results` array plus a `hint`, and `swiftseek_get()` returns `null`. Real problems (no DB, bad SQL, unreadable file, invalid regex) throw exceptions.

**What PHP needs on the server** (PHP's equivalent of `requirements.txt`): PHP 7.1+ (7.4 or newer recommended) with the standard `mysqli`, `zip`, `zlib`, and `mbstring` extensions enabled. There is **nothing to install per project** — no Composer packages, no `vendor/` directory. These are server-level extensions, standard on hosts like Mijndomein; check yours with `php -m`.

> **🔒 Why `config.php`, not `.env`.** Credentials go in `config.php`, which is safe by default: a PHP file is *executed* by the server, never sent as source, so opening `https://your-site/.../config.php` in a browser just shows a blank page — the password isn't exposed. (A `.env` file is *static*; Apache would hand it over as plain text — a leak. That's why this build doesn't use one.) `config.php` also refuses direct HTTP access (returns `404`). For maximum safety, keep the whole folder **above** your public web root and just `include` it.

---

## Documents & formats

Hand `add --file` and `ingest` (or `swiftseek_add`/`swiftseek_ingest` in PHP) your real files — extraction is built in, with as few dependencies as possible. (Instant uses the very same extraction, just live on each search instead of at ingest.)

| Format | How it's read | Dependency |
|---|---|---|
| `.txt`, `.md`, `.csv`, `.log`, … | read as UTF-8 | none |
| `.docx` | unzipped and parsed as XML | **none** (Python stdlib `zipfile`/`xml`; PHP `zip` extension) |
| `.pdf` | content streams inflated and text operators parsed | **none required** (Python stdlib `zlib`; PHP `zlib` extension) |

- **`ingest <folder>`** walks a file or directory (recursively by default), extracts each supported file, and stores it. It's **idempotent**: files already stored (matched by absolute path) are **skipped** by default. Pass `--update` (Python) / `['update' => true]` (PHP) to re-extract only files whose source is **newer** than the stored copy (incremental sync — it records each file's modification time), or `--reindex` / `['reindex' => true]` to re-extract **all** stored files regardless of age. A re-ingest keeps the existing `doc_type` unless you give a new one. The result counts `added` / `updated` / `skipped` / `failed`.
- **PDF extraction is best effort.** The built-in extractor handles common text PDFs well, but **scanned/image-only PDFs (no text layer) and exotic font encodings can come back empty or garbled.** `ingest` reports empty extraction as a *failure* rather than silently storing nothing — treat that as a signal. For tricky PDFs in the Python builds, installing **`pypdf`** (optional) upgrades extraction automatically; it is never required.
- **Legacy `.doc`** (old binary Word) is not supported — convert to `.docx` or `.txt` first.

---

## Metadata & filtering

Every document carries three optional pieces of metadata. They are **just labels** — they have no built-in meaning and aren't validated — and their only job is to let an agent **narrow the search before grepping the text** (the equivalent of a SQL `WHERE`):

| Field | What it is | How it's used |
|---|---|---|
| **`doc_type`** | A free-form tag *you* invent — any string at all (`tag1`, `tag2`, … or a real scheme like `invoice` / `contract` / `nl`). Optional; defaults to none. | **Exact-match** filter. `--doc-type tag1` matches only documents tagged exactly `tag1` (case-sensitive). |
| **`filepath`** | The source path, recorded automatically on `add`/`ingest`. | **Substring** filter. `--filepath 2024/` matches any path containing `2024/`. |
| **`title`** | A human label; defaults to the file's basename. | Shown in results; not a search filter. |

So `--doc-type` is **simply a tag** — `tag1` here is just a placeholder; pick whatever names suit your collection. Tag documents at ingest time, then scope a search to that group:

```bash
python swiftseek-lite.py ingest ./docs --doc-type tag1
python swiftseek-lite.py search "deductible" --doc-type tag1   # only the 'tag1' docs
```

In PHP it's the same idea: `swiftseek_ingest($db, $dir, ['doc_type' => 'tag1'])` then `swiftseek_search($db, 'deductible', ['doc_type' => 'tag1'])`. Omit `doc_type` entirely and every document is searched.

---

## Configuration & secrets

- **Python:** a `.env` file, *if present*, is auto-loaded at startup — first found of `$SWIFTSEEK_ENV`, `./.env`, or a `.env` next to the script; real environment variables always win over it. **Lite** needs no config to run and ships no `.env.example` (its store defaults to `./swiftseek.json`; set `SWIFTSEEK_STORE` to move it). **Instant** also runs with no config (search dir defaults to `.`; set `SWIFTSEEK_DIR` or pass `--dir`). **MySQL** needs `SWIFTSEEK_HOST/PORT/USER/PASSWORD/DB` — copy its `.env.example` to `.env` to begin.
- **PHP:** copy `config.example.php` to `config.php` and fill it in — `swiftseek_connect()` auto-loads it. (Or pass credentials straight to `swiftseek_connect([...])`.) See the `config.php` note in the PHP quick start above for why this is safer than `.env`.
- **Never commit real secrets.** The repo `.gitignore` already excludes the Python `.env` files and the PHP `config.php` (keeping the `.example` templates) plus the Lite JSON store. Run the database under a user scoped to just the `documents` table — these tools only `SELECT`/`INSERT`.

---

## Status

Early and intentionally small. The goal is to stay lean and easy to read — if you can't understand the whole tool in one sitting, it's grown too big.

Contributions, ideas, and "this worked / didn't work for my documents" reports are all welcome.

## Author
Joey van Griethuijsen, 
freelance AI & Software Solutions Architect

📧 Contact: [info@joeyvg.nl]

🌐 Website: [https://joeyvg.nl/en]

## License
MIT License
