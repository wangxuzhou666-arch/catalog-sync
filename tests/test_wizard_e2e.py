"""``catalog-sync init`` from the first question to a config that runs, against live local services.

The Mock SaaS and the reference platform run in this process on loopback (the `mock` marker allows
127.0.0.1 and nothing else). Nothing is faked on the tool's side: the real login, the real
`inspect`, a real sample pull and normalisation, the real sinks. What the wizard wrote is then used
by `run`, and the platform's and the database's contents are compared with the seeded dataset.
"""

from __future__ import annotations

import json
import stat
import textwrap
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from catalog_sync import cli, registry
from catalog_sync.compliance import parse_compliance
from catalog_sync.config import load_config
from catalog_sync.mock.auth import Account
from catalog_sync.mock.platform_server import PlatformServer
from catalog_sync.mock.server import MockSaasServer
from catalog_sync.mock.service import MockOptions
from catalog_sync.plugins.mock import settings as mock_settings
from catalog_sync.sdk import request_gap_setting
from catalog_sync.sinks.sqlite import read_table
from catalog_sync.wizard import flow
from catalog_sync.wizard.prompts import Prompter

pytestmark = pytest.mark.mock

PLATFORM_TOKEN = "wizard-platform-canary-" + "4d9a"
ACCOUNT_PASSWORD = "wizard-account-canary-" + "c81f"
ACCOUNT_USER = "ops-user"
# The pause between requests is at least 60 / ceiling seconds and is slept for real: at 6000 a
# minute a wizard's sample pull spent most of a second waiting for a server on this machine. The
# ceiling is part of what the wizard declares, but not what these tests are about: set it high.
CEILING_PER_MINUTE = 600_000


@dataclass
class Services:
    saas: MockSaasServer
    platform: PlatformServer


@pytest.fixture
def services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Services]:
    # The pause between requests is politeness towards a real service; the pull would otherwise
    # spend a minute of this file's time waiting for a server on the same machine. The ceiling
    # the wizard's declaration sets (CEILING_PER_MINUTE) still floors it at 60 / ceiling seconds.
    monkeypatch.setattr(mock_settings, "REQUEST_GAP", request_gap_setting(0.0))
    monkeypatch.setenv("PLATFORM_TOKEN", PLATFORM_TOKEN)
    monkeypatch.setenv("MOCK_SAAS_USER", ACCOUNT_USER)
    monkeypatch.setenv("MOCK_SAAS_PASSWORD", ACCOUNT_PASSWORD)
    options = MockOptions(account=Account(ACCOUNT_USER, ACCOUNT_PASSWORD))
    with (
        MockSaasServer(options) as saas,
        PlatformServer(tmp_path / "platform" / "platform.db", token=PLATFORM_TOKEN) as platform,
    ):
        yield Services(saas, platform)


@dataclass
class Init:
    """The CLI pointed at one config path and one workspace, with an answers file."""

    directory: Path
    services: Services
    capsys: pytest.CaptureFixture[str]

    @property
    def config(self) -> Path:
        return self.directory / "catalog-sync.toml"

    @property
    def workspace(self) -> Path:
        return self.directory / "ws"

    def answers(
        self, extra: str = "", *, connector: str = "mock.materials", sinks: str = ""
    ) -> Path:
        base = self.services.saas.base_url
        text = textwrap.dedent(
            f"""
            connector = "{connector}"

            [settings]
            base_url = "{base}"

            [compliance]
            data_owner = "Demo Co (invented data)"
            authorization = "own_account"
            authorization_ref = "demo/mock-saas"
            tos_url = "{base}/terms"
            tos_reviewed = true
            max_requests_per_minute = {CEILING_PER_MINUTE}
            retention_days = 30

            [login]
            method = "form"

            [sample]
            items = 4
            """
        )
        text += extra + (sinks or self.default_sinks())
        path = self.directory / "answers.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def default_sinks(self) -> str:
        return textwrap.dedent(
            f"""
            [[sinks]]
            kind = "sqlite"
            name = "local"
            path = "./out/catalog.db"

            [[sinks]]
            kind = "http"
            name = "platform"
            url = "{self.services.platform.ingest_url}"
            auth_header_env = "PLATFORM_TOKEN"
            """
        )

    def init(self, answers: Path, *flags: str) -> int:
        return cli.main(
            [
                "--config",
                str(self.config),
                "--workspace",
                str(self.workspace),
                "init",
                "--answers",
                str(answers),
                "--non-interactive",
                *flags,
            ]
        )

    def run(self, *args: str) -> int:
        return cli.main(["--config", str(self.config), "--workspace", str(self.workspace), *args])

    def output(self) -> tuple[str, str]:
        captured = self.capsys.readouterr()
        return captured.out, captured.err

    def audit(self) -> list[dict[str, object]]:
        path = self.workspace / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def init(tmp_path: Path, services: Services, capsys: pytest.CaptureFixture[str]) -> Init:
    return Init(tmp_path, services, capsys)


# --- the scripted run, end to end -----------------------------------------------------------------


def test_a_scripted_init_logs_in_samples_writes_a_config_and_that_config_runs(init: Init) -> None:
    assert init.init(init.answers()) == 0
    out, err = init.output()
    assert err == ""
    steps = (
        "Connectors:",
        "Compliance declaration",
        "Login methods:",
        "Looking at the source",
        "Pulling a sample of 4 item(s)",
        "The sample, normalised",
        "Sinks:",
        "Wrote ",
    )
    for step in steps:
        assert step in out, step
    assert "MOCK-0001" in out, "the preview shows real normalised values"
    assert f"catalog-sync --config {init.config} --workspace" not in out

    # What it wrote is a config the tool reads, with the declaration the gate accepts.
    config = load_config(init.config)
    source = config.sources["mock-materials"]
    assert source.plugin == "mock.materials" and source.options["credential"] == "mock"
    assert source.options["base_url"] == init.services.saas.base_url
    today = datetime.now(UTC).date()
    declaration = parse_compliance(source.compliance, label="sources.mock-materials", today=today)
    assert (
        declaration.tos_reviewed_on == today
        and declaration.max_requests_per_minute == CEILING_PER_MINUTE
    )
    assert config.credentials["mock"].options == {"base_url": init.services.saas.base_url}
    assert {name: sink.kind for name, sink in config.sinks.items()} == {
        "local": "sqlite",
        "platform": "http",
    }
    assert config.sinks["local"].options["table"] == "materials"
    assert config.profiles["mock-materials"].sinks == ("local", "platform")
    assert stat.S_IMODE(init.config.stat().st_mode) == 0o644, "no secret in it: others may read it"

    assert init.run("run", "mock-materials") == 0
    seeded = {m.id for m in init.services.saas.service.dataset.materials}
    local = read_table(init.directory / "out" / "catalog.db", "materials")
    remote = read_table(init.services.platform.service.store.path, "material")
    assert {key[1] for key in local} == seeded == {key[1] for key in remote}


def test_the_profile_leaves_out_the_mapping_that_ships_with_the_connector(init: Init) -> None:
    assert init.init(init.answers()) == 0
    text = init.config.read_text(encoding="utf-8")
    assert "mapping" not in tomllib.loads(text)["profiles"]["mock-materials"]
    packaged = registry.default_mapping("mock.materials")
    assert packaged is not None and str(packaged) not in text, "no path into the install"
    assert load_config(init.config).profiles["mock-materials"].mapping is None


def test_no_secret_reaches_the_file_the_output_or_the_audit_log(init: Init) -> None:
    assert init.init(init.answers()) == 0
    assert init.run("run", "mock-materials") == 0
    out, err = init.output()
    audit_text = (init.workspace / "audit.jsonl").read_text(encoding="utf-8")
    for place, text in {
        "config": init.config.read_text(encoding="utf-8"),
        "output": out + err,
        "audit": audit_text,
    }.items():
        assert PLATFORM_TOKEN not in text, place
        assert ACCOUNT_PASSWORD not in text, place
        assert "Bearer " not in text, place
    assert 'auth_header_env = "PLATFORM_TOKEN"' in init.config.read_text(encoding="utf-8")


def test_every_step_is_audited_and_the_init_line_carries_the_declarations_hash(init: Init) -> None:
    assert init.init(init.answers()) == 0
    lines = init.audit()
    assert [line["command"] for line in lines] == ["login", "inspect", "pull", "normalize", "init"]
    login_line, inspect_line, pull_line, normalize_line, init_line = lines
    assert inspect_line["outcome"] == "ok" and inspect_line["source"] == "mock.materials"
    assert login_line["outcome"] == "ok"
    assert init_line["outcome"] == "ok" and init_line["sinks"] == ["local", "platform"]
    assert init_line["counts"] == {"sample_items": 4, "sinks": 2}
    config = load_config(init.config)
    declaration = parse_compliance(
        config.sources["mock-materials"].compliance,
        label="sources.mock-materials",
        today=datetime.now(UTC).date(),
    )
    digests = {line["compliance_sha256"] for line in (inspect_line, pull_line, init_line)}
    assert digests == {declaration.digest}
    assert inspect_line["credential_hash"] == pull_line["credential_hash"] is not None
    assert pull_line["run_id"] == normalize_line["run_id"] == init_line["run_id"]
    assert pull_line["counts"]["items"] == 4  # type: ignore[index]


def test_the_sample_pull_is_a_real_run_in_the_workspace(init: Init) -> None:
    assert init.init(init.answers()) == 0
    init.output()
    assert init.run("runs", "list") == 0
    assert "mock.materials" in init.output()[0]


def test_suppliers_ask_what_a_sink_does_with_the_field_the_mapping_calls_sensitive(
    init: Init,
) -> None:
    sinks = textwrap.dedent(
        """
        [[sinks]]
        kind = "csv"
        name = "sheet"
        path = "./out/suppliers.csv"
        sensitive_fields = "hash"
        """
    )
    assert init.init(init.answers(connector="mock.suppliers", sinks=sinks)) == 0
    out, _ = init.output()
    assert "hidden, marked sensitive in the mapping: contact_email" in out
    config = load_config(init.config)
    assert config.sinks["sheet"].options["sensitive_fields"] == "hash"
    assert init.run("run", "mock-suppliers") == 0
    sheet = (init.directory / "out" / "suppliers.csv").read_text(encoding="utf-8")
    assert "sha256:" in sheet and "@" not in sheet.replace("@@", ""), "hashed, never the address"


# --- what stops it --------------------------------------------------------------------------------


def test_an_existing_config_is_not_overwritten_without_force(init: Init) -> None:
    init.config.write_text("# mine\n", encoding="utf-8")
    assert init.init(init.answers()) == 1
    err = init.output()[1]
    assert "already exists" in err and "--force" in err
    assert init.config.read_text(encoding="utf-8") == "# mine\n"
    assert init.audit() == [] and init.services.saas.request_log == [], "nothing was contacted"

    assert init.init(init.answers(), "--force") == 0
    assert load_config(init.config).sources


def test_a_config_that_appears_while_the_wizard_runs_is_not_overwritten(
    init: Init, monkeypatch: pytest.MonkeyPatch
) -> None:
    confirm = flow.confirm_sample

    def someone_else_writes_one(prompter: Prompter) -> None:
        confirm(prompter)
        init.config.write_text("# made meanwhile\n", encoding="utf-8")

    monkeypatch.setattr(flow, "confirm_sample", someone_else_writes_one)
    assert init.init(init.answers()) == 1
    assert "already exists" in init.output()[1]
    assert init.config.read_text(encoding="utf-8") == "# made meanwhile\n"
    assert init.audit()[-1]["outcome"] == "error:ConfigError"


@pytest.mark.parametrize(
    "bad_url",
    ["not a url", "ftp://files.example/x", "http://", "http://ops:hunter2-canary@127.0.0.1:1/"],
)
def test_a_base_url_that_is_not_an_http_address_is_refused_before_anything_is_contacted(
    init: Init, bad_url: str
) -> None:
    base = init.services.saas.base_url
    text = (
        init.answers()
        .read_text(encoding="utf-8")
        .replace(f'base_url = "{base}"', f'base_url = "{bad_url}"')
    )
    path = init.directory / "answers-url.toml"
    path.write_text(text, encoding="utf-8")
    assert init.init(path) == 1
    out, err = init.output()
    assert "settings.base_url" in err and "'base_url' must" in err
    assert "hunter2-canary" not in out + err, "a password in a URL is not echoed"
    assert not init.config.exists() and init.services.saas.request_log == []
    lines = init.audit()
    assert [line["command"] for line in lines] == ["init"], "no step but the wizard's own ran"
    assert lines[0]["outcome"] == "error:ConfigError"


def test_the_shipped_example_answers_still_set_up_the_mock_connector(init: Init) -> None:
    """The file in examples/init is the one the README points at; it must keep working."""
    example = Path(__file__).parent.parent / "examples" / "init" / "mock-materials.toml"
    text = example.read_text(encoding="utf-8")
    for shipped, live in {
        "http://127.0.0.1:8765": init.services.saas.base_url,
        "http://127.0.0.1:8780/api/v1/ingest": init.services.platform.ingest_url,
        "../init-demo.db": "./init-demo.db",
    }.items():
        assert shipped in text, shipped
        text = text.replace(shipped, live)
    path = init.directory / "example-answers.toml"
    path.write_text(text, encoding="utf-8")
    assert init.init(path) == 0, init.output()[1]
    config = load_config(init.config)
    assert set(config.sinks) == {"local", "platform"} and "mock-materials" in config.profiles


def test_not_having_read_the_terms_stops_before_anything_is_contacted(init: Init) -> None:
    answers = (
        init.answers()
        .read_text(encoding="utf-8")
        .replace("tos_reviewed = true", "tos_reviewed = false")
    )
    path = init.directory / "answers-no.toml"
    path.write_text(answers, encoding="utf-8")
    assert init.init(path) == 4
    err = init.output()[1]
    assert "read the terms at" in err and "will not pick one for you" in err
    assert not init.config.exists()
    assert init.services.saas.request_log == [], "not one request reached the service"
    assert [line["command"] for line in init.audit()] == ["init"]
    assert init.audit()[0]["outcome"] == "error:ComplianceError"


def test_the_review_date_is_only_ever_defaulted_after_the_person_confirmed(init: Init) -> None:
    text = init.answers().read_text(encoding="utf-8")
    without_confirmation = text.replace("tos_reviewed = true\n", "")
    path = init.directory / "answers-silent.toml"
    path.write_text(without_confirmation, encoding="utf-8")
    assert init.init(path) == 1
    assert "no answer for 'compliance.tos_reviewed'" in init.output()[1]
    assert not init.config.exists() and init.services.saas.request_log == []


@pytest.mark.parametrize(
    ("line", "complaint"),
    [
        ('data_owner = "Demo Co (invented data)"', "data_owner"),
        ('authorization_ref = "demo/mock-saas"', "authorization_ref"),
        (f"max_requests_per_minute = {CEILING_PER_MINUTE}", "max_requests_per_minute"),
        ("retention_days = 30", "retention_days"),
        (None, None),
    ],
)
def test_every_declaration_field_must_be_answered_when_nobody_can_be_asked(
    init: Init, line: str | None, complaint: str | None
) -> None:
    text = init.answers().read_text(encoding="utf-8")
    if line is None:  # the tos_url: the connector's hint must not stand in for the answer
        line, complaint = f'tos_url = "{init.services.saas.base_url}/terms"', "tos_url"
    path = init.directory / "answers-short.toml"
    path.write_text(text.replace(line + "\n", ""), encoding="utf-8")
    assert init.init(path) == 1
    assert f"no answer for 'compliance.{complaint}'" in init.output()[1]
    assert not init.config.exists() and init.services.saas.request_log == []


@pytest.mark.parametrize(
    ("replacement", "complaint"),
    [
        ('data_owner = "<the organisation>"', "data_owner"),
        ('authorization = "owner"', "authorization"),
        ('tos_url = "ftp://x"', "tos_url"),
        ("retention_days = 0", "retention_days"),
        ('max_requests_per_minute = "fast"', "max_requests_per_minute"),
    ],
)
def test_a_declaration_the_gate_would_refuse_is_refused_at_the_question(
    init: Init, replacement: str, complaint: str
) -> None:
    text = init.answers().read_text(encoding="utf-8")
    key = replacement.split(" =")[0]
    lines = [replacement if line.startswith(key + " =") else line for line in text.splitlines()]
    path = init.directory / "answers-bad.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    code = init.init(path)
    err = init.output()[1]
    assert code in (1, 4) and f"compliance.{complaint}" in err
    assert not init.config.exists() and init.services.saas.request_log == []


def test_a_sample_with_a_failed_item_stops_the_wizard_and_points_at_the_run(
    init: Init, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalog_sync.errors import HttpStatusError
    from catalog_sync.models import JsonObject
    from catalog_sync.plugins.mock.api import MockApi

    real = MockApi.material_detail

    def gone_for_one(self: MockApi, item_id: str) -> JsonObject:
        if item_id == "mat-0007":
            raise HttpStatusError(404, "GET /api/v1/materials/mat-0007 answered HTTP 404")
        return real(self, item_id)

    monkeypatch.setattr(MockApi, "material_detail", gone_for_one)
    assert init.init(init.answers()) == 1
    err = init.output()[1]
    assert "the sample pull had 1 failed item(s)" in err and "catalog-sync runs show" in err
    assert not init.config.exists(), "a config that was never shown to work is not left behind"


def test_a_login_the_service_refuses_stops_the_wizard_with_exit_code_2(
    init: Init, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_SAAS_PASSWORD", "not-the-password")
    assert init.init(init.answers()) == 2
    assert not init.config.exists()
    assert [line["command"] for line in init.audit()] == ["login", "init"]


@pytest.mark.parametrize(
    "secret",
    [
        "Bearer " + "abcdef123456" * 3,
        # These two used to pass for a name: letters, digits and underscores only.
        "ghp" + "_" + "aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5",
        "AKIA" + "IOSFODNN7EXAMPLE",
    ],
    ids=["bearer", "github token", "aws key id"],
)
def test_a_secret_given_where_a_variable_name_belongs_is_refused_unrepeated_and_not_written(
    init: Init, secret: str
) -> None:
    sinks = init.default_sinks().replace(
        'auth_header_env = "PLATFORM_TOKEN"', f'auth_header_env = "{secret}"'
    )
    assert init.init(init.answers(sinks=sinks)) == 1
    out, err = init.output()
    assert "NAME of an environment variable" in err
    assert secret not in out + err and not init.config.exists()


def test_a_sink_the_loader_would_refuse_is_refused_before_the_file_is_written(init: Init) -> None:
    sinks = textwrap.dedent(
        """
        [[sinks]]
        kind = "http"
        name = "platform"
        url = "http://platform.internal/ingest"
        auth_header_env = "PLATFORM_TOKEN"
        """
    )
    assert init.init(init.answers(sinks=sinks)) == 1
    assert "plain http" in init.output()[1] and not init.config.exists()


def test_no_sink_in_a_non_interactive_run_is_an_error_naming_the_key(init: Init) -> None:
    assert init.init(init.answers(sinks="\n")) == 1
    assert "no answer for 'sinks'" in init.output()[1] and not init.config.exists()


def test_an_unknown_connector_lists_the_ones_there_are(init: Init) -> None:
    assert init.init(init.answers(connector="nope.nothing")) == 1
    message = init.output()[1]
    assert "choose one of:" in message and "mock.materials, mock.suppliers" in message


def test_an_answers_file_that_is_not_toml_is_an_error_not_a_traceback(init: Init) -> None:
    path = init.directory / "broken.toml"
    path.write_text("connector = \n", encoding="utf-8")
    assert init.init(path) == 1
    assert "is not valid TOML" in init.output()[1]
    assert init.init(init.directory / "missing.toml") == 1
    assert "cannot read the answers file" in init.output()[1]


def test_an_existing_broken_config_does_not_stop_init_from_replacing_it(init: Init) -> None:
    init.config.write_text("this is [not toml\n", encoding="utf-8")
    assert init.init(init.answers(), "--force") == 0
    tomllib.loads(init.config.read_text(encoding="utf-8"))


# --- at a terminal --------------------------------------------------------------------------------


def test_the_same_wizard_asked_at_a_terminal_with_mistakes_on_the_way(
    init: Init, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = init.services.saas.base_url
    typed = iter(
        [
            "1",  # connector: mock.materials
            "",  # name: the default
            base,  # base_url
            "Demo Co",  # data_owner
            "owner",  # authorization: not one of them
            "own_account",
            "demo/mock-saas",  # authorization_ref
            "",  # tos_url: the connector's hint, confirmed
            "maybe",  # read the terms?
            "y",
            "",  # the day: today
            str(CEILING_PER_MINUTE),  # max_requests_per_minute
            "0",  # retention_days: too small
            "30",
            "1",  # login: form
            "3",  # sample size
            "y",  # looks right
            "sqlite",  # sink
            "",  # name
            "",  # path
            "",  # table
            "n",  # another sink?
        ]
    )
    prompts: list[str] = []

    def type_next(prompt: str) -> str:
        prompts.append(prompt)
        return next(typed)

    monkeypatch.setattr("builtins.input", type_next)
    code = cli.main(["--config", str(init.config), "--workspace", str(init.workspace), "init"])
    out, err = init.output()
    assert code == 0, err
    assert "choose one of: own_account, customer_authorized, contract, public_data" in out
    assert "please answer yes or no" in out and "enter a whole number at least 1" in out
    assert any(p.startswith("tos_url [") and "/terms]" in p for p in prompts), "the hint is offered"
    config = load_config(init.config)
    assert config.sources["mock-materials"].compliance["retention_days"] == 30  # type: ignore[index]
    assert config.sources["mock-materials"].compliance["tos_url"] == f"{base}/terms"  # type: ignore[index]
    default_sink = {"path": "{workspace}/out/catalog.db", "table": "materials"}
    assert config.sinks["sqlite"].options == default_sink
    assert init.run("run", "mock-materials") == 0
    assert (init.workspace / "out" / "catalog.db").is_file(), "Enter at every question: workspace"
    assert not (init.directory / "out").exists(), "nothing was written beside the config"


def test_the_terminal_run_ends_cleanly_if_the_input_does(
    init: Init, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hang_up(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", hang_up)
    assert cli.main(["--config", str(init.config), "--workspace", str(init.workspace), "init"]) == 1
    assert "input ended before the wizard was finished" in init.output()[1]
    assert not init.config.exists()


def test_the_final_instructions_name_the_command_the_variables_and_the_login(init: Init) -> None:
    assert init.init(init.answers()) == 0
    out = init.output()[0]
    assert "catalog-sync --config" in out and "run mock-materials" in out
    assert "$PLATFORM_TOKEN" in out and "cron" in out
    assert "catalog-sync auth status mock" in out
