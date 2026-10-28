"""Asking a question: from the answers file, at the terminal, and when nobody can be asked."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest

from catalog_sync.errors import ComplianceError, ConfigError
from catalog_sync.wizard.prompts import (
    Prompter,
    choice,
    env_var_name,
    iso_date,
    non_empty_text,
    whole_number,
    yes_no,
)


class Terminal:
    """Typed lines, and what the wizard printed."""

    def __init__(self, *lines: str) -> None:
        self._lines: Iterator[str] = iter(lines)
        self.shown: list[str] = []
        self.prompts: list[str] = []

    def read(self, prompt: str) -> str:
        self.prompts.append(prompt)
        try:
            return next(self._lines)
        except StopIteration:
            raise EOFError from None

    def prompter(
        self, answers: dict[str, object] | None = None, *, interactive: bool = True
    ) -> Prompter:
        return Prompter(
            answers or {}, interactive=interactive, read_line=self.read, write=self.shown.append
        )


# --- the answers file -----------------------------------------------------------------------------


def test_an_answer_in_the_file_wins_and_is_parsed_from_its_text() -> None:
    terminal = Terminal()
    ask = terminal.prompter({"count": 7, "flag": True, "day": date(2026, 10, 26), "name": " x "})
    assert ask.ask("count", "How many?", whole_number(1)) == 7
    assert ask.ask("flag", "Sure?", yes_no) is True
    assert ask.ask("day", "When?", iso_date) == date(2026, 10, 26)
    assert ask.ask("name", "Name?", non_empty_text) == "x"
    assert terminal.prompts == [], "a person was never asked"


def test_a_bad_answer_in_the_file_is_an_error_naming_the_key_not_a_question() -> None:
    terminal = Terminal("3")
    ask = terminal.prompter({"count": "many"})
    with pytest.raises(ConfigError, match=r"the answers file, 'count': enter a whole number"):
        ask.ask("count", "How many?", whole_number(1))
    assert terminal.prompts == [], "a script cannot be asked again"


def test_a_compliance_problem_in_the_file_keeps_its_exit_code() -> None:
    def refuse(_text: str) -> str:
        raise ComplianceError("not acceptable")

    with pytest.raises(ComplianceError) as caught:
        Terminal().prompter({"x": "1"}).ask("x", "Q?", refuse)
    assert caught.value.exit_code == 4 and "'x'" in str(caught.value)


def test_nested_keys_are_named_in_full() -> None:
    ask = Terminal().prompter({"compliance": {"retention_days": "zero"}}).child("compliance")
    with pytest.raises(ConfigError, match=r"'compliance\.retention_days'"):
        ask.ask("retention_days", "Days?", whole_number(1))


def test_children_are_the_tables_of_an_array_and_none_without_one() -> None:
    ask = Terminal().prompter({"sinks": [{"kind": "csv"}, {"kind": "jsonl"}]})
    assert [c.ask("kind", "Kind?", non_empty_text) for c in ask.children("sinks")] == [
        "csv",
        "jsonl",
    ]
    assert Terminal().prompter({}).children("sinks") == []
    with pytest.raises(ConfigError, match="'sinks' must be an array of tables"):
        Terminal().prompter({"sinks": "csv"}).children("sinks")
    with pytest.raises(ConfigError, match="'login' must be a table"):
        Terminal().prompter({"login": 3}).child("login")


# --- not interactive ------------------------------------------------------------------------------


def test_without_a_person_the_default_applies_and_a_missing_answer_names_its_key() -> None:
    ask = Terminal().prompter(interactive=False)
    assert ask.ask("sample.items", "How many?", whole_number(1), default="5") == 5
    with pytest.raises(ConfigError, match=r"no answer for 'sample\.size'.*not interactive"):
        ask.ask("sample.size", "How many?", whole_number(1))


def test_a_suggestion_is_not_applied_when_nobody_can_confirm_it() -> None:
    """The rate a service allows, where its terms are: a guess about a fact only the operator
    knows. A script that left the answer out has not said it."""
    with pytest.raises(ConfigError, match="no answer for 'max_requests_per_minute'"):
        Terminal().prompter(interactive=False).ask(
            "max_requests_per_minute", "Rate?", whole_number(1), suggestion="60"
        )


def test_an_optional_question_has_no_answer_when_nobody_gives_one() -> None:
    assert (
        Terminal().prompter(interactive=False).ask_optional("token", "Token var?", env_var_name)
        is None
    )
    assert (
        Terminal()
        .prompter({"token": "PLATFORM_TOKEN"}, interactive=False)
        .ask_optional("token", "Token var?", env_var_name)
        == "PLATFORM_TOKEN"
    )


# --- at the terminal ------------------------------------------------------------------------------


def test_enter_takes_the_default_or_the_suggestion_and_shows_it() -> None:
    terminal = Terminal("", "", "12")
    ask = terminal.prompter()
    assert ask.ask("a", "Pages?", whole_number(1), default="5") == 5
    assert ask.ask("b", "Rate?", whole_number(1), suggestion="60") == 60
    assert ask.ask("c", "Days?", whole_number(1), default="90") == 12
    assert terminal.prompts == ["Pages? [5]: ", "Rate? [60]: ", "Days? [90]: "]


def test_a_bad_answer_is_explained_and_asked_again() -> None:
    terminal = Terminal("zero", "0", "4")
    assert terminal.prompter().ask("n", "How many?", whole_number(1)) == 4
    assert terminal.shown.count("  enter a whole number at least 1") == 2
    assert len(terminal.prompts) == 3


def test_an_empty_answer_with_nothing_to_fall_back_on_is_asked_again() -> None:
    terminal = Terminal("", "Acme")
    assert terminal.prompter().ask("o", "Owner?", non_empty_text) == "Acme"
    assert "  an answer is needed here" in terminal.shown


def test_help_text_is_shown_once_before_the_question() -> None:
    terminal = Terminal("x", "y")
    terminal.prompter().ask("a", "A?", non_empty_text, help_text="  about A")
    assert terminal.shown == ["  about A"]


def test_the_input_ending_is_an_error_not_a_hang_or_a_traceback() -> None:
    with pytest.raises(ConfigError, match="input ended before the wizard was finished"):
        Terminal().prompter().ask("a", "A?", non_empty_text)


def test_an_optional_question_takes_an_empty_line_as_none_and_asks_again_on_a_bad_one() -> None:
    assert Terminal("").prompter().ask_optional("v", "Var?", env_var_name) is None
    terminal = Terminal("not a name!", "PLATFORM_TOKEN")
    assert terminal.prompter().ask_optional("v", "Var?", env_var_name) == "PLATFORM_TOKEN"
    assert any("NAME of an environment variable" in line for line in terminal.shown)


def test_the_real_input_function_is_used_and_looked_up_when_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt: "typed")
    assert (
        Prompter({}, interactive=True, write=lambda _: None).ask("a", "A?", non_empty_text)
        == "typed"
    )


# --- the parsers ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("y", True),
        ("YES", True),
        ("true", True),
        ("1", True),
        ("n", False),
        ("No", False),
        ("0", False),
    ],
)
def test_yes_and_no_in_the_usual_spellings(text: str, expected: bool) -> None:
    assert yes_no(text) is expected


def test_anything_else_is_not_a_yes_or_a_no() -> None:
    with pytest.raises(ConfigError, match="yes or no"):
        yes_no("maybe")


def test_a_choice_by_name_or_by_number_and_nothing_else() -> None:
    pick = choice(["form", "browser", "paste"])
    assert (pick("browser"), pick("1"), pick(" 3 ")) == ("browser", "form", "paste")
    for bad in ("4", "0", "brows", "", "-1"):
        with pytest.raises(ConfigError, match="choose one of: form, browser, paste"):
            pick(bad)


@pytest.mark.parametrize("text", ["0", "-1", "2.5", "x", "", "101"])
def test_a_number_outside_its_bounds_is_refused(text: str) -> None:
    with pytest.raises(ConfigError, match="whole number between 1 and 100"):
        whole_number(1, 100)(text)


def test_dates_are_iso() -> None:
    assert iso_date("2026-10-26") == date(2026, 10, 26)
    for bad in ("26/10/2026", "2026-13-01", "today", ""):
        with pytest.raises(ConfigError, match="YYYY-MM-DD"):
            iso_date(bad)


@pytest.mark.parametrize(
    "secret", ["Bearer abc.def", "sk-live-1234", "has space", "a=b", "", "9lead"]
)
def test_a_variable_name_is_a_name_and_a_secret_is_refused_unrepeated(secret: str) -> None:
    with pytest.raises(ConfigError, match="NAME of an environment variable") as caught:
        env_var_name(secret)
    assert secret == "" or secret not in str(caught.value)


@pytest.mark.parametrize(
    "secret",
    ["ghp" + "_" + "aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5", "AKIA" + "IOSFODNN7EXAMPLE", "A_" * 33],
    ids=["github token", "aws key id", "too long"],
)
def test_a_token_pasted_where_a_name_is_asked_is_refused_and_never_repeated(secret: str) -> None:
    with pytest.raises(ConfigError, match="NAME of an environment variable") as caught:
        env_var_name(secret)
    assert secret not in str(caught.value)
    assert "MY_SERVICE_TOKEN" in str(caught.value), "the message shows what a name looks like"


def test_a_variable_name_is_accepted() -> None:
    assert env_var_name(" PLATFORM_TOKEN ") == "PLATFORM_TOKEN"
    assert env_var_name("_X1") == "_X1"
