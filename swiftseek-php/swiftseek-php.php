<?php
/**
 * swiftseek-php.php — grep-style document search for PHP + native MySQL.
 *
 * This is the PHP sibling of swiftseek-mysql.py / swiftseek-lite.py. Same idea:
 * reproduce the "grep beats vector RAG inside an agent loop" pattern for plain text
 * documents (policies, manuals, contracts — NOT code). Whole documents per row, real
 * regex (PCRE) over the raw content, metadata filtered first in SQL, fails loudly
 * (empty results, never plausible-but-wrong neighbours).
 *
 * WHY A FUNCTION LIBRARY (read this first)
 * ----------------------------------------
 * Unlike the Python builds (CLI tools), this file is a *library*: it only DEFINES
 * functions. It has NO entry point, reads no $_GET/$_POST, prints nothing, and routes
 * nothing. Dropping it on a public web root exposes no API surface — and if someone
 * requests it directly over HTTP it answers 404 and exits (guard at the bottom of
 * this header). You include it from your own code and call the functions:
 *
 *     require_once __DIR__ . '/swiftseek-php.php';
 *     $db = swiftseek_connect();                       // auto-loads ./config.php
 *     swiftseek_migrate($db);                          // create the table once
 *     swiftseek_ingest($db, '/path/to/docs', ['doc_type' => 'tag1']);
 *     $hits = swiftseek_search($db, 'flood|water damage|discharge', ['ignore_case' => true]);
 *     header('Content-Type: application/json');
 *     echo json_encode($hits, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE);
 *
 * Functions return ordinary PHP arrays/values (no exit codes) — json_encode them if
 * you want the same shape the Python CLIs print. Infrastructure problems (no DB, bad
 * SQL, unreadable file, invalid regex) throw RuntimeException / InvalidArgumentException;
 * a "no match" is NOT an error — search() returns a payload with empty `results` and
 * a `hint`, and get() returns null.
 *
 * CREDENTIALS — use config.php, NOT a .env file
 * ---------------------------------------------
 * Copy config.example.php to config.php and fill in your MySQL credentials (it
 * `return`s an array). swiftseek_connect() auto-loads ./config.php when you call it
 * with no arguments. config.php is the safe choice on shared hosting: PHP files are
 * EXECUTED by the server, never sent as source, so opening it in a browser shows a
 * blank page — the password is not exposed. (A .env file is STATIC and Apache would
 * serve it as plain text — a leak. That's why this build uses config.php.) You can
 * also pass a config array directly: swiftseek_connect(['host'=>..., 'user'=>..., ...]),
 * or point at a specific file: swiftseek_connect(['config' => '/path/config.php']).
 * Keep config.php out of version control (the repo .gitignore already excludes it),
 * and ideally keep this whole folder ABOVE the public web root.
 *
 * NATIVE MYSQL ON SHARED HOSTING (e.g. Mijndomein)
 * ------------------------------------------------
 * swiftseek_connect() uses ext/mysqli talking to the local MySQL server (localhost),
 * which is exactly what shared hosts give you — no network service, no API. Run under
 * a DB user scoped to just the documents table.
 *
 * TEXT EXTRACTION (built in — no Composer packages)
 * -------------------------------------------------
 * swiftseek_add(['file'=>...]) and swiftseek_ingest() accept .txt/.md (and similar),
 * .docx, and .pdf:
 *   * .docx — unzipped with ext/zip (ZipArchive) and the XML stripped to text.
 *   * .pdf  — a built-in extractor inflates content streams with ext/zlib
 *             (gzuncompress / gzinflate) and pulls text out of the PDF operators.
 * PDF extraction is best effort: scanned/image-only PDFs (no text layer) and exotic
 * font encodings can come back empty or garbled. ingest() reports empty extraction as
 * a failure rather than swallowing it — treat that as a signal, not success.
 *
 * METADATA (optional, filter-only — not validated, no built-in meaning)
 * ---------------------------------------------------------------------
 * Each document carries doc_type, filepath and title. They exist only to narrow a
 * search (the SQL WHERE clause):
 *   * doc_type  a free-form tag YOU invent — any string at all, e.g. 'tag1', 'tag2',
 *               or a real scheme like 'invoice' / 'contract'. EXACT-match filter:
 *               ['doc_type' => 'tag1'].
 *   * filepath  source path, recorded automatically; SUBSTRING filter ['filepath'=>..].
 *   * title     human label (defaults to the file's basename); shown, not filtered.
 * Tag at add/ingest time, then scope searches to that tag. Omit it to search all.
 *
 * REQUIREMENTS (this is PHP's "requirements.txt" — server config, not a pip file)
 * --------------------------------------------------------------------------------
 * PHP 7.1+ (7.4 or newer recommended) with these standard extensions enabled:
 *   mysqli    the database connection (native MySQL driver)
 *   zip       .docx extraction (ZipArchive)
 *   zlib      .pdf stream inflation (gzuncompress / gzinflate)
 *   mbstring  UTF-8 safe search snippets and char counts
 * There is NOTHING to install per project: no Composer packages, no vendor/ dir.
 * These extensions are server-level and standard on shared hosts like Mijndomein
 * (check yours with `php -m` or a phpinfo() page).
 *
 * LIMITATION TO RESPECT: lexical search matches strings, not meaning. "Am I covered
 * if a pipe bursts?" will not match "sudden and accidental discharge of water" on
 * tokens. Expanding the pattern with synonyms narrows the gap but never fully closes
 * it — for genuinely conceptual questions, grep is the wrong tool.
 */

// --------------------------------------------------------------------------- //
// Safety: if this file is requested directly over the web, expose nothing.
// When included from another script, $_SERVER['SCRIPT_FILENAME'] is that script,
// so this guard is false and we simply go on to define the functions.
// --------------------------------------------------------------------------- //
if (PHP_SAPI !== 'cli'
    && isset($_SERVER['SCRIPT_FILENAME'])
    && @realpath($_SERVER['SCRIPT_FILENAME']) === @realpath(__FILE__)) {
    http_response_code(404);
    exit;
}

// Guard against double declaration if the file is include()d more than once.
if (!function_exists('swiftseek_connect')) {

    /** Resolve a config value: explicit $opts -> environment -> default. */
    function swiftseek__opt(array $opts, string $key, string $env, $default)
    {
        if (array_key_exists($key, $opts) && $opts[$key] !== null && $opts[$key] !== '') {
            return $opts[$key];
        }
        $v = getenv($env);
        if ($v !== false && $v !== '') {
            return $v;
        }
        return $default;
    }

    /**
     * Load a config.php (which `return`s an array) and return that array, or [] if
     * the file is absent/invalid. $path defaults to config.php next to this library.
     */
    function swiftseek_config(?string $path = null): array
    {
        $path = $path ?? (__DIR__ . DIRECTORY_SEPARATOR . 'config.php');
        if (!is_file($path)) {
            return [];
        }
        $loaded = require $path;
        return is_array($loaded) ? $loaded : [];
    }

    // ----------------------------------------------------------------------- //
    // connection
    // ----------------------------------------------------------------------- //
    /**
     * Open a native mysqli connection. With no $opts, auto-loads ./config.php (see
     * config.example.php). Pass $opts to override: host, port, user, password,
     * database — or 'config' => '/path/to/config.php' to load a specific file. Any
     * key still missing falls back to a SWIFTSEEK_* environment variable, then a
     * default. Throws on failure.
     */
    function swiftseek_connect(array $opts = []): mysqli
    {
        if (!function_exists('mysqli_connect')) {
            throw new RuntimeException('the mysqli extension is required but not loaded');
        }
        if (isset($opts['config'])) {                 // explicit config file path
            $opts = array_merge(swiftseek_config($opts['config']), $opts);
            unset($opts['config']);
        } elseif (!$opts) {                           // default: ./config.php if present
            $opts = swiftseek_config();
        }
        $host = swiftseek__opt($opts, 'host', 'SWIFTSEEK_HOST', 'localhost');
        $user = swiftseek__opt($opts, 'user', 'SWIFTSEEK_USER', 'root');
        $pass = swiftseek__opt($opts, 'password', 'SWIFTSEEK_PASSWORD', '');
        $db   = swiftseek__opt($opts, 'database', 'SWIFTSEEK_DB', '');
        $port = (int) swiftseek__opt($opts, 'port', 'SWIFTSEEK_PORT', 3306);

        // We check errors explicitly below, so disable mysqli's own exceptions
        // (PHP 8.1+ defaults them on) for predictable behaviour.
        mysqli_report(MYSQLI_REPORT_OFF);
        $conn = @new mysqli($host, $user, $pass, $db, $port);
        if ($conn->connect_errno) {
            throw new RuntimeException('db_connection_failed: ' . $conn->connect_error);
        }
        $conn->set_charset('utf8mb4');
        return $conn;
    }

    /** Bind a dynamic parameter list onto a prepared statement (by reference). */
    function swiftseek__bind(mysqli_stmt $stmt, string $types, array $params): void
    {
        if ($types === '') {
            return;
        }
        $refs = [$types];
        foreach (array_keys($params) as $k) {
            $refs[] = &$params[$k];
        }
        call_user_func_array([$stmt, 'bind_param'], $refs);
    }

    /** Build the metadata WHERE clause (parameterised — no SQL injection). */
    function swiftseek__meta_filters(array $f): array
    {
        $where = [];
        $types = '';
        $params = [];
        if (isset($f['id']) && $f['id'] !== null && $f['id'] !== '') {
            $where[] = 'id = ?';
            $types .= 'i';
            $params[] = (int) $f['id'];
        }
        if (!empty($f['doc_type'])) {
            $where[] = 'doc_type = ?';
            $types .= 's';
            $params[] = $f['doc_type'];
        }
        if (!empty($f['filepath'])) {
            $where[] = 'filepath LIKE ?';
            $types .= 's';
            $params[] = '%' . $f['filepath'] . '%';
        }
        return [$where, $types, $params];
    }

    // ----------------------------------------------------------------------- //
    // migrate
    // ----------------------------------------------------------------------- //
    /** Create the documents table (idempotent). */
    function swiftseek_migrate(mysqli $conn): array
    {
        $ddl = 'CREATE TABLE IF NOT EXISTS documents (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            filepath    VARCHAR(1024) NOT NULL,
            title       VARCHAR(512)  NULL,
            doc_type    VARCHAR(64)   NULL,
            content     MEDIUMTEXT    NOT NULL,
            char_count  INT           NOT NULL DEFAULT 0,
            mtime       DOUBLE        NULL,
            created_at  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
            KEY idx_doc_type (doc_type),
            KEY idx_filepath (filepath(255))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4';
        if (!$conn->query($ddl)) {
            throw new RuntimeException('migrate_failed: ' . $conn->error);
        }
        // Add mtime to a pre-existing table that lacks it (idempotent).
        $res = $conn->query("SELECT COUNT(*) AS c FROM information_schema.columns "
            . "WHERE table_schema = DATABASE() AND table_name = 'documents' "
            . "AND column_name = 'mtime'");
        $row = $res ? $res->fetch_assoc() : null;
        if ($row && (int) $row['c'] === 0) {
            $conn->query('ALTER TABLE documents ADD COLUMN mtime DOUBLE NULL AFTER char_count');
        }
        return ['ok' => true, 'action' => 'migrate', 'detail' => 'documents table ready'];
    }

    // ----------------------------------------------------------------------- //
    // add  ($doc keys: file | content, filepath, title, doc_type)
    // ----------------------------------------------------------------------- //
    /** Insert one document. Pass 'file' (text/.docx/.pdf) or raw 'content'. */
    function swiftseek_add(mysqli $conn, array $doc): array
    {
        $file = $doc['file'] ?? null;
        $content = $doc['content'] ?? null;
        $filepath = $doc['filepath'] ?? null;

        if ($file !== null) {
            $content = swiftseek_extract_text($file);
            if ($filepath === null) {
                $rp = realpath($file);
                $filepath = $rp !== false ? $rp : $file;
            }
        } elseif ($content !== null) {
            if ($filepath === null) {
                throw new InvalidArgumentException("'filepath' is required when adding raw 'content'");
            }
        } else {
            throw new InvalidArgumentException("provide 'file' or 'content'");
        }

        $title = $doc['title'] ?? basename($filepath);
        $docType = $doc['doc_type'] ?? null;
        $charCount = mb_strlen($content, 'UTF-8');
        if ($file !== null) {
            $m = @filemtime($file);
            $mtime = $m === false ? null : (float) $m;
        } else {
            $mtime = array_key_exists('mtime', $doc) ? $doc['mtime'] : null;
        }

        $stmt = $conn->prepare(
            'INSERT INTO documents (filepath, title, doc_type, content, char_count, mtime) '
            . 'VALUES (?, ?, ?, ?, ?, ?)'
        );
        if ($stmt === false) {
            throw new RuntimeException('sql_prepare_failed: ' . $conn->error);
        }
        $stmt->bind_param('ssssid', $filepath, $title, $docType, $content, $charCount, $mtime);
        if (!$stmt->execute()) {
            $err = $stmt->error;
            $stmt->close();
            throw new RuntimeException('insert_failed: ' . $err);
        }
        $id = $stmt->insert_id;
        $stmt->close();
        return ['ok' => true, 'action' => 'add', 'id' => (int) $id,
                'filepath' => $filepath, 'char_count' => $charCount];
    }

    // ----------------------------------------------------------------------- //
    // list
    // ----------------------------------------------------------------------- //
    /** List document metadata (no content). $filters: doc_type, filepath, id, limit. */
    function swiftseek_list(mysqli $conn, array $filters = []): array
    {
        [$where, $types, $params] = swiftseek__meta_filters($filters);
        $limit = (int) ($filters['limit'] ?? 100);
        $sql = 'SELECT id, filepath, title, doc_type, char_count, created_at FROM documents';
        if ($where) {
            $sql .= ' WHERE ' . implode(' AND ', $where);
        }
        $sql .= ' ORDER BY id LIMIT ?';
        $types .= 'i';
        $params[] = $limit;

        $stmt = $conn->prepare($sql);
        if ($stmt === false) {
            throw new RuntimeException('sql_prepare_failed: ' . $conn->error);
        }
        swiftseek__bind($stmt, $types, $params);
        $stmt->execute();
        $res = $stmt->get_result();
        $rows = [];
        while ($row = $res->fetch_assoc()) {
            $row['id'] = (int) $row['id'];
            $row['char_count'] = (int) $row['char_count'];
            $rows[] = $row;
        }
        $stmt->close();
        return ['count' => count($rows), 'documents' => $rows];
    }

    // ----------------------------------------------------------------------- //
    // get
    // ----------------------------------------------------------------------- //
    /**
     * Fetch one full document by id, or null if it does not exist. Pass
     * $lineRange = [start, end] (1-based inclusive) to slice the content by line.
     */
    function swiftseek_get(mysqli $conn, int $id, ?array $lineRange = null): ?array
    {
        $stmt = $conn->prepare(
            'SELECT id, filepath, title, doc_type, content, char_count, created_at '
            . 'FROM documents WHERE id = ?'
        );
        if ($stmt === false) {
            throw new RuntimeException('sql_prepare_failed: ' . $conn->error);
        }
        $stmt->bind_param('i', $id);
        $stmt->execute();
        $row = $stmt->get_result()->fetch_assoc();
        $stmt->close();
        if (!$row) {
            return null;
        }
        $row['id'] = (int) $row['id'];
        $row['char_count'] = (int) $row['char_count'];
        if ($lineRange !== null) {
            $lines = preg_split('/\r\n|\r|\n/', $row['content']);
            $start = max(1, (int) $lineRange[0]);
            $end = (int) $lineRange[1];
            $slice = array_slice($lines, $start - 1, max(0, $end - $start + 1));
            $row['content'] = implode("\n", $slice);
            $row['line_range'] = [$start, $end];
            $row['total_lines'] = count($lines);
        }
        return $row;
    }

    // ----------------------------------------------------------------------- //
    // search  (SQL metadata filter -> PCRE grep -> capped context windows)
    // ----------------------------------------------------------------------- //
    /**
     * Regex grep over content. Matching is CASE-INSENSITIVE by default; pass
     * ['case_sensitive' => true] for exact case. $opts: case_sensitive, fixed,
     * context, max_matches, max_docs, max_line_chars (default 900), max_output_chars,
     * plus metadata filters (doc_type, filepath, id). Returns a payload array
     * (empty results => add a hint).
     */
    function swiftseek_search(mysqli $conn, string $pattern, array $opts = []): array
    {
        $ignoreCase = (bool) ($opts['ignore_case'] ?? true);   // case-insensitive by default
        if (!empty($opts['case_sensitive'])) {
            $ignoreCase = false;                               // explicit opt-out
        }
        $fixed      = (bool) ($opts['fixed'] ?? false);
        $context    = max(0, (int) ($opts['context'] ?? 2));
        $maxMatches = (int) ($opts['max_matches'] ?? 5);
        $maxDocs    = (int) ($opts['max_docs'] ?? 20);
        $maxLine    = (int) ($opts['max_line_chars'] ?? 900);
        $maxOutput  = (int) ($opts['max_output_chars'] ?? 40000);

        $rx = swiftseek__compile_regex($pattern, $ignoreCase, $fixed); // throws on bad regex

        [$where, $types, $params] = swiftseek__meta_filters($opts);
        $sql = 'SELECT id, filepath, title, content FROM documents';
        if ($where) {
            $sql .= ' WHERE ' . implode(' AND ', $where);
        }
        $stmt = $conn->prepare($sql);
        if ($stmt === false) {
            throw new RuntimeException('sql_prepare_failed: ' . $conn->error);
        }
        swiftseek__bind($stmt, $types, $params);
        $stmt->execute();
        $res = $stmt->get_result();
        $rows = [];
        while ($r = $res->fetch_assoc()) {
            $rows[] = $r;
        }
        $stmt->close();

        $results = [];
        $totalMatches = 0;
        $budget = 0;
        $truncated = false;
        foreach ($rows as $row) {
            $lines = preg_split('/\r\n|\r|\n/', $row['content']);
            $count = count($lines);
            $docMatches = [];
            foreach ($lines as $idx => $lineText) {
                if (@preg_match($rx, $lineText, $m, PREG_OFFSET_CAPTURE) !== 1) {
                    continue;
                }
                $byteOff = $m[0][1];
                $matched = $m[0][0];
                $cStart = mb_strlen(substr($lineText, 0, $byteOff), 'UTF-8');
                $cEnd = $cStart + mb_strlen($matched, 'UTF-8');

                $lo = max(0, $idx - $context);
                $hi = min($count - 1, $idx + $context);
                $ctx = [];
                for ($j = $lo; $j <= $hi; $j++) {
                    $span = ($j === $idx) ? [$cStart, $cEnd] : null;
                    [$txt, $trunc] = swiftseek__snippet($lines[$j], $maxLine, $span);
                    $entry = ['line_no' => $j + 1, 'text' => $txt, 'match' => ($j === $idx)];
                    if ($trunc) {
                        $entry['truncated'] = true;
                    }
                    $ctx[] = $entry;
                    $budget += mb_strlen($txt, 'UTF-8');
                }
                [$lineSnip] = swiftseek__snippet($lineText, $maxLine, [$cStart, $cEnd]);
                $docMatches[] = ['line_no' => $idx + 1, 'line' => $lineSnip, 'context' => $ctx];
                $budget += mb_strlen($lineSnip, 'UTF-8');
                if ($budget >= $maxOutput) {
                    $truncated = true;
                    break;
                }
                if (count($docMatches) >= $maxMatches) {
                    break;
                }
            }
            if ($docMatches) {
                $totalMatches += count($docMatches);
                $results[] = [
                    'id' => (int) $row['id'],
                    'filepath' => $row['filepath'],
                    'title' => $row['title'],
                    'match_count' => count($docMatches),
                    'matches' => $docMatches,
                ];
            }
            if ($truncated || count($results) >= $maxDocs) {
                break;
            }
        }

        $payload = [
            'pattern' => $pattern,
            'ignore_case' => $ignoreCase,
            'docs_searched' => count($rows),
            'docs_matched' => count($results),
            'total_matches' => $totalMatches,
            'approx_output_chars' => $budget,
            'truncated' => $truncated,
            'results' => $results,
        ];
        if ($truncated) {
            $payload['hint'] = 'Output hit the ~max_output_chars budget and stopped early; '
                . 'more matches likely exist. Narrow the query (more specific pattern, add '
                . 'metadata filters), or raise max_output_chars if you really need it all.';
        } elseif (!$results) {
            $payload['hint'] = 'No lexical match. Expand with synonyms / regex alternation '
                . "(e.g. 'a|b|c') or loosen metadata filters (matching is already "
                . 'case-insensitive). If still empty, the question may be conceptual '
                . 'rather than lexical.';
        }
        return $payload;
    }

    /** Wrap a user pattern as a valid PCRE; picks a delimiter not present in it. */
    function swiftseek__compile_regex(string $pattern, bool $ignoreCase, bool $fixed): string
    {
        $delim = null;
        foreach (['/', '#', '~', '%', '@', '!', ';', ',', '|', "\x01"] as $cand) {
            if (strpos($pattern, $cand) === false) {
                $delim = $cand;
                break;
            }
        }
        if ($delim === null) {
            $delim = "\x01";
        }
        $body = $fixed ? preg_quote($pattern, $delim) : $pattern;
        // Prefer UTF-8 mode; fall back to byte mode if the pattern won't compile that way.
        foreach ([($ignoreCase ? 'iu' : 'u'), ($ignoreCase ? 'i' : '')] as $flags) {
            $rx = $delim . $body . $delim . $flags;
            if (@preg_match($rx, '') !== false) {
                return $rx;
            }
        }
        throw new InvalidArgumentException('bad_regex: ' . $pattern);
    }

    /**
     * Cap a line at $maxChars (UTF-8 aware). Returns [text, truncated].
     * When the match span is known, centre the window on it; else trim from the start.
     */
    function swiftseek__snippet(string $text, int $maxChars, ?array $span = null): array
    {
        $len = mb_strlen($text, 'UTF-8');
        if ($maxChars <= 0 || $len <= $maxChars) {
            return [$text, false];
        }
        if ($span === null) {
            return [mb_substr($text, 0, $maxChars, 'UTF-8') . ' …', true];
        }
        [$s, $e] = $span;
        $pad = max(0, intdiv($maxChars - ($e - $s), 2));
        if (($e - $s) >= $maxChars) {
            $hi = min($len, max($e, $s + $maxChars));
        } else {
            $hi = min($len, $e + $pad);
        }
        $lo = max(0, $hi - $maxChars);
        $snip = mb_substr($text, $lo, $hi - $lo, 'UTF-8');
        if ($lo > 0) {
            $snip = '… ' . $snip;
        }
        if ($hi < $len) {
            $snip = $snip . ' …';
        }
        return [$snip, true];
    }

    // ----------------------------------------------------------------------- //
    // ingest  (walk a file/folder, extract text, insert each — idempotent by filepath)
    // ----------------------------------------------------------------------- //
    /** Supported file extensions (without the dot). */
    function swiftseek__supported_exts(): array
    {
        return ['txt', 'text', 'md', 'markdown', 'rst', 'log', 'csv', 'pdf', 'docx'];
    }

    /** List ingestible files under $root (a file or directory), sorted. */
    function swiftseek__iter_files(string $root, bool $recursive, array $exts): array
    {
        if (is_file($root)) {
            return [$root];
        }
        if (!is_dir($root)) {
            return [];
        }
        $out = [];
        if ($recursive) {
            $it = new RecursiveIteratorIterator(
                new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS)
            );
            foreach ($it as $f) {
                if ($f->isFile()
                    && in_array(strtolower($f->getExtension()), $exts, true)) {
                    $out[] = $f->getPathname();
                }
            }
        } else {
            foreach (scandir($root) as $name) {
                $full = $root . DIRECTORY_SEPARATOR . $name;
                if (is_file($full)
                    && in_array(strtolower(pathinfo($name, PATHINFO_EXTENSION)), $exts, true)) {
                    $out[] = $full;
                }
            }
        }
        sort($out);
        return $out;
    }

    /**
     * Recursively ingest a file or folder. $opts: doc_type, no_recursive, reindex,
     * update. By default files already stored (by absolute filepath) are skipped.
     * 'update' => true re-extracts a stored file only if the source is newer than the
     * stored copy; 'reindex' => true re-extracts every stored file regardless of age.
     */
    function swiftseek_ingest(mysqli $conn, string $path, array $opts = []): array
    {
        if (!file_exists($path)) {
            throw new RuntimeException("no such path: $path");
        }
        $recursive = !($opts['no_recursive'] ?? false);
        $reindex = (bool) ($opts['reindex'] ?? false);
        $update = (bool) ($opts['update'] ?? false);
        $docTypeOpt = $opts['doc_type'] ?? null;
        $exts = swiftseek__supported_exts();

        $added = [];
        $updated = [];
        $skipped = [];
        $failed = [];
        foreach (swiftseek__iter_files($path, $recursive, $exts) as $file) {
            $rp = realpath($file);
            $ap = $rp !== false ? $rp : $file;

            $stmt = $conn->prepare('SELECT id, mtime, doc_type FROM documents WHERE filepath = ? LIMIT 1');
            $stmt->bind_param('s', $ap);
            $stmt->execute();
            $existing = $stmt->get_result()->fetch_assoc();
            $stmt->close();

            $replace = false;
            if ($existing) {
                if ($reindex) {
                    $replace = true;
                } elseif ($update) {
                    $cur = @filemtime($file);
                    $cur = $cur === false ? null : (float) $cur;
                    $prev = isset($existing['mtime']) ? (float) $existing['mtime'] : null;
                    if ($cur !== null && ($prev === null || $cur > $prev)) {
                        $replace = true;
                    } else {
                        $skipped[] = $ap;          // already stored and not newer
                        continue;
                    }
                } else {
                    $skipped[] = $ap;              // already stored (default: skip)
                    continue;
                }
                $del = $conn->prepare('DELETE FROM documents WHERE filepath = ?');
                $del->bind_param('s', $ap);
                $del->execute();
                $del->close();
            }

            try {
                $content = swiftseek_extract_text($file);
            } catch (Throwable $e) {
                $failed[] = ['filepath' => $ap, 'error' => $e->getMessage()];
                continue;
            }
            if (trim($content) === '') {
                $failed[] = ['filepath' => $ap,
                             'error' => 'no_text_extracted (empty / image-only PDF?)'];
                continue;
            }
            // On replace, keep the previous doc_type unless a new one was given.
            $docType = $docTypeOpt;
            if ($docType === null && $existing && isset($existing['doc_type'])) {
                $docType = $existing['doc_type'];
            }
            $m = @filemtime($file);
            $r = swiftseek_add($conn, [
                'content' => $content,
                'filepath' => $ap,
                'title' => basename($ap),
                'doc_type' => $docType,
                'mtime' => $m === false ? null : (float) $m,
            ]);
            $rec = ['id' => $r['id'], 'filepath' => $ap, 'char_count' => $r['char_count']];
            if ($replace) {
                $updated[] = $rec;
            } else {
                $added[] = $rec;
            }
        }
        return ['ok' => true, 'action' => 'ingest',
                'added' => count($added), 'updated' => count($updated),
                'skipped' => count($skipped), 'failed' => count($failed),
                'added_docs' => $added, 'updated_docs' => $updated,
                'skipped_paths' => $skipped, 'failed_docs' => $failed];
    }

    // ----------------------------------------------------------------------- //
    // text extraction  (PDF + DOCX, built in — no Composer packages)
    // ----------------------------------------------------------------------- //
    /** Return plain text for a .txt/.md/.docx/.pdf file. Throws on failure. */
    function swiftseek_extract_text(string $path): string
    {
        $ext = strtolower(pathinfo($path, PATHINFO_EXTENSION));
        if ($ext === 'pdf') {
            return swiftseek__extract_pdf($path);
        }
        if ($ext === 'docx') {
            return swiftseek__extract_docx($path);
        }
        if ($ext === 'doc') {
            throw new RuntimeException('legacy .doc (binary Word) is not supported; '
                . 'convert to .docx or .txt first');
        }
        $data = @file_get_contents($path);
        if ($data === false) {
            throw new RuntimeException("cannot_read_file: $path");
        }
        if (!mb_check_encoding($data, 'UTF-8')) {
            $data = mb_convert_encoding($data, 'UTF-8', 'ISO-8859-1');
        }
        return $data;
    }

    /** Text from a Word .docx via ext/zip — strips the document XML to plain text. */
    function swiftseek__extract_docx(string $path): string
    {
        if (!class_exists('ZipArchive')) {
            throw new RuntimeException('.docx support needs the PHP zip extension (ZipArchive)');
        }
        $zip = new ZipArchive();
        if ($zip->open($path) !== true) {
            throw new RuntimeException('docx_read_failed: cannot open as zip');
        }
        $xml = $zip->getFromName('word/document.xml');
        $zip->close();
        if ($xml === false) {
            throw new RuntimeException('not a Word .docx (no word/document.xml)');
        }
        // Turn structural elements into whitespace, then drop every remaining tag.
        $xml = preg_replace('~<w:tab\b[^>]*>~', "\t", $xml);
        $xml = preg_replace('~<w:(?:br|cr)\b[^>]*>~', "\n", $xml);
        $xml = preg_replace('~</w:p>~', "\n", $xml);
        $text = preg_replace('~<[^>]+>~', '', $xml);
        return html_entity_decode($text, ENT_QUOTES | ENT_XML1, 'UTF-8');
    }

    /** Pure-PHP PDF text extraction via ext/zlib. Best effort; see module notes. */
    function swiftseek__extract_pdf(string $path): string
    {
        $data = @file_get_contents($path);
        if ($data === false) {
            throw new RuntimeException("cannot_read_file: $path");
        }
        $len = strlen($data);
        $chunks = [];
        $pos = 0;
        while (true) {
            $s = strpos($data, 'stream', $pos);
            if ($s === false) {
                break;
            }
            if ($s >= 3 && substr($data, $s - 3, 3) === 'end') { // tail of 'endstream'
                $pos = $s + 6;
                continue;
            }
            $start = $s + 6;
            if (substr($data, $start, 2) === "\r\n") {
                $start += 2;
            } elseif ($start < $len && ($data[$start] === "\n" || $data[$start] === "\r")) {
                $start += 1;
            }
            $end = strpos($data, 'endstream', $start);
            if ($end === false) {
                break;
            }
            $raw = substr($data, $start, $end - $start);
            $pos = $end + 9;

            $blob = @gzuncompress($raw);
            if ($blob === false) {
                $blob = @gzinflate($raw);
            }
            if ($blob === false) {
                $blob = $raw; // likely already uncompressed
            }
            $chunks[] = swiftseek__pdf_text_from_content($blob);
        }
        $clean = [];
        foreach ($chunks as $c) {
            if (trim($c) !== '') {
                $clean[] = $c;
            }
        }
        $joined = implode("\n", $clean);
        // Content-stream bytes are Latin-1 soup; normalise to UTF-8 for storage/search.
        if (!mb_check_encoding($joined, 'UTF-8')) {
            $joined = mb_convert_encoding($joined, 'UTF-8', 'ISO-8859-1');
        }
        return $joined;
    }

    /** Pull visible text out of one decompressed PDF content stream (best effort). */
    function swiftseek__pdf_text_from_content(string $s): string
    {
        $n = strlen($s);
        $lines = [];
        $line = [];
        $inArray = false;
        $spacePending = false;

        $emit = function (string $t) use (&$line, &$spacePending) {
            if ($spacePending && $line) {
                $line[] = ' ';
            }
            $spacePending = false;
            $line[] = $t;
        };
        $newline = function () use (&$lines, &$line) {
            $lines[] = implode('', $line);
            $line = [];
        };

        $digits = '0123456789';
        $i = 0;
        while ($i < $n) {
            $c = $s[$i];
            if ($c === '(') {                       // literal (string)
                $i++;
                $depth = 1;
                $out = '';
                while ($i < $n && $depth > 0) {
                    $ch = $s[$i];
                    if ($ch === '\\') {
                        $i++;
                        if ($i >= $n) {
                            break;
                        }
                        $esc = $s[$i];
                        $map = ['n' => "\n", 'r' => "\r", 't' => "\t", 'b' => "\x08",
                                'f' => "\x0C", '(' => '(', ')' => ')', '\\' => '\\'];
                        if (isset($map[$esc])) {
                            $out .= $map[$esc];
                            $i++;
                        } elseif (strpos('01234567', $esc) !== false) {
                            $od = $esc;
                            $i++;
                            for ($k = 0; $k < 2; $k++) {
                                if ($i < $n && strpos('01234567', $s[$i]) !== false) {
                                    $od .= $s[$i];
                                    $i++;
                                } else {
                                    break;
                                }
                            }
                            $out .= chr(intval($od, 8) & 0xFF);
                        } elseif ($esc === "\r") {
                            $i++;
                            if ($i < $n && $s[$i] === "\n") {
                                $i++;
                            }
                        } elseif ($esc === "\n") {
                            $i++;
                        } else {
                            $out .= $esc;
                            $i++;
                        }
                    } elseif ($ch === '(') {
                        $depth++;
                        $out .= $ch;
                        $i++;
                    } elseif ($ch === ')') {
                        $depth--;
                        if ($depth > 0) {
                            $out .= $ch;
                        }
                        $i++;
                    } else {
                        $out .= $ch;
                        $i++;
                    }
                }
                $emit($out);
            } elseif ($c === '<' && $i + 1 < $n && $s[$i + 1] !== '<') {  // <hex string>
                $j = strpos($s, '>', $i);
                if ($j === false) {
                    break;
                }
                $hex = preg_replace('/[^0-9a-fA-F]/', '', substr($s, $i + 1, $j - $i - 1));
                if (strlen($hex) % 2) {
                    $hex .= '0';
                }
                $bin = @hex2bin($hex);
                $emit($bin === false ? '' : $bin);
                $i = $j + 1;
            } elseif ($c === '<' && $i + 1 < $n && $s[$i + 1] === '<') {  // << dict >> -> skip
                $i += 2;
            } elseif ($c === '[') {
                $inArray = true;
                $i++;
            } elseif ($c === ']') {
                $inArray = false;
                $spacePending = false;
                $i++;
            } elseif ($c === '-' || strpos($digits, $c) !== false) {
                $j = $i;
                while ($j < $n && (strpos($digits, $s[$j]) !== false
                        || $s[$j] === '-' || $s[$j] === '.')) {
                    $j++;
                }
                if ($inArray && (float) substr($s, $i, $j - $i) <= -100) {
                    $spacePending = true;            // wide negative kern ~ a space
                }
                $i = $j;
            } elseif (($two = substr($s, $i, 2)) === 'Td' || $two === 'TD'
                      || $two === 'T*' || $two === 'Tm') {
                $newline();
                $i += 2;
            } elseif ($c === "'" || $c === '"') {
                $newline();
                $i++;
            } else {
                $i++;
            }
        }
        if ($line) {
            $newline();
        }
        $clean = [];
        foreach ($lines as $l) {
            if (trim($l) !== '') {
                $clean[] = $l;
            }
        }
        return implode("\n", $clean);
    }

} // end function_exists guard
