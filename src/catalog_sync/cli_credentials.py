"""The ``login`` and ``auth status`` commands, and resolving a credential name for other commands.

A credential name (``mock``) resolves to a *kind* (how that service's credential looks and how
to obtain one, supplied by a plugin) plus settings from the optional ``[credentials.NAME]`` table.
Without a table the name itself must be an installed kind.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from catalog_sync import registry
from catalog_sync.audit import AuditLog, AuditScope, audited, hash_token_id
from catalog_sync.compliance import Compliance, gate_source
from catalog_sync.config import DEFAULT_RENEW_BEFORE_DAYS
from catalog_sync.credentials.base import (
    Credential,
    CredentialHandle,
    CredentialProvider,
    renewal_hint,
)
from catalog_sync.credentials.browser import BrowserLoginProvider
from catalog_sync.credentials.curl_paste import CurlPasteProvider
from catalog_sync.credentials.form import FormLoginProvider
from catalog_sync.credentials.freshness import (
    Freshness,
    FreshnessReport,
    assess,
    renewal_window_warning,
)
from catalog_sync.credentials.jwt import describe_token_id
from catalog_sync.credentials.kind import CredentialKind
from catalog_sync.credentials.store import credential_path, load_credential
from catalog_sync.errors import (
    AuthExpired,
    ComplianceError,
    ConfigError,
    CredentialMissing,
    LoginFailed,
)
from catalog_sync.http import RateLimiter, shared_limiter

if TYPE_CHECKING:
    import argparse

    from catalog_sync.cli import Context

HEADLESS_TIMEOUT_S = 90.0  # a form submit, a redirect and the app's first calls
HEADED_TIMEOUT_S = 300.0  # a person typing, possibly with a second factor


@dataclass(frozen=True)
class ResolvedCredential:
    name: str
    kind: CredentialKind
    path: Path
    renew_before_days: float
    schedule_interval_days: float | None

    def load(self) -> Credential:
        return load_credential(self.path, self.kind.layout)

    def assess(self, credential: Credential) -> FreshnessReport:
        return assess(credential, now=_utc_now(), renew_before_days=self.renew_before_days)

    def window_warning(self) -> str | None:
        return renewal_window_warning(
            renew_before_days=self.renew_before_days,
            schedule_interval_days=self.schedule_interval_days,
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def resolve_credential(name: str, context: Context) -> ResolvedCredential:
    config = context.config.credentials.get(name) if context.config else None
    kind_name = config.kind if config else name
    installed = registry.available(registry.CREDENTIALS_GROUP)
    if kind_name not in installed:
        known = ", ".join(installed) or "none"
        if config:
            raise ConfigError(
                f"credential {name!r} uses kind {kind_name!r}, which is not installed "
                f"(installed kinds: {known})"
            )
        raise ConfigError(
            f"unknown credential {name!r}: no [credentials.{name}] table in the config and no "
            f"installed credential kind of that name (installed kinds: {known})"
        )
    base_dir = context.config.base_dir if context.config else Path.cwd()
    kind = registry.credential_kind_factory(kind_name)(
        config.options if config else {}, base_dir=base_dir
    )
    return ResolvedCredential(
        name=name,
        kind=kind,
        path=credential_path(context.workspace.credentials_dir, name),
        renew_before_days=config.renew_before_days if config else DEFAULT_RENEW_BEFORE_DAYS,
        schedule_interval_days=config.schedule_interval_days if config else None,
    )


def credential_option(options: Mapping[str, object], context: Context) -> dict[str, object]:
    """Replace a source's ``credential`` name by a handle, after checking it is usable.

    The check runs before the first request: an expired credential found here costs nothing,
    found by the service it costs a failed run.
    """
    name = options.get("credential")
    if not isinstance(name, str):
        return dict(options)
    resolved = resolve_credential(name, context)
    credential = resolved.load()
    report = resolved.assess(credential)
    _complain_if_not_fresh(resolved, report)
    handle = CredentialHandle(
        name, partial(load_credential, resolved.path, resolved.kind.layout), renewal_hint(name)
    )
    return {**options, "credential": handle}


def _complain_if_not_fresh(resolved: ResolvedCredential, report: FreshnessReport) -> None:
    name = resolved.name
    if report.state is Freshness.EXPIRED:
        raise AuthExpired(
            f"credential {name!r} expired {abs(report.days_left or 0):.1f} day(s) ago. "
            f"{renewal_hint(name)}"
        )
    if report.state is Freshness.RENEW_SOON:
        print(
            f"warning: credential {name!r} expires in {report.days_left:.1f} day(s). "
            f"{renewal_hint(name)}",
            file=sys.stderr,
        )
    if report.state is Freshness.UNKNOWN:
        print(
            f"warning: cannot read the expiry of credential {name!r}; it may be expired.",
            file=sys.stderr,
        )
    warning = resolved.window_warning()
    if warning:
        print(f"warning: {warning}", file=sys.stderr)


# --- login --------------------------------------------------------------------------------------


def cmd_login(args: argparse.Namespace, context: Context) -> int:
    log = AuditLog(context.workspace.audit_path, clock=context.clock)
    # The audit line names the credential as its "source": it is what was logged into.
    with audited(log, "login", source=args.credential) as scope:
        return _login(args, context, scope)


def _login(args: argparse.Namespace, context: Context, scope: AuditScope) -> int:
    _check_login_flags(args)
    resolved = resolve_credential(args.credential, context)
    limiter = _gate_login(args, resolved, context, scope)
    previous = _try_load(resolved)
    state = resolved.assess(previous).state if previous else None
    if state is Freshness.FRESH and not args.force:
        scope.credential_hash = hash_token_id(previous.token_id if previous else None)
        scope.outcome = "unchanged"
        print(
            f"credential {resolved.name!r} is still valid ({_describe_expiry(previous)}); "
            "nothing to do. Use --force to log in again anyway."
        )
        return 0
    # A token that is about to lapse is replaced, not re-read: re-reading hands back the same one.
    force_login = args.force or state in (Freshness.RENEW_SOON, Freshness.EXPIRED)
    # Every request the login makes, the connector's own check included, counts against the
    # ceiling the source declared.
    with _within(limiter):
        if args.paste_curl:
            provider: CredentialProvider = CurlPasteProvider(
                path=resolved.path,
                layout=resolved.kind.layout,
                pasted=_read_pasted(args.paste_curl),
            )
            method = "pasted cURL command"
            credential = provider.refresh(force=force_login)
        elif args.form:
            method = "HTTP login"
            credential = _form_login(resolved).refresh(force=force_login)
        else:
            method = "browser login"
            credential = _browser_login(resolved, context, args, force_login=force_login)
    scope.credential_hash = hash_token_id(credential.token_id)
    _print_stored(resolved, credential, method)
    return 0


def _within(limiter: RateLimiter | None) -> AbstractContextManager[None]:
    return nullcontext() if limiter is None else shared_limiter(limiter)


def _gate_login(
    args: argparse.Namespace, resolved: ResolvedCredential, context: Context, scope: AuditScope
) -> RateLimiter | None:
    """The compliance check of a login that contacts the service: ``--form`` and ``--browser``.

    Like a pull, it needs a valid declaration, and it finds it through the sources that use this
    credential: those are the ones whose declared ceiling the login's requests count against (the
    lowest, when several use it). A credential no source uses has no declaration to hold the login
    to, so it is refused (exit 4). ``--paste-curl`` only reads text, so it needs none.
    """
    if args.paste_curl:
        return None
    _require_login_support(args, resolved.kind)
    declarations = _declarations_using(resolved.name, context)
    if not declarations:
        raise ComplianceError(
            f"login --{'form' if args.form else 'browser'} contacts the service, so it needs a "
            f"compliance declaration, and no [sources.*] entry uses credential {resolved.name!r}. "
            f"Add a source with credential = {resolved.name!r} and a [sources.NAME.compliance] "
            "table (`catalog-sync init` writes both). `login --paste-curl` makes no request and "
            "needs neither."
        )
    today = context.clock().date()
    for declaration in declarations:
        warning = declaration.tos_review_warning(today)
        if warning:
            print(f"warning: credential {resolved.name!r}: {warning}", file=sys.stderr)
    strictest = min(declarations, key=lambda declaration: declaration.max_requests_per_minute)
    scope.compliance_sha256 = strictest.digest
    return RateLimiter(strictest.max_requests_per_minute)


def _declarations_using(credential: str, context: Context) -> list[Compliance]:
    """The valid declarations of the networked sources whose ``credential`` option is this one.
    Raises ComplianceError if one of them is not valid."""
    if context.config is None:
        return []
    found: list[Compliance] = []
    for entry in context.config.sources.values():
        if entry.options.get("credential") != credential:
            continue
        gate = gate_source(
            entry.name,
            entry,
            plugin=entry.plugin,
            options=entry.options,
            today=context.clock().date(),
        )
        if gate.compliance is not None:
            found.append(gate.compliance)
    return found


def _check_login_flags(args: argparse.Namespace) -> None:
    chosen = [flag for flag in ("paste_curl", "form", "browser") if getattr(args, flag)]
    if len(chosen) > 1:
        named = " and ".join("--" + flag.replace("_", "-") for flag in chosen)
        raise ConfigError(f"choose one way to log in, not both {named}")
    if not chosen:
        raise ConfigError(
            "choose how to log in: --paste-curl FILE (or '-' for stdin), --form or --browser"
        )
    if args.headed and not args.browser:
        raise ConfigError("--headed only applies to --browser")


def _read_pasted(source: str) -> str:
    if source == "-":
        return sys.stdin.read()
    try:
        return Path(source).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {source}: {exc.strerror}") from exc


def _require_login_support(args: argparse.Namespace, kind: CredentialKind) -> None:
    if args.form and kind.form_login is None:
        raise ConfigError(f"credential kind {kind.name!r} has no HTTP login; use --paste-curl")
    if args.browser and (kind.browser is None or kind.verify is None):
        raise ConfigError(f"credential kind {kind.name!r} does not support browser login")


def _form_login(resolved: ResolvedCredential) -> FormLoginProvider:
    kind = resolved.kind
    form_login = kind.form_login
    if form_login is None:
        raise AssertionError("_require_login_support has checked that the kind has an HTTP login")
    return FormLoginProvider(
        path=resolved.path,
        layout=kind.layout,
        login=lambda: form_login(os.environ),
        verify=kind.verify,
    )


def _browser_login(
    resolved: ResolvedCredential, context: Context, args: argparse.Namespace, *, force_login: bool
) -> Credential:
    kind = resolved.kind
    site, verify = kind.browser, kind.verify
    if site is None or verify is None:
        raise AssertionError("_require_login_support has checked that the kind has a browser login")

    def provider(*, headed: bool) -> BrowserLoginProvider:
        return BrowserLoginProvider(
            path=resolved.path,
            layout=kind.layout,
            site=site,
            profile_dir=context.workspace.browser_profile_dir(resolved.name),
            # In a visible window a person logs in; the tool never types for them.
            account=None if headed else kind.account(os.environ),
            headless=not headed,
            timeout_s=HEADED_TIMEOUT_S if headed else HEADLESS_TIMEOUT_S,
            verify=verify,
        )

    if args.headed:
        return provider(headed=True).refresh(force=force_login)
    try:
        return provider(headed=False).refresh(force=force_login)
    except LoginFailed as exc:
        if not _is_interactive():
            raise
        print(
            f"headless login failed: {exc}\nOpening a visible window: log in there, the tool "
            "will only take the token.",
            file=sys.stderr,
        )
        return provider(headed=True).refresh(force=force_login)


def _print_stored(resolved: ResolvedCredential, credential: Credential, method: str) -> None:
    layout = resolved.kind.layout
    present = [key for key, header in layout.headers.items() if header in credential.headers]
    absent = [key for key in layout.headers if key not in present]
    print(f"credential {resolved.name!r} stored ({method}), {_describe_expiry(credential)}")
    print(f"  token id: {describe_token_id(credential.token_id)}")
    print(f"  keys: {', '.join(present)}")
    if absent:
        print(f"  not captured: {', '.join(absent)}")


# --- auth status --------------------------------------------------------------------------------


def cmd_auth_status(args: argparse.Namespace, context: Context) -> int:
    resolved = resolve_credential(args.credential, context)
    credential = resolved.load()
    report = resolved.assess(credential)
    print(f"credential: {resolved.name} (kind {resolved.kind.name})")
    print(f"state:      {report.state.replace('_', ' ')}")
    print(f"expiry:     {_describe_expiry(credential)}")
    print(f"token id:   {describe_token_id(credential.token_id)}")
    print(f"renew when: fewer than {resolved.renew_before_days:g} day(s) are left")
    warning = resolved.window_warning()
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    if report.state is Freshness.EXPIRED:
        print(renewal_hint(resolved.name), file=sys.stderr)
        return AuthExpired.exit_code
    return 0


# --- shared -------------------------------------------------------------------------------------


def _try_load(resolved: ResolvedCredential) -> Credential | None:
    try:
        return resolved.load()
    except CredentialMissing:
        return None


def _describe_expiry(credential: Credential | None) -> str:
    if credential is None or credential.expires_at is None:
        return "expiry unknown"
    days = (credential.expires_at - _utc_now()).total_seconds() / 86_400
    when = credential.expires_at.strftime("%Y-%m-%d %H:%M UTC")
    if days <= 0:
        return f"expired {when} ({-days:.1f} day(s) ago)"
    return f"expires {when} ({days:.1f} day(s) left)"
