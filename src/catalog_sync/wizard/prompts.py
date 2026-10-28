"""Asking questions, at the terminal or from an answers file, through one code path.

Every question has a key (``compliance.data_owner``) and a parser that turns the text of an answer
into a value or raises ``ConfigError`` saying what is wrong. Where the answer comes from:

* the **answers file** (TOML, nested like the keys), if it has one. A bad answer there is an
  error naming the key, never a re-prompt: a script cannot be asked again.
* else, at a **terminal**, ``input()``: the default (shown in brackets, taken on Enter) or a
  *suggestion* (the same, for a value the person must still confirm), and a re-prompt on a bad
  answer.
* else, when not interactive (``--non-interactive``): the **default**, if the question has one.
  A *suggestion* is not applied: it is a guess about a fact only the operator knows (the rate a
  service allows, where its terms are), and a script that omits the answer has not confirmed it.
  A question with neither is an error that names the key to add to the answers file.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import TypeVar

from catalog_sync.connector_info import ENV_NAME, ENV_NAME_RULE
from catalog_sync.errors import CatalogSyncError, ComplianceError, ConfigError

T = TypeVar("T")
Parser = Callable[[str], T]
_YES = frozenset({"y", "yes", "true", "1"})
_NO = frozenset({"n", "no", "false", "0"})


class Prompter:
    def __init__(
        self,
        answers: Mapping[str, object],
        *,
        interactive: bool,
        read_line: Callable[[str], str] | None = None,
        write: Callable[[str], None] = print,
        prefix: str = "",
    ) -> None:
        self._answers = answers
        self._interactive = interactive
        self._read_line = read_line or _input
        self._write = write
        self._prefix = prefix

    @property
    def is_interactive(self) -> bool:
        return self._interactive

    def say(self, text: str = "") -> None:
        self._write(text)

    def has_answer(self, key: str) -> bool:
        return key in self._answers

    def child(self, key: str) -> Prompter:
        """The questions under `key`, answered by the table of that name (or by nobody)."""
        table = self._answers.get(key, {})
        if not isinstance(table, dict):
            raise ConfigError(f"the answers file: '{self._full(key)}' must be a table")
        return self._derived(table, key)

    def children(self, key: str) -> list[Prompter]:
        """One prompter per table of the array `key` (``[[sinks]]``); none when there is no such
        array."""
        entries = self._answers.get(key, [])
        if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
            raise ConfigError(f"the answers file: '{self._full(key)}' must be an array of tables")
        return [self._derived(entry, f"{key}[{number}]") for number, entry in enumerate(entries)]

    def ask(
        self,
        key: str,
        question: str,
        parse: Parser[T],
        *,
        default: str | None = None,
        suggestion: str | None = None,
        help_text: str | None = None,
    ) -> T:
        if key in self._answers:
            return self._from_file(key, question, parse)
        if not self._interactive:
            return self._without_a_person(key, question, parse, default)
        return self._at_the_terminal(question, parse, default or suggestion, help_text)

    def ask_optional(
        self, key: str, question: str, parse: Parser[T], *, help_text: str | None = None
    ) -> T | None:
        """Like `ask`, but nothing is a fine answer: an empty line at the terminal, or no entry in
        the answers file, gives None."""
        if key in self._answers:
            return self._from_file(key, question, parse)
        if not self._interactive:
            return None
        if help_text:
            self._write(help_text)
        typed = self._read(f"{question} (empty for none): ")
        while typed:
            try:
                return parse(typed)
            except CatalogSyncError as exc:
                self._write(f"  {exc}")
            typed = self._read(f"{question} (empty for none): ")
        return None

    # --- the three sources of an answer -----------------------------------------------------

    def _from_file(self, key: str, question: str, parse: Parser[T]) -> T:
        text = _as_text(self._answers[key])
        try:
            value = parse(text)
        except CatalogSyncError as exc:
            raise _in_file(self._full(key), exc) from exc
        self._write(f"  {question} {text}")
        return value

    def _without_a_person(
        self, key: str, question: str, parse: Parser[T], default: str | None
    ) -> T:
        if default is None:
            raise ConfigError(
                f"no answer for '{self._full(key)}' ({question}). Add it to the answers file: "
                "this run is not interactive and the question has no default"
            )
        try:
            value = parse(default)
        except CatalogSyncError as exc:
            raise _in_file(self._full(key), exc) from exc
        self._write(f"  {question} {default} (the default)")
        return value

    def _at_the_terminal(
        self, question: str, parse: Parser[T], preset: str | None, help_text: str | None
    ) -> T:
        if help_text:
            self._write(help_text)
        shown = f" [{preset}]" if preset else ""
        while True:
            typed = self._read(f"{question}{shown}: ")
            text = typed or (preset or "")
            if not text:
                self._write("  an answer is needed here")
                continue
            try:
                return parse(text)
            except CatalogSyncError as exc:
                self._write(f"  {exc}")

    def _read(self, prompt: str) -> str:
        try:
            return self._read_line(prompt).strip()
        except EOFError as exc:
            raise ConfigError("the input ended before the wizard was finished") from exc

    def _derived(self, answers: Mapping[str, object], name: str) -> Prompter:
        return Prompter(
            answers,
            interactive=self._interactive,
            read_line=self._read_line,
            write=self._write,
            prefix=self._full(name),
        )

    def _full(self, key: str) -> str:
        return f"{self._prefix}.{key}" if self._prefix else key


def _input(prompt: str) -> str:
    """Looked up when called, so that a test (or a wrapper) replacing ``input`` is heard."""
    return input(prompt)


def _as_text(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _in_file(key: str, exc: CatalogSyncError) -> CatalogSyncError:
    """The same kind of error (a compliance problem keeps its exit code), naming the answer."""
    message = f"the answers file, '{key}': {exc}"
    return ComplianceError(message) if isinstance(exc, ComplianceError) else ConfigError(message)


# --- parsers ------------------------------------------------------------------------------------


def non_empty_text(text: str) -> str:
    if not text.strip():
        raise ConfigError("an answer is needed here")
    return text.strip()


def yes_no(text: str) -> bool:
    word = text.strip().lower()
    if word in _YES:
        return True
    if word in _NO:
        return False
    raise ConfigError("please answer yes or no")


def choice(options: Sequence[str]) -> Parser[str]:
    """One of `options`, by name or by its number in the list shown (1 is the first)."""

    def parse(text: str) -> str:
        word = text.strip()
        if word in options:
            return word
        if word.isdigit() and 1 <= int(word) <= len(options):
            return options[int(word) - 1]
        raise ConfigError(f"choose one of: {', '.join(options)} (or its number)")

    return parse


def whole_number(minimum: int, maximum: int | None = None) -> Parser[int]:
    def parse(text: str) -> int:
        word = text.strip()
        if (
            not word.isdigit()
            or int(word) < minimum
            or (maximum is not None and int(word) > maximum)
        ):
            bound = f"at least {minimum}" if maximum is None else f"between {minimum} and {maximum}"
            raise ConfigError(f"enter a whole number {bound}")
        return int(word)

    return parse


def iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text.strip())
    except ValueError as exc:
        raise ConfigError("enter a date as YYYY-MM-DD, for example 2026-10-26") from exc


def env_var_name(text: str) -> str:
    """The name of an environment variable. What does not look like one is refused without being
    repeated: it may be the secret itself, pasted by mistake."""
    word = text.strip()
    if ENV_NAME.fullmatch(word) is None:
        raise ConfigError(
            f"give the NAME of an environment variable ({ENV_NAME_RULE}), not the secret itself; "
            "the secret goes in that variable"
        )
    return word
