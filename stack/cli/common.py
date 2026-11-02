"""Options shared by every verb: ``--store`` and ``--plan`` are accepted both before the verb
(``stack --store X run …``) and after it (``stack run … --store X``); the verb's value wins.
Also the small helpers the verbs share (:func:`fail`, :func:`load_plan_or_fail`, :func:`pairs`).

**Defaults** (R6-5, round 5): ``--store`` defaults to ``v4/store-dev`` **under the repository**
(:data:`DEFAULT_STORE`, absolute: the shell's cwd does not move it); env ``STACK_STORE``. There is
**no default plan**: a verb that needs one (``run``, ``release``, ``fold``, ``diff … latest``, a
non-dry ``acquire reopen``, and ``eval triage``, ``screen``, ``affiliation``, ``model_roles``,
``technique_roles`` and ``audit-prompts``) refuses without ``--plan`` or ``STACK_PLAN``
(:func:`load_plan_or_fail`); ``build`` takes its plan as an argument. **A missing store is never
created silently**: ``run``, ``release``, a non-dry ``build`` and a non-dry ``overrides add`` refuse
a store root that does not exist unless ``--create`` (:func:`require_store`, which makes the
directory: a store is a directory); ``fold``, ``migrate``, ``verify`` and a non-dry ``acquire
reopen`` refuse one, with no ``--create``. Dry runs and read-only verbs never create one.

**S3 stores** (D321): ``--store s3://<bucket>/<prefix>`` names the mirror directory
``$STACK_MIRROR/<bucket>/<prefix>/`` (default ``v4/mirror`` under the repository,
:data:`DEFAULT_MIRROR`), bound by its ``.stack-mirror.json`` (:func:`stack.store.remote.resolve_store`,
called once in :func:`resolve_globals`); a mirror named by its path is refused. ``Globals.store`` is
always the local root. No verb prints the bucket (D540): :func:`fail` masks it, and a verb that echoes
a store path or URI passes it through :func:`shown` (``s3://<bucket>/<prefix>``,
``…/mirror/<bucket>/<prefix>``); a store-config or S3 client error no verb handles is a masked
``error:`` line at the CLI boundary (:class:`StackGroup`, D544), not a traceback."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from typer.core import TyperGroup

from stack.plan import Plan, PlanError, load_plan
from stack.store.layout import COMMENT_RE
from stack.store.remote import StoreError, mask_buckets, mask_s3_text, resolve_store

#: The repository root (``stack/cli/common.py`` → ``../..``).
REPO_ROOT = Path(__file__).resolve().parents[2]
#: The default store: the repository's dev store, whatever the cwd (R6-5).
DEFAULT_STORE = REPO_ROOT / "v4" / "store-dev"
#: Where ``s3://`` stores are mirrored when ``STACK_MIRROR`` is unset (D321).
DEFAULT_MIRROR = REPO_ROOT / "v4" / "mirror"
MIRROR_ENV = "STACK_MIRROR"
STORE_HELP = ("Store: a local root (data/, runs/, blobs/, …) or s3://<bucket>/<prefix> (mirrored under "
              "$STACK_MIRROR, default v4/mirror); default v4/store-dev under the repository.")
PLAN_HELP = "Plan file (configs/plans/*.yaml); no default (env STACK_PLAN)."
NO_PLAN = "no plan: pass --plan <configs/plans/<plan>.yaml> or set STACK_PLAN (there is no default plan)"


@dataclass(frozen=True)
class Globals:
    """``store`` is the local root (after :func:`resolve_globals`); ``store_arg`` the ``--store``
    value as given (a path or an ``s3://`` URI)."""

    store: Path
    plan: Path | None = None
    store_arg: str | None = None


#: ``--store``/``--plan`` after the verb (``Annotated`` options; default ``None`` = the global).
StoreOpt = Annotated[str | None, typer.Option("--store", help=STORE_HELP + " Overrides the global option.")]
PlanOpt = Annotated[Path | None, typer.Option("--plan", help=PLAN_HELP + " Overrides the global option.")]
#: ``--create`` of the verbs that write a store (:func:`require_store`).
CreateOpt = Annotated[bool, typer.Option(
    "--create", help="Create the store root when it does not exist (a store is never created silently).")]


def _check_comment(value: str | None) -> str | None:
    if value is not None and not COMMENT_RE.fullmatch(value):
        raise typer.BadParameter(f"{value!r}: a comment is {COMMENT_RE.pattern} (it becomes part of the job's and its files' names)")
    return value


#: ``--comment`` of ``build`` and ``run``: recorded in the job's record (D322).
#: ``--no-upload`` (``stack build``, ``stack run``; D352): the job writes its
#: records, log and data into the mirror only (blobs still go up); ``stack store push`` uploads it later.
NoUploadOpt = Annotated[bool, typer.Option(
    "--no-upload", help="Write this job's records, log and data only into the local mirror (an s3:// store); "
    "`stack store push` uploads it later.")]
CommentOpt = Annotated[str | None, typer.Option(
    "--comment", callback=_check_comment,
    help="A short label for this job ([a-z0-9-]+), recorded in its record and its file names.")]


def mirror_base() -> Path:
    """``$STACK_MIRROR`` (absolute), else :data:`DEFAULT_MIRROR`."""
    env = os.environ.get(MIRROR_ENV)
    return Path(env).absolute() if env else DEFAULT_MIRROR


def resolve_globals(ctx: typer.Context, store: str | Path | None, plan: Path | None) -> Globals:
    """The verb's options over the global ones. The store is resolved here, once, for every verb
    (:func:`stack.store.remote.resolve_store`): an ``s3://`` URI to its mirror directory, a path to
    itself made absolute (library code joins paths it was handed, already prefixed with the root,
    onto the root again when they are relative, so a relative ``--store`` read
    ``<store>/<store>/data/…``). A refused store (a mirror named by path, a changed binding) is
    :func:`fail`. ``plan`` stays ``None`` when neither the verb nor the global option (nor
    ``STACK_PLAN``) names one."""
    g: Globals = ctx.obj if isinstance(ctx.obj, Globals) else Globals(DEFAULT_STORE)
    arg = str(store) if store is not None else (g.store_arg if g.store_arg is not None else str(g.store))
    try:
        root = resolve_store(arg, mirror_base=mirror_base())
    except StoreError as exc:
        fail(str(exc))
    return Globals(store=root, plan=plan or g.plan, store_arg=arg)


def require_store(store: Path, create: bool) -> None:
    """Refuse (:func:`fail`) a store root that does not exist, unless ``create``: then make the
    directory (a store is a directory; no ``store.json``, D321). Called by the verbs that write a
    store, on their non-dry path only (R6-5)."""
    if store.exists():
        if not store.is_dir():
            fail(f"store root {store} is not a directory")
        return
    if not create:
        fail(f"no store at {store}: a store is never created silently; pass --create to make a new "
             "one there (or --store/STACK_STORE for an existing one)")
    store.mkdir(parents=True)
    typer.echo(shown(f"created store {store}"), err=True)


def shown(text: str | Path) -> str:
    """``text`` as a verb may print it: every S3 bucket name masked (D540,
    :func:`stack.store.remote.mask_buckets`)."""
    return mask_buckets(str(text))


class StackGroup(TyperGroup):
    """The CLI boundary (D544): a store-config error (:class:`~stack.store.s3config.S3ConfigError`,
    :class:`~stack.store.remote.StoreError`) or an S3 client error (botocore's ``BotoCoreError``,
    ``ClientError``, and :class:`~stack.store.s3config.S3TransientError`) that a verb did not handle
    is ``error: …`` (masked: :func:`shown`, :func:`stack.store.remote.mask_s3_text`), exit 1, not a
    traceback. Every other exception is still a traceback (a bug)."""

    def invoke(self, ctx: Any) -> Any:  # a click Context: click's own, or typer's vendored one (typer >= 0.26)
        from botocore.exceptions import BotoCoreError, ClientError

        from stack.store.s3config import S3ConfigError, S3TransientError

        try:
            return super().invoke(ctx)
        except (S3ConfigError, StoreError) as exc:
            fail(str(exc))
        except (BotoCoreError, ClientError, S3TransientError) as exc:
            fail(f"S3: {type(exc).__name__}: {mask_s3_text(str(exc))}")


def fail(message: str) -> NoReturn:
    """An expected refusal: ``error: <message>`` on stderr (the bucket masked, :func:`shown`), exit 1
    (the package docstring's rule)."""
    typer.echo(f"error: {shown(message)}", err=True)
    raise typer.Exit(1)


def load_plan_or_fail(path: Path | None) -> Plan:
    """The plan at ``path``; no plan (``None``: neither ``--plan`` nor ``STACK_PLAN``) or an
    unreadable or invalid one is :func:`fail`."""
    if path is None:
        fail(NO_PLAN)
    try:
        return load_plan(path)
    except PlanError as exc:
        fail(str(exc))


def pairs(values: list[str] | None, flag: str) -> list[tuple[str, str]]:
    """``<key>=<value>`` options (``stack fold --input``/``--join``, ``stack build --filter``/``--max-usd``)
    as pairs."""
    out = []
    for v in values or []:
        key, sep, ref = v.partition("=")
        if not sep or not key or not ref:
            raise typer.BadParameter(f"{flag} {v!r}: expected <key>=<value>")
        out.append((key, ref))
    return out


def echo_json(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))
