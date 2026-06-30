# AgentSwiftSeek

**Give an AI agent a search tool, not a search engine.**

AgentSwiftSeek is a tiny, fast search tool for your documents, built for AI agents. Instead of the usual "embed everything into a vector database" setup, it lets an agent search your documents the way a person would search a folder of files: look for a word, read around the matches, refine the search, repeat. It's deliberately small, cheap to run, and easy to understand.

It comes in two flavors:

- **`docgrep.py`** — backed by MySQL, for when your documents already live in (or belong in) a database with searchable metadata.
- **`docgrep_lite.py`** — zero dependencies, nothing to install, stores everything in a single JSON file. Great for a quick start, a prototype, or a small private collection.

Both expose the exact same commands and behave identically, so you can start on the lite version and graduate to MySQL later without changing how your agent uses it.

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

That's the whole idea.

---

## How it compares

### vs. a regular RAG / vector-database setup

| | Vector RAG | AgentSwiftSeek |
|---|---|---|
| **Setup** | Embedding model + vector database + chunking strategy to tune | One script. Lite version installs nothing. |
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

## Quick start

**The zero-setup version:**

```bash
# nothing to install — Python's standard library only
export DOCGREP_STORE=./docgrep.json
python docgrep_lite.py init
python docgrep_lite.py add --file policy_001.txt --doc-type policy
python docgrep_lite.py search "flood"
```

**The MySQL version:**

```bash
pip install pymysql
export DOCGREP_HOST=localhost DOCGREP_USER=app DOCGREP_PASSWORD=secret DOCGREP_DB=docs
python docgrep.py migrate
python docgrep.py add --file policy_001.txt --doc-type policy
python docgrep.py search "flood"
```

Both scripts carry full instructions for an AI agent right at the top of the file — how to search, refine, and read results — so you can point your agent at the script and it will know how to use it.

> **Note:** AgentSwiftSeek searches plain text. Pulling the text out of PDFs and Word documents first is up to you (plenty of free libraries do this). Feed AgentSwiftSeek the extracted text.

---

## Status

Early and intentionally small. The goal is to stay lean and easy to read — if you can't understand the whole tool in one sitting, it's grown too big.

Contributions, ideas, and "this worked / didn't work for my documents" reports are all welcome.

## License

MIT — see `LICENSE`.