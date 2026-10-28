"""Self-tests for the repository's gates (CODING-STYLE R.7).

A gate that has never gone red proves nothing, so every custom gitleaks rule and the nesting
checker get at least one input they must reject and one they must accept, generated at run time.
The red results are pinned to the rule that is supposed to cause them: the scan is repeated with
that rule deleted from the config and must then stay quiet about it.

Secret-looking samples are assembled from fragments here so this file does not trip the gates
it tests.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from check_nesting import MAX_DEPTH, find_violations
from tests.conftest import REPO_ROOT

GITLEAKS = shutil.which("gitleaks")
CONFIG = REPO_ROOT / ".gitleaks.toml"


@pytest.fixture(autouse=True)
def _gitleaks_required_in_ci(request: pytest.FixtureRequest) -> None:
    if "gitleaks" not in request.node.name and "gitleaks" not in request.node.keywords:
        return
    if GITLEAKS is not None:
        return
    if os.environ.get("CI"):
        pytest.fail("gitleaks is not installed; CI must run the gate self-tests")
    pytest.skip("gitleaks binary not found on PATH; install it to run the gate self-tests")


def scan(tmp_path: Path, files: dict[str, str], config: Path = CONFIG) -> list[str]:
    """Rule ids that fire on `files` (relative path -> content) under `config`."""
    source = tmp_path / "scanned"
    for relative, content in files.items():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    report = tmp_path / "report.json"
    result = subprocess.run(
        [
            str(GITLEAKS),
            "detect",
            "--no-git",
            "--source",
            str(source),
            "--config",
            str(config),
            "--report-format",
            "json",
            "--report-path",
            str(report),
            "--no-banner",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    findings = json.loads(report.read_text(encoding="utf-8")) if report.exists() else []
    return sorted({finding["RuleID"] for finding in findings})


def config_without(rule_id: str, tmp_path: Path) -> Path:
    """The real config with one custom rule deleted (the 'old behaviour' for the green half)."""
    text = CONFIG.read_text(encoding="utf-8")
    head, *blocks = re.split(r"(?m)^(?=\[\[rules\]\]$)", text)
    kept = [block for block in blocks if f'id = "{rule_id}"' not in block]
    assert len(kept) == len(blocks) - 1, f"rule {rule_id} not found in {CONFIG.name}"
    mutated = tmp_path / "mutated.toml"
    mutated.write_text(head + "".join(kept), encoding="utf-8")
    return mutated


def jwt_like() -> str:
    return ".".join(
        ["eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiJkZW1vIn0", "c2lnbmF0dXJlX2Zha2U"]
    )


def han(*codepoints: int) -> str:
    return "".join(chr(point) for point in codepoints)


def basic_credentials() -> str:
    """What follows 'Basic ' in a real header: base64 of user:password."""
    return base64.b64encode(b"svc-account:" + b"Pw9" * 4).decode("ascii")


# A cookie value, a database password and the like: made here so no secret-shaped literal sits in
# this file. The values are not hashes or tokens of any service.
SESSION_VALUE = "a1B2c3D4" * 2
DB_PASSWORD = "pw" + "-" + "7Hq2"


@dataclass(frozen=True)
class RuleCase:
    rule_id: str
    fails: dict[str, str]  # file -> content that must be flagged by this rule
    passes: dict[str, str]  # file -> content that must produce no finding at all


CASES = [
    RuleCase(
        "cs-jwt",
        fails={"token.txt": f"token = {jwt_like()}\n"},
        passes={
            "ok.txt": "header.payload.signature\neyJhbGciOiJIUzI1NiJ9.short\nv1.2.3\n",
        },
    ),
    RuleCase(
        "cs-bearer-token",
        fails={
            "http.txt": "Authorization: Bearer " + "Ab1" * 8 + "\n",
            "lower.txt": "authorization: bearer " + "xY9" * 8 + "\n",
        },
        passes={
            "ok.txt": "Authorization: Bearer <token>\nAuthorization: Bearer {token}\n"
            "bearer tokens go in a header\n",
        },
    ),
    RuleCase(
        "cs-local-user-path",
        fails={
            "mac.txt": "open('/Us" + "ers/jane/project/file.txt')\n",
            "linux.txt": "cd /ho" + "me/jane/work/\n",
            "url.txt": "file:///Us" + "ers/jane/x/y\n",
        },
        passes={
            "ok.txt": "src/home/docs/readme\nhome/jane/x/y\n~/Documents/x\n/usr/local/bin/tool\n"
            "see the Users/ guide\n",
        },
    ),
    RuleCase(
        "cs-personal-email",
        fails={
            "a.txt": "contact: jane.doe" + "@" + "gmail.com\n",
            "lookalike_domain.txt": "x" + "@" + "notexample.com\n",
            "suffix_trick.txt": "x" + "@" + "example.com.evil.io\n",
            "fake_noreply.txt": "fake-noreply" + "@" + "anthropic.com\n",
        },
        passes={
            "ok.txt": "\n".join(
                [
                    "someone@example.com",
                    "someone@example.org",
                    "12345+jane@users.noreply.github.com",
                    "noreply@anthropic.com",
                ]
            ),
        },
    ),
    RuleCase(
        "cs-cjk-text",
        fails={
            "notes.txt": f"title: {han(0x4E2D, 0x6587)}\n",
            "fullwidth_comma.txt": f"a{han(0xFF0C)}b\n",
            "README.zh-CN.md.bak": han(0x4E2D, 0x6587),  # the allowlist is anchored at the end
            "XREADME.zh-CN.md": han(0x4E2D, 0x6587),  # ...and at a path-segment boundary
            "plugins/other/login.py": han(0x4E2D, 0x6587),  # the old, built-in location
            # The connector allowlist is anchored on the layout of a connector package: its
            # login.py directly under src/<package>/, and nothing else in that package.
            "connectors/acme/src/catalog_sync_acme/source.py": han(0x4E2D, 0x6587),
            "connectors/acme/src/catalog_sync_acme/sub/login.py": han(0x4E2D, 0x6587),
            "connectors/acme/login.py": han(0x4E2D, 0x6587),
            "XXconnectors/acme/src/catalog_sync_acme/login.py": han(0x4E2D, 0x6587),
        },
        passes={
            "README.zh-CN.md": han(0x4E2D, 0x6587) + "\n",
            "docs/README.zh-CN.md": han(0x4E2D, 0x6587) + "\n",
            "connectors/acme/src/catalog_sync_acme/login.py": han(0x4E2D, 0x6587) + "\n",
            "elsewhere/connectors/acme/src/catalog_sync_acme/login.py": han(0x4E2D, 0x6587) + "\n",
            "plain.txt": "ascii only, caf\u00e9 and na\u00efve are not CJK\n",
        },
    ),
    RuleCase(
        "cs-url-password",
        fails={
            # The review's miss: a compose service name has no dot, which cs-personal-email needs.
            "service_name.txt": f"DATABASE_URL=postgresql://u:{DB_PASSWORD}@postgres:5432/x\n",
            "short_password.txt": "postgresql://u:" + "pw" + "@postgres:5432/x\n",
            "dotted.txt": f"postgres://app:{DB_PASSWORD}@db.internal.invalid:5432/app\n",
            "empty_user.txt": f"REDIS_URL=redis://:{DB_PASSWORD}@cache:6379/0\n",
            "yaml.txt": f'dsn: "postgresql://loader:{DB_PASSWORD}@pg/catalog"\n',
            # The allowlist for the CI database is anchored to the loopback host, not to the pair.
            "ci_default_elsewhere.txt": "postgresql://postgres:" + "postgres@prod-db:5432/x\n",
        },
        passes={
            "ok.txt": "\n".join(
                [
                    "postgresql://user:${DB_PASSWORD}@postgres:5432/x",
                    "postgresql://user:$DB_PASSWORD@postgres/x",
                    "postgresql://user:<password>@host/db",
                    "postgresql://user:{password}@host/db",
                    "postgresql://loader:[redacted]@db.example.invalid/catalog",
                    "postgresql://u:%s@h/db",
                    "postgresql://u:****@h/db",
                    "postgresql://user:password@host/db",
                    "https://user:PASSWORD@example.com/",
                    "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
                    "http://ops:hunter2-canary@127.0.0.1:1/",
                    "http://ops:"
                    + "pw"
                    + "@x:8765",  # carried by an earlier test; history stays clean
                    "https://example.invalid:8443/health and https://example.invalid/@handle",
                    "https://host:8080/path@elsewhere and mailto:someone@example.com",
                    "postgresql://db.example.invalid:5432/catalog",
                ]
            )
            + "\n",
        },
    ),
    RuleCase(
        "cs-basic-auth",
        fails={
            "header.txt": f"Authorization: Basic {basic_credentials()}\n",
            "lower.txt": f"authorization: basic {basic_credentials()}\n",
            "dict.txt": f"headers = {{'Authorization': 'Basic {basic_credentials()}'}}\n",
            "json.txt": f'{{"authorization": "Basic {basic_credentials()}"}}\n',
            "credential_file.cred": f"AUTHORIZATION=Basic {basic_credentials()}\n",
        },
        passes={
            "ok.txt": "\n".join(
                [
                    "Authorization: Basic <base64>",
                    "Authorization: Basic {credentials}",
                    "Authorization: Basic $ENCODED",
                    "Authorization: Basic credentials-go-here",
                    "Basic setup is done; basic authentication is discouraged",
                    "authorization: basic",
                ]
            )
            + "\n",
        },
    ),
    RuleCase(
        "cs-cookie",
        fails={
            "header.txt": f"Cookie: SESSION={SESSION_VALUE}\n",
            "lower.txt": f"cookie: sid={SESSION_VALUE}; theme=dark\n",
            "set_cookie.txt": f"Set-Cookie: sid={SESSION_VALUE}; Path=/; HttpOnly\n",
            "json.txt": f'{{"cookie": "sessionid={SESSION_VALUE}"}}\n',
            # The credential file the tool itself writes: KEY=value lines, a cookie as name=value.
            "credential_file.cred": f"COOKIE=sid={SESSION_VALUE}\n",
            "named_key.cred": f"SESSION_COOKIE=sid={SESSION_VALUE}\n",
        },
        passes={
            "ok.txt": "\n".join(
                [
                    "Cookie: <cookie>",
                    "Cookie: name=value",
                    "Cookie: sid=abc123; theme=dark",
                    "COOKIE=${COOKIE}",
                    "COOKIE=",
                    "# COOKIE=<paste the cookie here>",
                    "cookies: sid=" + "x" * 4,
                    "the cookie jar keeps cookie: values per domain",
                    'headers["cookie"] = f"sid={sid}"',
                ]
            )
            + "\n",
        },
    ),
]


@pytest.mark.gitleaks
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.rule_id)
def test_gitleaks_rule_flags_what_it_should_and_nothing_else(
    case: RuleCase, tmp_path: Path
) -> None:
    for name, content in case.fails.items():
        fired = scan(tmp_path / f"fail-{name.replace('/', '_')}", {name: content})
        assert case.rule_id in fired, f"{case.rule_id} missed {name!r}; fired: {fired}"
    for name, content in case.passes.items():
        fired = scan(tmp_path / f"pass-{name.replace('/', '_')}", {name: content})
        assert fired == [], f"{name!r} should be clean but fired {fired}"


@pytest.mark.gitleaks
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.rule_id)
def test_gitleaks_red_comes_from_the_named_rule(case: RuleCase, tmp_path: Path) -> None:
    mutated = config_without(case.rule_id, tmp_path)
    for name, content in case.fails.items():
        fired = scan(tmp_path / f"mut-{name.replace('/', '_')}", {name: content}, mutated)
        assert case.rule_id not in fired, f"{name!r} still flagged as {case.rule_id} without it"


@pytest.mark.gitleaks
@pytest.mark.parametrize(
    ("name", "content", "expect_clean"),
    [
        (".venv/lib/pkg/meta.txt", "owner: jane.doe" + "@" + "gmail.com\n", True),
        ("src/.venv/pkg/meta.txt", "owner: jane.doe" + "@" + "gmail.com\n", True),
        ("venv/pkg/meta.txt", "owner: jane.doe" + "@" + "gmail.com\n", False),
        ("uv.lock", "contact = jane.doe" + "@" + "gmail.com\n", True),
        ("my-uv.lock", "contact = jane.doe" + "@" + "gmail.com\n", False),
        ("a/__pycache__/x.txt", "jane.doe" + "@" + "gmail.com\n", True),
    ],
)
def test_gitleaks_global_path_allowlist_has_the_intended_edges(
    tmp_path: Path, name: str, content: str, expect_clean: bool
) -> None:
    fired = scan(tmp_path, {name: content})
    assert (fired == []) is expect_clean, fired


HASH = "13b152606304b7763a166612a5285c2d7b33bda84a1159c90dfbcba9db6fdacf"
NOT_A_HASH = "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3kM5oR7tV9xB2cE4gJ6lN8qS0uW2yA4c"  # 64 characters
OTHER_HASH = "0121cb27f976d015c25cb965fe6a7bdcf355b6581b833d2a321d0ec930992a27"


@pytest.mark.gitleaks
@pytest.mark.parametrize(
    ("content", "expect_clean"),
    [
        # What the allowlist is for: the sha256 an idempotency key is, under that name.
        (f'"idempotency_key": "{HASH}"\n', True),
        (f'idempotency_key = "{HASH}"\n', True),
        # ...and not a hole for secrets: the same hash under a secret's name, a key that is not a
        # sha256 (too short, not hex), and a real secret on the same line as an allowed key.
        (f'"api_key": "{HASH}"\n', False),
        (f'"secret_key": "{HASH}"\n', False),
        (f'"idempotency_key": "{HASH[:40]}"\n', False),
        (f'"idempotency_key": "{NOT_A_HASH}"\n', False),
        (f'"idempotency_key": "{HASH}", "api_key": "{OTHER_HASH}"\n', False),
    ],
)
def test_gitleaks_allows_an_idempotency_hash_and_nothing_wider(
    tmp_path: Path, content: str, expect_clean: bool
) -> None:
    fired = scan(tmp_path, {"example.json": content})
    assert (fired == []) is expect_clean, fired


@pytest.mark.gitleaks
def test_without_the_allowlist_entry_an_idempotency_hash_is_flagged(tmp_path: Path) -> None:
    """The red half: with the regex removed, the sample that passes above is a generic finding."""
    text = CONFIG.read_text(encoding="utf-8")
    start = text.index('\nregexTarget = "match"')
    end = text.index("paths = [")
    mutated = tmp_path / "mutated.toml"
    mutated.write_text(text[:start] + "\n" + text[end:], encoding="utf-8")
    # A config that failed to load would also "find nothing": prove this one still works.
    assert "cs-jwt" in scan(tmp_path / "alive", {"t.txt": f"token = {jwt_like()}\n"}, mutated)
    fired = scan(tmp_path / "scan", {"example.json": f'"idempotency_key": "{HASH}"\n'}, mutated)
    assert fired == ["generic-api-key"]


@pytest.mark.gitleaks
def test_the_repository_itself_is_clean(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    result = subprocess.run(
        [
            str(GITLEAKS),
            "detect",
            "--no-git",
            "--source",
            str(REPO_ROOT),
            "--config",
            str(CONFIG),
            "--report-format",
            "json",
            "--report-path",
            str(report),
            "--no-banner",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    findings = json.loads(report.read_text(encoding="utf-8")) if report.exists() else []
    assert result.returncode == 0, [(f["RuleID"], f["File"]) for f in findings]


# The commit .gitleaks.toml allows for the CJK rule: it added a connector's login selectors at a
# path that has since moved. The working tree scan above cannot see it; only a history scan can.
LOGIN_SELECTORS_COMMIT = "4e5745c94cd4dab9d0134d08d3465925191327bb"


def scan_history(tmp_path: Path, config: Path, log_opts: str) -> list[tuple[str, str]]:
    """(rule id, file) of every finding in the commits `git log <log_opts>` selects."""
    report = tmp_path / "history-report.json"
    result = subprocess.run(
        [
            str(GITLEAKS),
            "detect",
            "--source",
            str(REPO_ROOT),
            "--config",
            str(config),
            f"--log-opts={log_opts}",
            "--report-format",
            "json",
            "--report-path",
            str(report),
            "--no-banner",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    findings = json.loads(report.read_text(encoding="utf-8")) if report.exists() else []
    # Exit 1 with no findings is a config that failed to load, which would also "find nothing".
    assert (result.returncode == 1) == bool(findings), result.stderr
    return [(finding["RuleID"], finding["File"]) for finding in findings]


def has_commit(sha: str) -> bool:
    """False in a source archive, or after the history was rewritten."""
    probe = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


@pytest.mark.gitleaks
def test_the_full_history_is_clean(tmp_path: Path) -> None:
    """The tree scan misses commits that carried a path which no longer exists; this one does not.

    HEAD, not --all: a stash or a sibling branch is not part of the commit under test.
    """
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    assert scan_history(tmp_path, CONFIG, "HEAD") == []


@pytest.mark.gitleaks
def test_the_history_allowlist_is_one_commit_and_the_cjk_rule_only(tmp_path: Path) -> None:
    """Red half: without the entry that commit is a cs-cjk-text finding and nothing else."""
    if not has_commit(LOGIN_SELECTORS_COMMIT):
        pytest.skip("the allowlisted commit is not in this history")
    text = CONFIG.read_text(encoding="utf-8")
    entry = f'commits = ["{LOGIN_SELECTORS_COMMIT}"]\n'
    assert text.count(entry) == 1, "the allowlist entry moved or was widened"
    mutated = tmp_path / "without-commit.toml"
    elsewhere = f'commits = ["{"0" * 40}"]\n'  # an allowlist block needs a condition to load
    mutated.write_text(text.replace(entry, elsewhere), encoding="utf-8")
    fired = scan_history(tmp_path, mutated, f"-1 {LOGIN_SELECTORS_COMMIT}")
    assert {rule for rule, _ in fired} == {"cs-cjk-text"}, fired
    assert scan_history(tmp_path, CONFIG, f"-1 {LOGIN_SELECTORS_COMMIT}") == []


# --- nesting checker ------------------------------------------------------------------------

HEADERS = {
    "if": "if x:",
    "for": "for i in x:",
    "while": "while x:",
    "with": "with x:",
}


def nested(openers: list[str], leaf: str = "pass") -> str:
    """A function whose body nests `openers` in order, with `leaf` at the bottom."""

    def render(index: int, indent: int) -> str:
        pad = "    " * indent
        if index == len(openers):
            return f"{pad}{leaf}\n"
        name = openers[index]
        inner = render(index + 1, indent + 1)
        if name == "try":
            return f"{pad}try:\n{inner}{pad}except OSError:\n{pad}    pass\n"
        if name == "match":  # one level, though the case body is indented twice
            return f"{pad}match x:\n{pad}    case 1:\n{render(index + 1, indent + 2)}"
        return f"{pad}{HEADERS[name]}\n{inner}"

    return "def f(x):\n" + render(0, 1)


@pytest.mark.parametrize("construct", [*HEADERS, "try", "match"])
def test_every_construct_counts_towards_depth(construct: str) -> None:
    at_limit = nested(["if"] * (MAX_DEPTH - 1) + [construct])
    below = nested(["if"] * (MAX_DEPTH - 2) + [construct])
    assert find_violations(at_limit) != [], f"{construct} must count as a level"
    assert find_violations(below) == []


def test_the_limit_is_exactly_five_levels() -> None:
    assert find_violations(nested(["if"] * 4)) == []
    (violation,) = find_violations(nested(["if"] * 5))
    assert violation.depth == 5


def test_an_elif_chain_does_not_nest() -> None:
    branches = "".join(f"    elif x == {n}:\n        pass\n" for n in range(2, 12))
    source = "def f(x):\n    if x == 1:\n        pass\n" + branches + "    else:\n        pass\n"
    assert find_violations(source) == []


def test_elif_inside_a_deep_block_is_still_judged_by_the_block_it_sits_in() -> None:
    source = textwrap.dedent(
        """
        def f(x):
            if x:
                for i in x:
                    while x:
                        with x:
                            if x:
                                pass
                            elif x:
                                pass
        """
    )
    assert len(find_violations(source)) == 2, "each branch body is its own offending block"


def test_an_if_written_inside_else_is_a_real_extra_level() -> None:
    as_elif = textwrap.dedent(
        """
        def f(x):
            if x:
                for i in x:
                    while x:
                        if x:
                            pass
                        elif x:
                            pass
        """
    )
    as_else_if = textwrap.dedent(
        """
        def f(x):
            if x:
                for i in x:
                    while x:
                        if x:
                            pass
                        else:
                            if x:
                                pass
        """
    )
    assert find_violations(as_elif) == [], "elif stays on the level of its if"
    assert len(find_violations(as_else_if)) == 1, "else: followed by if nests one deeper"


TRY_BLOCKS = {
    "body": "try:\n    if x:\n        deep = 1\nfinally:\n    pass",
    "except": "try:\n    pass\nexcept OSError:\n    if x:\n        deep = 1",
    "else": "try:\n    pass\nexcept OSError:\n    pass\nelse:\n    if x:\n        deep = 1",
    "finally": "try:\n    pass\nfinally:\n    if x:\n        deep = 1",
}


@pytest.mark.parametrize("part", sorted(TRY_BLOCKS))
def test_every_part_of_a_try_statement_counts_as_one_level(part: str) -> None:
    prefix = "def f(x):\n    if x:\n        for i in x:\n            while x:\n"
    indented = textwrap.indent(TRY_BLOCKS[part], " " * 16)
    assert len(find_violations(prefix + indented + "\n")) == 1


def test_a_nested_function_starts_again_at_zero() -> None:
    source = textwrap.dedent(
        """
        def f(x):
            if x:
                for i in x:
                    while x:
                        with x:
                            def g(y):
                                if y:
                                    pass
        """
    )
    assert find_violations(source) == []


def test_each_offending_block_is_reported_once() -> None:
    source = nested(["if"] * 5, leaf="a = 1\n" + "    " * 6 + "b = 2")
    assert len(find_violations(source)) == 1


def run_checker(directory: Path, source: str) -> subprocess.CompletedProcess[str]:
    directory.mkdir(parents=True)
    (directory / "sample.py").write_text(source, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "check_nesting.py"), str(directory)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_command_line_exit_codes(tmp_path: Path) -> None:
    deep = run_checker(tmp_path / "deep", nested(["if"] * 5))
    assert deep.returncode == 1
    assert "sample.py:" in deep.stderr and "nesting depth 5" in deep.stderr
    assert run_checker(tmp_path / "ok", nested(["if"] * 4)).returncode == 0
    broken = run_checker(tmp_path / "broken", "def f(:\n")
    assert broken.returncode == 2
    assert "cannot parse" in broken.stderr


def test_the_checker_skips_virtualenvs(tmp_path: Path) -> None:
    hidden = tmp_path / ".venv" / "lib"
    hidden.mkdir(parents=True)
    (hidden / "deep.py").write_text(nested(["if"] * 6), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "check_nesting.py"), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0


# --- the type gate (mypy --strict) ----------------------------------------------------------------
#
# The gate is the third-party tool; what is ours is the claim that it is switched on strictly. The
# cases below show it rejecting what strictness exists to reject, accepting the documented
# exception (an ignore that names the error code), and going quiet when strictness is taken away.


def mypy(tmp_path: Path, source: str, *flags: str) -> subprocess.CompletedProcess[str]:
    module = tmp_path / "sample.py"
    module.write_text(textwrap.dedent(source), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "mypy", "--config-file", "", *flags, str(module)],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )


UNTYPED = "def double(value):\n    return value * 2\n"
WRONG = 'count: int = "three"\n'
TYPED = "def double(value: int) -> int:\n    return value * 2\n"
ANY_RETURN = "import json\n\n\ndef number(text: str) -> int:\n    return json.loads(text)\n"
NAMED_IGNORE = "def double(value):  # type: ignore[no-untyped-def]\n    return value * 2\n"


@pytest.mark.parametrize(
    ("source", "complaint"),
    [(UNTYPED, "no-untyped-def"), (WRONG, "assignment"), (ANY_RETURN, "no-any-return")],
    ids=["untyped-def", "wrong-type", "any-return"],
)
def test_the_type_gate_rejects_what_strict_mode_exists_to_reject(
    tmp_path: Path, source: str, complaint: str
) -> None:
    result = mypy(tmp_path, source, "--strict")
    assert result.returncode == 1 and complaint in result.stdout


def test_the_type_gate_accepts_typed_code_and_an_ignore_that_names_its_error(
    tmp_path: Path,
) -> None:
    assert mypy(tmp_path, TYPED, "--strict").returncode == 0
    assert mypy(tmp_path, NAMED_IGNORE, "--strict").returncode == 0


def test_the_type_gate_goes_quiet_without_strictness(tmp_path: Path) -> None:
    """Revert the rule and the red must go: it came from `strict`, not from something nearby."""
    assert mypy(tmp_path, UNTYPED).returncode == 0
    assert mypy(tmp_path, ANY_RETURN).returncode == 0


def test_the_projects_own_configuration_is_strict() -> None:
    import tomllib

    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["mypy"]["strict"] is True
    assert config["tool"]["mypy"]["files"] == ["src"]
