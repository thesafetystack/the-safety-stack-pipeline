"""Store root layout, file-name grammar, compound ids and the header key set (§2.2, §7.2, A.6, D16).

```
<root>/data/<family>/<series>/<utc>_<run_id>[_<comment>].<full|part>.parquet   a series file
<root>/data/<family>/<utc>_<run_id>[_<comment>].full.parquet                   a file without a series
<root>/data/<family>/<series>/.<name>.tmp                                      a run's staged output
<root>/builds/<head>.<plan>/build.json.zst                                 build record
<root>/builds/<head>.<plan>/runs/<head>.<stage>.json.zst                   a build's run record
<root>/runs/<head>.<stage>.json.zst                                        a standalone run record
<root>/blobs/<sha256>                                                      content-addressed, flat (D360)
<root>/releases/<tag>/     <root>/cache/
```

**Data files** (D372, D381; ``docs/OPERATE.md``, "Data files: series and chains"): ``<family>`` is
the family directory, today's chain id
(``documents``, ``corpus``, ``screen``, ``sources.<provider>``, ``coverage.<provider>``, …; the
schema family is the part before the first dot); ``<series>`` is ``[a-z0-9][a-z0-9-]*``;
``<utc>`` is ``YYYYMMDDTHHMMSSZ``, the run's own start to the second (bumped past the newest input,
:func:`tie_guard`); ``<comment>`` is ``[a-z0-9-]+``, a label only. A file without a series is
always full. Within a series directory names sort in time order; the chain is the directory
(:mod:`stack.store.chain`). The writer stage is in the record and the footer, not in the name.

**The S3 store layout** (D320; ``docs/OPERATE.md``, "Stores and the mirror"): every job (a build or a run)
has a **head** ``<utc>_<id>[_<comment>]`` (no dot; ``<utc>`` = the job's start). A build is the
directory ``builds/<head>.<plan>/`` holding ``build.json.zst``, ``build.log.zst`` and
``build-running.json``; a run's files are ``<stem>.json.zst``, ``<stem>.log.zst`` and
``<stem>-running.json`` with ``<stem>`` = ``<head>.<stage>``, under ``builds/<build>/runs/`` or,
outside a build, ``runs/``. Blobs are flat: ``blobs/<sha256>``. A job name splits at its first dot
(:func:`parse_job_name`). Records live only there (D344).
"""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass
from pathlib import Path

from stack.core.hashes import jcs

#: ``stack.schema_version``: the version of the header itself. ``"2"``: the header carries
#: ``stack.family_version`` (D295); ``"1"``: a legacy file without it, whose family version the
#: reader finds from its ``schema_digest`` (:func:`stack.store.parquet.file_format`). Another value
#: is refused like a newer family version.
STORE_SCHEMA_VERSION = "2"
LEGACY_SCHEMA_VERSION = "1"
SCHEMA_VERSIONS: tuple[str, ...] = (LEGACY_SCHEMA_VERSION, STORE_SCHEMA_VERSION)
#: The header value for "not applicable" (D15).
NONE = "none"
#: ``blocks_written`` on files of non-chain writers (collect, annotate, fold) (D15).
WILDCARD = "*"
#: Stages whose ``documents`` files write named blocks (``blocks_written`` is a JSON list).
CHAIN_STAGES: tuple[str, ...] = ("gather", "triage", "acquire")

#: Every ``stack.*`` header key (A.6), required on every file (D15; ``none`` where not
#: applicable): the base keys, then ``basis_kind`` (``<wf>``), the series keys (D372:
#: ``series`` (empty without one), ``comment`` (empty without one), ``kind`` (``full``/``part``),
#: ``base`` = the base file's name in its directory, empty for a full without a base; ``after`` =
#: the directory's last published file when the run pinned its inputs, empty for an empty
#: directory: for a part or an extending full ``after == base``), then the fold keys.
#: ``family_version`` (the family's format version, a decimal string) is absent from a legacy
#: ``schema_version = "1"`` file.
FOLD_HEADER_KEYS: tuple[str, ...] = ("fold_config", "filter", "annotations_folded", "release_config")
HEADER_KEYS: tuple[str, ...] = (
    "schema_version",
    "family",
    "key",
    "stage",
    "writer",
    "run_id",
    "file_id",
    "plan",
    "inputs",
    "prior",
    "only",
    "config_digest",
    "scope_complete",
    "row_count",
    "finished_at",
    "code_digest",
    "schema_digest",
    "family_version",
    "chain_id",
    "ids_digest",
    "blocks_written",
    "basis_kind",
    "series",
    "comment",
    "kind",
    "base",
    "after",
    *FOLD_HEADER_KEYS,
)
HEADER_PREFIX = "stack."

DATA, RUNS, BLOBS, RELEASES, CACHE = "data", "runs", "blobs", "releases", "cache"
BUILDS_DIR = "builds"
#: The local binding of a mirror directory to its S3 store (``{"uri": "s3://bucket/prefix"}``);
#: never uploaded (D321).
MIRROR_FILE = ".stack-mirror.json"
#: Suffixes of a job's files; no stage or plan name may end in one (D320).
RESERVED_SUFFIXES: tuple[str, ...] = (".json.zst", ".log.zst", "-running.json")

UTC_FORMAT = "%Y%m%dT%H%MZ"
#: A data file's stamp: the run's start to the second (D372).
DATA_UTC_FORMAT = "%Y%m%dT%H%M%SZ"
_UTC_RE = r"\d{8}T\d{4}Z"
_DATA_UTC_RE = r"\d{8}T\d{6}Z"
_RUN_RE = r"[0-9a-z]+"
#: A family directory: a schema family, optionally ``.<provider>`` (today's chain ids, D374).
FAMILY_DIR_RE = re.compile(r"[a-z0-9_]+(?:\.[a-z0-9_-]+)?")
#: A series and a data file's comment (D372; ``docs/OPERATE.md``, "Data files: series and chains").
SERIES_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
DATA_COMMENT_RE = re.compile(r"[a-z0-9-]+")
_DATA_NAME_RE = re.compile(
    rf"(?P<utc>{_DATA_UTC_RE})_(?P<run_id>{_RUN_RE})(?:_(?P<comment>[a-z0-9-]+))?\.(?P<kind>full|part)\.parquet")
#: ``--comment``: one grammar for the run head and the data file name (D372). Old heads
#: with ``[A-Za-z0-9_-]`` comments still parse (:func:`parse_head`).
_COMMENT_RE = DATA_COMMENT_RE
COMMENT_RE = _COMMENT_RE
#: A stage: ``<phase>`` or ``<phase>.<name>`` (``gather``, ``collect.arxiv_sweep``); also the plan
#: loader's check (:mod:`stack.plan`).
STAGE_RE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9][a-z0-9_-]*)?")
#: A plan name (``std-v3``, ``std-2026-10-13a``).
PLAN_RE = re.compile(r"[a-z0-9-]+")
_HEAD_RE = re.compile(rf"(?P<utc>{_UTC_RE})_(?P<id>{_RUN_RE})(?:_(?P<comment>[A-Za-z0-9_-]+))?")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def canonical_key_header(key: tuple[str, ...]) -> str:
    """The ``stack.key`` header value: the key columns as canonical JSON (``["doc_id"]``)."""
    return jcs(list(key))


def utc_stamp(at: dt.datetime) -> str:
    """``YYYYMMDDTHHMMZ`` for an aware datetime (a job head)."""
    if at.tzinfo is None:
        raise ValueError(f"naive datetime {at!r}")
    return at.astimezone(dt.UTC).strftime(UTC_FORMAT)


def data_stamp(at: dt.datetime) -> str:
    """``YYYYMMDDTHHMMSSZ`` for an aware datetime (a data file name)."""
    if at.tzinfo is None:
        raise ValueError(f"naive datetime {at!r}")
    return at.astimezone(dt.UTC).strftime(DATA_UTC_FORMAT)


def parse_data_stamp(stamp: str) -> dt.datetime:
    return dt.datetime.strptime(stamp, DATA_UTC_FORMAT).replace(tzinfo=dt.UTC)


def family_of_dir(family_dir: str) -> str:
    """The schema family of a family directory (``sources.af_graphql`` -> ``sources``)."""
    if not FAMILY_DIR_RE.fullmatch(family_dir):
        raise ValueError(f"family directory {family_dir!r} must match {FAMILY_DIR_RE.pattern}")
    return family_dir.split(".", 1)[0]


def check_series(series: str) -> str:
    if not SERIES_RE.fullmatch(series):
        raise ValueError(f"series {series!r} must match {SERIES_RE.pattern}")
    return series


def check_data_comment(comment: str) -> str:
    if not DATA_COMMENT_RE.fullmatch(comment):
        raise ValueError(f"comment {comment!r} must match {DATA_COMMENT_RE.pattern}")
    return comment


@dataclass(frozen=True)
class DataFile:
    """A data file's place and name (D372): ``data/<family_dir>[/<series>]/<name>``."""

    family_dir: str
    series: str | None
    utc: str
    run_id: str
    comment: str | None
    kind: str  # full | part

    @property
    def family(self) -> str:
        """The schema family."""
        return family_of_dir(self.family_dir)

    @property
    def name(self) -> str:
        return f"{self.utc}_{self.run_id}{'_' + self.comment if self.comment else ''}.{self.kind}.parquet"

    @property
    def dir(self) -> str:
        """The store-relative directory key, no trailing slash."""
        return series_dir(self.family_dir, self.series)

    @property
    def key(self) -> str:
        return f"{self.dir}/{self.name}"

    @property
    def file_id(self) -> str:
        return file_id(self.run_id, self.family)

    @property
    def is_part(self) -> bool:
        return self.kind == "part"


def series_dir(family_dir: str, series: str | None) -> str:
    """``data/<family_dir>/<series>`` (``data/<family_dir>`` without a series)."""
    family_of_dir(family_dir)
    return f"{DATA}/{family_dir}" + (f"/{check_series(series)}" if series is not None else "")


def make_data_file(family_dir: str, series: str | None, at: dt.datetime | str, run_id: str, kind: str, *,
                   comment: str | None = None) -> DataFile:
    """A :class:`DataFile`, every part checked; ``ValueError`` on a part without a series."""
    utc = at if isinstance(at, str) else data_stamp(at)
    if kind not in ("full", "part"):
        raise ValueError(f"kind {kind!r} is not full|part")
    if series is None and kind != "full":
        raise ValueError(f"data/{family_dir}/: a file without a series is always full")
    if comment is not None:
        check_data_comment(comment)
    out = DataFile(family_dir, series, utc, run_id, comment or None, kind)
    if parse_data_key(out.key) != out:
        raise ValueError(f"{out.key!r} does not round-trip")
    return out


def parse_data_name(name: str) -> tuple[str, str, str | None, str]:
    """``(utc, run_id, comment, kind)`` of a data file name; ``ValueError`` otherwise."""
    m = _DATA_NAME_RE.fullmatch(name)
    if m is None:
        raise ValueError(f"{name!r} is not <utc>_<run>[_<comment>].<full|part>.parquet")
    return m["utc"], m["run_id"], m["comment"], m["kind"]


def parse_data_key(key: str | Path) -> DataFile:
    """Parse ``data/<family_dir>[/<series>]/<name>`` (a key, or a path whose last parts are
    those); a staged ``.<name>.tmp`` reads as ``<name>``. ``ValueError`` if it does not follow D372."""
    parts = Path(key).parts
    if DATA not in parts:
        raise ValueError(f"{key}: not under {DATA}/")
    i = len(parts) - 1 - parts[::-1].index(DATA)
    rest = parts[i + 1:]
    if len(rest) not in (2, 3):
        raise ValueError(f"{key}: expected {DATA}/<family>[/<series>]/<name>")
    name = rest[-1]
    if name.startswith(".") and name.endswith(".tmp"):
        name = name[1:].removesuffix(".tmp")
    utc, run_id, comment, kind = parse_data_name(name)
    fam = rest[0]
    family_of_dir(fam)
    series = check_series(rest[1]) if len(rest) == 3 else None
    if series is None and kind != "full":
        raise ValueError(f"{key}: a file without a series is always full")
    return DataFile(fam, series, utc, run_id, comment, kind)


def parse_dir_ref(ref: str) -> tuple[str, str | None]:
    """``(family_dir, series)`` of a directory reference ``data/<family_dir>/<series>[/]``;
    ``data/<family_dir>/`` alone is refused (no implicit selection, D373)."""
    parts = [p for p in ref.strip().split("/") if p]
    if not parts or parts[0] != DATA:
        raise ValueError(f"{ref!r}: a data path is store-relative and starts with {DATA}/")
    if len(parts) == 2:
        family_of_dir(parts[1])
        raise ValueError(f"{ref!r} names a family directory without a series: no implicit selection; "
                         f"name a series directory {DATA}/{parts[1]}/<series>/ or an exact file")
    if len(parts) != 3:
        raise ValueError(f"{ref!r} is not {DATA}/<family>/<series>/")
    family_of_dir(parts[1])
    return parts[1], check_series(parts[2])


def tie_guard(at: dt.datetime, newest_input: str | None) -> str:
    """The data stamp of a run that started ``at``: its own second, or 1 s after the newest input
    file's stamp when it would not sort after it (D372)."""
    stamp = data_stamp(at)
    if newest_input is not None and stamp <= newest_input:
        stamp = data_stamp(parse_data_stamp(newest_input) + dt.timedelta(seconds=1))
    return stamp


def file_id(run_id: str, family: str) -> str:
    """``file_id = <run_id>.<family>`` (§2.2)."""
    return f"{run_id}.{family}"


def snapshot_id(run_id: str, seq: int) -> str:
    """``snapshot_id = <run_id>.<seq>`` (§2.2)."""
    if seq < 0:
        raise ValueError(f"negative snapshot seq {seq}")
    return f"{run_id}.{seq}"


# ---------------------------------------------------------------- job names (D320)
@dataclass(frozen=True)
class Head:
    """A job's head ``<utc>_<id>[_<comment>]``; ``str()`` is the text."""

    utc: str
    id: str
    comment: str | None = None

    def __str__(self) -> str:
        return f"{self.utc}_{self.id}" + (f"_{self.comment}" if self.comment else "")


@dataclass(frozen=True)
class JobName:
    """A run stem ``<head>.<stage>`` or a build directory name ``<head>.<plan>``."""

    head: Head
    tail: str  # the stage of a run, the plan of a build

    def __str__(self) -> str:
        return f"{self.head}.{self.tail}"


def check_tail(tail: str, *, what: str) -> str:
    """Refuse (``ValueError``) a stage or plan name that ends in a reserved suffix (D320)."""
    for suf in RESERVED_SUFFIXES:
        if tail.endswith(suf):
            raise ValueError(f"{what} {tail!r} ends in the reserved suffix {suf!r}")
    return tail


def make_head(at: dt.datetime | str, id: str, comment: str | None = None) -> str:
    """``<utc>_<id>[_<comment>]`` for a job that started ``at`` (an aware datetime or a
    ``YYYYMMDDTHHMMZ`` stamp); round-trips through :func:`parse_head`."""
    utc = at if isinstance(at, str) else utc_stamp(at)
    if comment is not None and not _COMMENT_RE.fullmatch(comment):
        raise ValueError(f"comment {comment!r} must match {_COMMENT_RE.pattern}")
    text = str(Head(utc, id, comment or None))
    if parse_head(text) != Head(utc, id, comment or None):
        raise ValueError(f"head {text!r} does not round-trip")
    return text


def parse_head(text: str) -> Head:
    """The parts of a head; ``ValueError`` if it does not follow D320."""
    m = _HEAD_RE.fullmatch(text)
    if m is None:
        raise ValueError(f"{text!r} is not a head <utc>_<id>[_<comment>]")
    return Head(m["utc"], m["id"], m["comment"])


def run_stem(head: str, stage: str) -> str:
    """``<head>.<stage>``: the stem of a run's json, log and marker."""
    parse_head(head)
    if not STAGE_RE.fullmatch(check_tail(stage, what="stage")):
        raise ValueError(f"stage {stage!r} is not <phase> or <phase>.<name>")
    return f"{head}.{stage}"


def build_name(head: str, plan: str) -> str:
    """``<head>.<plan>``: a build's directory name under ``builds/``."""
    parse_head(head)
    if not PLAN_RE.fullmatch(check_tail(plan, what="plan")):
        raise ValueError(f"plan {plan!r} must match {PLAN_RE.pattern}")
    return f"{head}.{plan}"


def parse_job_name(name: str) -> JobName:
    """Split a run stem or a build name at its first dot: the head, then the stage or plan."""
    head, dot, tail = name.partition(".")
    if not dot or not tail:
        raise ValueError(f"{name!r} is not <head>.<stage|plan>")
    check_tail(tail, what="stage or plan")
    if not (STAGE_RE.fullmatch(tail) or PLAN_RE.fullmatch(tail)):
        raise ValueError(f"{name!r}: {tail!r} is neither a stage nor a plan name")
    return JobName(parse_head(head), tail)


def split_job_file(name: str) -> tuple[str, str]:
    """``(<stem>, <suffix>)`` of a job file name (``….json.zst``, ``….log.zst``, ``…-running.json``),
    the stem checked by :func:`parse_job_name`; ``ValueError`` for any other name."""
    for suf in RESERVED_SUFFIXES:
        if name.endswith(suf):
            stem = name.removesuffix(suf)
            parse_job_name(stem)
            return stem, suf
    raise ValueError(f"{name!r} is not a job file (no suffix of {RESERVED_SUFFIXES})")


@dataclass(frozen=True)
class StoreLayout:
    """Paths under one store root. Creating the directories is :meth:`ensure`'s job only."""

    root: Path

    @property
    def data(self) -> Path:
        return self.root / DATA

    @property
    def runs(self) -> Path:
        return self.root / RUNS

    @property
    def builds_dir(self) -> Path:
        """``builds/``: one directory per build (D320)."""
        return self.root / BUILDS_DIR

    @property
    def mirror_file(self) -> Path:
        """``.stack-mirror.json`` (D321)."""
        return self.root / MIRROR_FILE

    @property
    def blobs(self) -> Path:
        return self.root / BLOBS

    @property
    def releases(self) -> Path:
        return self.root / RELEASES

    @property
    def cache(self) -> Path:
        return self.root / CACHE

    def ensure(self) -> StoreLayout:
        for d in (self.data, self.builds_dir, self.blobs, self.releases,
                  self.cache):
            d.mkdir(parents=True, exist_ok=True)
        return self

    def data_file(self, f: DataFile) -> Path:
        return self.root / f.key

    def staged_file(self, f: DataFile) -> Path:
        """``data/<family>[/<series>]/.<name>.tmp``: a run's output before its publish renames it."""
        return self.root / f.dir / f".{f.name}.tmp"

    # ---------------------------------------------------------------- the S3 store layout (D320)
    def build_dir(self, build: str) -> Path:
        """``builds/<head>.<plan>/``."""
        if not PLAN_RE.fullmatch(parse_job_name(build).tail):
            raise ValueError(f"build {build!r}: {parse_job_name(build).tail!r} is not a plan name")
        return self.builds_dir / build

    def build_json(self, build: str) -> Path:
        return self.build_dir(build) / "build.json.zst"

    def build_log(self, build: str) -> Path:
        return self.build_dir(build) / "build.log.zst"

    def build_marker(self, build: str) -> Path:
        return self.build_dir(build) / "build-running.json"

    def _run_base(self, stem: str, build: str | None) -> Path:
        if not STAGE_RE.fullmatch(parse_job_name(stem).tail):
            raise ValueError(f"run {stem!r}: {parse_job_name(stem).tail!r} is not a stage")
        return (self.runs if build is None else self.build_dir(build) / RUNS) / stem

    def run_json(self, stem: str, build: str | None = None) -> Path:
        """``builds/<build>/runs/<stem>.json.zst``, or ``runs/<stem>.json.zst`` outside a build."""
        b = self._run_base(stem, build)
        return b.with_name(b.name + ".json.zst")

    def run_log(self, stem: str, build: str | None = None) -> Path:
        b = self._run_base(stem, build)
        return b.with_name(b.name + ".log.zst")

    def run_marker(self, stem: str, build: str | None = None) -> Path:
        b = self._run_base(stem, build)
        return b.with_name(b.name + "-running.json")

    def flat_blob(self, digest: str) -> Path:
        """``blobs/<sha256>`` (64 lowercase hex, else ``ValueError``)."""
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError(f"blob digest {digest!r} is not 64 lowercase hex")
        return self.blobs / digest

    def key(self, path: str | Path) -> str:
        """The ``/``-separated key of ``path`` (absolute, or relative to the root) under the root;
        ``ValueError`` for a path outside it or the root itself."""
        root = Path(os.path.normpath(self.root.absolute()))
        p = Path(path)
        p = Path(os.path.normpath(p if p.is_absolute() else root / p))
        try:
            rel = p.relative_to(root)
        except ValueError:
            raise ValueError(f"{path} is outside the store root {self.root}") from None
        if not rel.parts:
            raise ValueError(f"{path} is the store root, not a key under it")
        return rel.as_posix()
