<?php
/**
 * config.example.php — copy this to "config.php" and fill in your real credentials.
 *
 * Why config.php instead of a .env file: a PHP file is EXECUTED by the web server,
 * never sent as source. Opening https://your-site/.../config.php in a browser runs
 * it and shows a blank page — your password is NOT exposed. A .env file, by contrast,
 * is static and Apache would serve it as plain text. config.php is the safe choice.
 *
 * swiftseek_connect() auto-loads ./config.php when called with no arguments.
 * The real config.php is git-ignored — commit only this example.
 *
 * Belt-and-suspenders: this file also refuses direct HTTP access (404, see below),
 * and for maximum safety you can keep the whole folder ABOVE your public web root.
 */

// Refuse to do anything if this file is somehow requested directly over HTTP.
if (PHP_SAPI !== 'cli'
    && isset($_SERVER['SCRIPT_FILENAME'])
    && @realpath($_SERVER['SCRIPT_FILENAME']) === @realpath(__FILE__)) {
    http_response_code(404);
    exit;
}

return [
    'host'     => 'localhost',
    'port'     => 3306,
    'user'     => 'app',
    'password' => 'change-me',
    'database' => 'docs',
];
