"""The dataset release (format = the dated release id): the public row format of The Safety Stack as a projection of a build's
corpus, written as a self-describing bundle (one variant per call).

A **dataset config** (``configs/releases/dataset-<variant>.yaml``, :class:`DatasetConfig`, ``format: dataset``) names
the variant (``public`` or ``internal``, the contentful one) and the shared files under ``configs/releases/dataset/``:
``mapping.yaml`` (the one description of the released schema: every public field with its internal path or the name
of its transform here, its description, enums; the variants' policies; capped, surface and prose paths; the status
remap), ``overrides-public.yaml`` (who decided each override, publicly), ``vocab.yaml`` (every value word with a
sentence; rendered as VOCABULARY.md) and the authored templates (``FORMAT.head.md``, ``README.card.md``, ``LICENSE``),
and the judge eval set (``judge_eval``, a repo directory such as ``evals/judge-2026-10-25j``) whose stored judge labels
the export scores against the release's own build for the card's quality table (:func:`judge_eval`, D728; also
``release.json`` ``quality.triage_vs_screen``, :mod:`stack.evals.release_quality`). A config key the model does not
know is refused; a key an earlier format had is refused with what replaced it (:data:`OBSOLETE_CONFIG_KEYS`). ``stack export --config dataset-public --out DIR`` (:func:`run_dataset_export`) writes it; the
old ``public-v1`` / ``internal-v1`` configs (:mod:`stack.fold.release`) are untouched.

**Inputs** (all local; nothing is fetched): the corpus of an ``ok`` build (:func:`stack.fold.release.release_gate`), the
build record, the run records of its annotate runs (``prompt_pin``: what each run's ``config_digest`` hashes, checked
by recomputing it), the build's ``<wf>`` family files (``override_ids`` and ``evidence_id`` per document: provenance)
and ``<producer>_evidence`` files (``model``: the annotator), the overrides chain the build applied (one head for every
run, else refused; every part of the chain up to that head, for the per-run export), the taxonomies and prompt files of
the repo (each checked against the run's pinned digest; the organisation vocabulary among them), for the quality section the ``documents`` file the build's fold.corpus run read, the series' ``triage_evidence`` head
and the judge eval set's files (D728) and, for the internal variant, the text blobs (the store's, or ``blobs_dir``;
every text's sha256 is checked).

**The row** (``mapping.yaml``): bibliographic scalars hoisted from ``metadata``, ``identity`` and ``content``
allow-listed, ``release`` (what this release did to the row) and ``annotations``: one struct with one typed column per
kind, each ``{metadata, payload}``, NULL when the kind was not run (each affiliation of the affiliation payload also carries
the stored ``affiliation_orgs`` record's answer for it, :data:`ORG_FIELDS`); ``metadata`` is one struct type for every kind
(:data:`META_FIELDS`). ``config_id`` = ``<producer kind>:<prompt>:<model>:<8 hex of config_digest>`` (``rule`` in the
model slot for a computed payload), or ``override:<id>[+<id>]`` when overrides supplied the whole payload (D717: the
corpus then carries the overrides' :func:`stack.overrides.rows_digest` as its config digest, checked here; a corpus
folded before D717 is refused for such a record). ``provenance``: ``pipeline`` (no override), ``override`` (a used
``row`` override at document grain; no evidence left at item grain), ``mixed`` (field edits). ``annotator``: the
evidence row's ``model`` (a fallback model shows), ``matcher`` for model_name_matches, ``rule`` for a computed
payload, the model of an ``ai:<model>`` override, ``human`` for a human-tier one. The status remap
(``mapping.yaml`` ``status_remap``) is applied; FORMAT.md states its rules and how many values it changed, and
``release.json`` counts them by rule.

**Variants**: one arrow schema (built once from the mapping); ``public`` NULLs every abstract and cuts the quote and
surface strings longer than ``quote_cap_chars`` at a word boundary with ``…`` (:func:`cut_string`), listing each cut
in ``release.cuts``; ``internal`` keeps abstracts and full quotes and the converted text inline in ``content.text.body``
(row groups close at :data:`ROW_GROUP_TEXT_BYTES` of text). Model prose is
never capped; ``prose_scrub`` applies in both, and so does :func:`scrub_url` to the URL fields (:data:`URL_FIELDS`,
D787: the fetch-only query parameters, a contact address or a URL signature, are dropped; the rest of the URL is kept
byte for byte).

**Bundle** (D725; exactly the paths of the mapping's ``files:``, taxonomies one per pinned vocabulary):
``data/corpus.parquet`` (zstd, 1024-row groups, a ``description`` on every field; the card's only config),
``config/configs.json`` (one entry per ``config_id``), ``config/overrides.jsonl`` (the active overrides:
:func:`active_overrides`), ``taxonomies/<name>-<version>.yaml`` (the organisation vocabulary among them; one file per
name and version a shipped config or one of the build's own annotate runs pinned, so rows accepted from an earlier
config ship their version beside the current one, D758; ``release.json`` ``taxonomies`` lists per name the versions,
oldest first; two files of one name and version with different bytes are refused),
``schema.json`` (the row's JSON Schema, every kind's record and payload inline), ``VOCABULARY.md``, ``FORMAT.md``
and ``README.md`` (generated tables in authored templates; the card carries the quality table), ``LICENSE`` and
``release.json`` (the manifest: every other file with bytes and sha256, rows for tables; ``stages``: per annotate
stage of the build, what it ships, D758: its selection digest, its target config and its accepted earlier configs
as the stage's run recorded them from the plan line, each with ``config_id`` (the one most of its documents carry), ``config_ids`` and the shipped documents under it;
FORMAT.md one sentence per accepted config). :func:`check_bundle` runs
the mapping's checks over the written stage before it is renamed into place; any problem keeps nothing.
"""

from __future__ import annotations

import copy
import datetime as dt
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.parse
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, model_validator

from stack import duck, orgvocab, yamlio
from stack.checks.digests import H
from stack.fold import FoldError, chained_basis, corpus_input, data_file, filter_rows
from stack.fold.release import RELEASE_CONFIGS, REPO_ROOT, build_status, check_out_dir, release_gate
from stack.overrides import Override, drop_revoked, rows_digest, winners
from stack.query import resolve_filter
from stack.schemas.blocks import DOCUMENT_FIELDS
from stack.schemas.payloads.registry import REGISTRY
from stack.store.index import store_index
from stack.store.parquet import read_family_fields, read_header

DATASET_DIR = RELEASE_CONFIGS / "dataset"
ROW_GROUP_ROWS = 1024
#: A row group of the corpus also closes once its inline texts (``content.text.body``) reach this many UTF-8 bytes,
#: so a contentful row group stays small enough for readers that load a group at a time (the HF viewer pages by row
#: group): ~16 MiB of text is ~260 rows of the median document, or 16 one-megabyte documents.
ROW_GROUP_TEXT_BYTES = 16 << 20
BATCH_ROWS = 4096
ELLIPSIS = "…"
CONTACT = {"email": "info@thesafetystack.ai", "takedown": "mailto:info@thesafetystack.ai",
           "public_reports": "https://github.com/thesafetystack/the-safety-stack/issues/new/choose"}
LICENCE = {"ours": "CC0-1.0: what we made (the annotations, the identity and provenance structure, the release "
                   "metadata, the taxonomies, prompts and documentation)",
           "not_covered": "bibliographic records (titles, authors, dates, identifiers, abstracts) as their providers "
                          "supply them, under those providers' terms; quoted and full document text, its owners'; "
                          "see LICENSE"}
#: The internal workflow of a public kind whose payload is a record of another call: the producer's public kind.
PRODUCER_KIND = {"screen": "screen", "doc_class": "screen", "stack_relevance": "screen",
                 "shallow_review_relevance": "screen", "text_usability": "screen", "affiliation": "affiliation",
                 "safety_techniques": "safety_techniques", "model_roles": "model_roles",
                 "model_name_matches": "model_roles", "affiliation_orgs": "affiliation_orgs", "translation": "translation"}
#: Taxonomy files of the repo that a prompt pin may name by sha256 (checked against the run's template digests): the
#: catalogue name and the default path (:func:`taxonomy_source`).
TAXONOMY_FILES = {"technique_staged": ("techniques", "taxonomy/techniques-1.1.0.yaml"),
                  "model_roles": ("models", "configs/models.yaml"),
                  "affiliation_orgs": ("orgs", "taxonomy/orgs-0.2.2.yaml")}
#: The plan pin key that names another file than :data:`TAXONOMY_FILES`' default.
TAXONOMY_PIN_KEYS = {"technique_staged": "taxonomy", "affiliation_orgs": "vocab"}
#: The vendored taxonomy files (pipeline path → tag, data-repository source, sha256).
VENDORED_LOCK = REPO_ROOT / "taxonomy" / "vendored.lock.yaml"
#: The fields the release adds to each ``items[].affiliations[]`` of the kind mapping ``org_fields.kind`` names
#: (affiliation), copied from the document's stored record of ``org_fields.from`` (``annotate.affiliation_orgs``,
#: joined by ``item_key`` and position): ``org_ids`` (ids of the bundled ``taxonomies/orgs-<version>.yaml``; [] when
#: none), ``org_status`` (:data:`stack.orgvocab.STATUSES`) and ``org_candidates`` (D737: the record's ``ambiguous``,
#: one id list per name the context did not decide; [] when none); all NULL where the document has no such record. That
#: record is also its own kind column (its metadata is their provenance). :func:`with_org_fields` (type),
#: :func:`with_orgs` (values).
ORG_FIELDS = ("org_ids", "org_status", "org_candidates")
#: Computed (model-free) workflows: how ``config/configs.json`` ``components.method`` describes them.
COMPUTED_METHOD = {"affiliation": "parsed from the publisher's structured XML byline; no model call",
                   "affiliation_orgs": "deterministic organisation matcher (stack/orgvocab.py) over the affiliation "
                                       "payload's surfaces, with the pinned organisation vocabulary; no model call"}
#: Strings no shipped file of ours may contain (document-derived strings in the parquet files are reported by the
#: candidate checks, not refused here): local paths and internal locations everywhere; storage hosts everywhere but
#: in the taxonomy files, which are the pinned catalogue files byte for byte and list public model-weight URLs.
PATH_LEAKS = (r"(?:^|[\s'\"(=,\[])/(?:home|tmp)/", r"\bmi[m]ir\b", r"\bimp[l]/\d+", r"\.local/", r"\.stack-mirror",
              r"\bcoord/", r"\bexplorations/")
#: Exact phrases removed before the path-leak patterns are matched: names of real organisations in the organisation
#: vocabulary that a pattern would flag (the name and its id, as the bundled taxonomy file spells them; built like the
#: patterns, so this file carries no such literal). Only these phrases: the bare word still trips the check.
_MC = "Mi" + "mir Center for Long Term Futures Research"
LEAK_ALLOWED_PHRASES = (_MC, "-".join(_MC.lower().split()))
STORE_LEAKS = (r"\b(?:s3|gs)://", r"amazonaws\.com", r"r2\.cloudflarestorage", r"storage\.googleapis\.com")
#: Links to link-shared documents and chats (D796): a document, sheet, form, file or folder id on Google Docs or
#: Drive, a Claude, ChatGPT or Gemini share, chat or artifact link, a Notion page, a Dropbox share. None of our files
#: (the release's own files; every tracked text file of the repository, ``tests/test_public_tree.py``) may carry one;
#: a host name alone (the URL rules of :mod:`stack.urls`) is not a link.
SHARED_LINKS = (r"docs\.google\.com/(?:document|spreadsheets|presentation|forms|drawings|file)/(?:u/\d+/)?d/(?:e/)?"
                r"[A-Za-z0-9_-]{10,}",
                r"drive\.google\.com/(?:file/d/|open\?id=|drive/(?:u/\d+/)?folders/|uc\?(?:[^\s\"']*&)?id=)[A-Za-z0-9_-]{10,}",
                r"claude\.ai/(?:artifacts?|share|public|chat|project)s?/[A-Za-z0-9_-]{8,}",
                r"(?:chatgpt\.com|chat\.openai\.com)/(?:share|c|g)/[A-Za-z0-9_-]{8,}",
                r"(?:gemini\.google\.com|g\.co/gemini)/share/[A-Za-z0-9_-]{6,}", r"\bnotion\.(?:so|site)/[A-Za-z0-9_-]{6,}",
                r"[A-Za-z0-9-]+\.notion\.site\b", r"dropbox\.com/(?:s|scl|sh)/[A-Za-z0-9_-]{6,}",
                r"dl\.dropboxusercontent\.com/")
#: The release row's URL fields (public paths) :func:`scrub_url` cleans in both variants (D787), and those
#: :func:`check_bundle` also checks: a member's ``key_value`` where its ``key_kind`` is ``url``, which stays as stored
#: (an identity key, joined to ``config/overrides.jsonl`` ``key``), so a release whose URL key carries such a
#: parameter is refused instead.
URL_FIELDS = ("canonical_url", "content.source.url", "identity.members[].canonical_url")
URL_CHECKED = (*URL_FIELDS, "identity.members[].key_value")
#: Query parameter names :func:`scrub_url` drops (compared lower-cased, percent-decoded). Contact parameters, always:
#: a fetch's polite-pool address (Europe PMC, NCBI E-utilities, Unpaywall ``email``; Crossref, OpenAlex ``mailto``).
URL_CONTACT_PARAMS = frozenset({"email", "mailto"})
#: Signed-URL families, each dropped only in a query that carries its marker: AWS SigV4 and GCS V4 query
#: authentication (every ``x-amz-*`` / ``x-goog-*`` parameter, beside that prefix's ``signature`` or ``credential``);
#: CloudFront and the legacy S3 / GCS form (beside ``signature``); Azure SAS (beside both ``sig`` and ``sv``). Other
#: parameters of a signed URL (``response-content-disposition``, …) are kept.
URL_SIGNED_PREFIXES = ("x-amz-", "x-goog-")
URL_SIGNATURE_PARAMS = frozenset({"signature", "expires", "key-pair-id", "policy", "awsaccesskeyid", "googleaccessid"})
URL_SAS_PARAMS = frozenset({"sig", "sv", "se", "st", "sp", "sr", "spr", "srt", "ss", "si", "sip", "sdd", "skoid",
                            "sktid", "skt", "ske", "sks", "skv"})
#: The bundle's fixed paths (D725); the taxonomies are ``taxonomies/<name>-<version>.yaml``.
CONFIGS_FILE = "config/configs.json"
OVERRIDES_FILE = "config/overrides.jsonl"
ROW_SCHEMA_FILE = "schema.json"
VOCABULARY_FILE = "VOCABULARY.md"


class DatasetError(FoldError):
    """A dataset release refused (nothing written, or nothing kept)."""


# ---------------------------------------------------------------- config
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetConfig(_Model):
    """A dataset release config (module docstring)."""

    name: str
    format: Literal["dataset"]
    variant: Literal["public", "internal"]
    hf_repo: str
    mapping: str = "dataset/mapping.yaml"
    overrides_public: str = "dataset/overrides-public.yaml"
    vocab: str = "dataset/vocab.yaml"
    templates: str = "dataset"
    #: The judge eval set (a directory of the repo, e.g. ``evals/judge-2026-10-25j``) whose stored judge labels the
    #: export scores against the release's build for the card's quality table (:func:`judge_eval`, D728).
    judge_eval: str
    internal_variant_note: str | None = None  # one line in the card and FORMAT; None drops it

    @model_validator(mode="before")
    @classmethod
    def _no_obsolete_keys(cls, raw: Any) -> Any:
        if isinstance(raw, Mapping):
            old = [k for k in raw if k in OBSOLETE_CONFIG_KEYS]
            if old:
                raise ValueError("; ".join(f"config key {k!r} is obsolete: {OBSOLETE_CONFIG_KEYS[k]}" for k in old))
        return raw

    def path(self, rel: str) -> Path:
        return RELEASE_CONFIGS / rel


#: Dataset config keys of earlier formats, refused with what replaced them (an unknown key is refused too, by
#: ``extra="forbid"``).
OBSOLETE_CONFIG_KEYS = {
    "views": "the flat views are no longer written (D723); FORMAT.md's recipes build flat tables from the corpus",
    "prompts": "prompts/ no longer ships (D725); config/configs.json names each prompt and its digests",
    "examples": "examples/rows.json no longer ships (D725)",
    "corrections": "export-time corrections are gone (D768): a wrong stored title or doc_type is a `fields` override "
                   "in the store (configs/overrides/data_quality_2026_10.yaml)",
}


def is_dataset_config(ref: str | Path) -> bool:
    """Whether release config ``ref`` (a name or path) is a dataset config (``format: dataset``)."""
    from stack.fold.release import config_path

    p = config_path(ref)
    if not p.is_file():
        return False
    raw = yamlio.load(p.read_text("utf-8"))
    return isinstance(raw, dict) and raw.get("format") == "dataset"


def load_dataset_config(ref: str | Path) -> DatasetConfig:
    from stack.fold.release import config_path

    p = config_path(ref)
    try:
        cfg = DatasetConfig.model_validate(yamlio.load(p.read_text("utf-8")))
    except (OSError, ValueError) as exc:
        raise DatasetError(f"dataset config {ref}: {exc}") from exc
    if cfg.name != p.stem:
        raise DatasetError(f"dataset config {p}: name {cfg.name!r} is not the file stem {p.stem!r}")
    return cfg


# ---------------------------------------------------------------- types
_SCALARS: dict[str, pa.DataType] = {"string": pa.string(), "int32": pa.int32(), "int64": pa.int64(),
                                    "bool": pa.bool_(), "float64": pa.float64(), "date32": pa.date32(),
                                    "timestamp[us, UTC]": pa.timestamp("us", tz="UTC")}


def parse_type(s: str) -> pa.DataType:
    """A mapping ``type``: a scalar name, ``list<T>`` or ``struct<name T, ...>`` (struct fields nullable)."""
    s = s.strip()
    if s in _SCALARS:
        return _SCALARS[s]
    if s.startswith("list<") and s.endswith(">"):
        return pa.list_(parse_type(s[5:-1]))
    if s.startswith("struct<") and s.endswith(">"):
        parts, depth, cur = [], 0, ""
        for ch in s[7:-1]:
            depth += ch == "<"
            depth -= ch == ">"
            if ch == "," and depth == 0:
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        parts.append(cur)
        return pa.struct([pa.field(p.strip().split(" ", 1)[0], parse_type(p.strip().split(" ", 1)[1])) for p in parts])
    raise DatasetError(f"mapping type {s!r}: not a known type")


def _internal_field(path: str) -> pa.Field:
    """The documents-schema field at dotted ``path`` (``[]`` = a list level; ``a.b[]`` names the list ``b``)."""
    parts = path.replace("[]", "").split(".")
    fields = {f.name: f for f in DOCUMENT_FIELDS}
    f: pa.Field | None = fields.get(parts[0])
    for p in parts[1:]:
        if f is None:
            break
        t = f.type
        if pa.types.is_list(t):
            t = t.value_type
        f = t.field(p) if pa.types.is_struct(t) and t.get_field_index(p) >= 0 else None
    if f is None:
        raise DatasetError(f"mapping: internal path {path!r} is not in the documents schema")
    return f


def _elem(t: pa.DataType) -> pa.DataType:
    return t.value_type if pa.types.is_list(t) else t


def _desc(doc: str | None) -> dict[bytes, bytes] | None:
    return {b"description": doc.encode("utf-8")} if doc else None


# ---------------------------------------------------------------- the mapping
@dataclass
class Node:
    """One public field: ``kind`` ``leaf`` (internal), ``transform``, ``struct`` (internal), ``list`` (internal list of
    struct), ``group`` (struct of children evaluated in the parent's context) or ``annotations``."""

    path: str
    name: str
    kind: str
    entry: Mapping[str, Any]
    rel: str | None = None  # internal path relative to the nearest internal ancestor's object
    children: list[Node] = field(default_factory=list)
    ftype: pa.DataType | None = None
    nullable: bool = True


def _segments(path: str) -> list[str]:
    return path.split(".")


def _parent(path: str) -> str:
    return path.rsplit(".", 1)[0] if "." in path else ""


@dataclass
class KindSpec:
    """One annotation kind: public name, internal workflow, the projected payload type and its name map."""

    kind: str
    workflow: str
    entry: Mapping[str, Any]
    ptype: pa.StructType
    names: dict[str, str]  # public dotted path within the payload -> internal field name at that level
    version: int


class Mapping_:
    """``mapping.yaml`` loaded and checked: the node tree, the kinds, the arrow schema (module docstring)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.raw: dict[str, Any] = yamlio.load(path.read_text("utf-8"))
        r = self.raw
        self.variants: dict[str, dict[str, Any]] = r["variants"]
        self.quote_paths: list[str] = list(r["quote_paths"])
        self.surface_paths: list[str] = list(r["surface_paths"])
        self.prose_paths: list[str] = list(r.get("prose_paths") or [])
        #: Payload fields derived from the abstract (D755: ``annotations.translation.payload.abstract_en``): NULL in a
        #: variant that withholds abstracts (:func:`withhold_abstract_paths`); plain dotted paths only.
        self.abstract_paths: list[str] = list(r.get("abstract_paths") or [])
        for ap in self.abstract_paths:
            if "[]" in _ann_path(ap)[1]:
                raise DatasetError(f"mapping: abstract_paths {ap!r}: a list path is not supported")
        self.prose_scrub: list[dict[str, Any]] = list(r.get("prose_scrub") or [])
        self.dropped: set[str] = set(r.get("dropped_workflows") or [])
        self.json_kinds: dict[str, Any] = dict(r.get("json_kinds") or {})
        self.remap_rules: list[dict[str, Any]] = list(r.get("status_remap") or [])
        #: ``org_fields: {kind, from}``: the kind whose affiliations gain :data:`ORG_FIELDS` and the kind whose stored
        #: records supply them; None = no organisation fields.
        of = r.get("org_fields")
        if of is not None and (not isinstance(of, dict) or set(of) != {"kind", "from"}):
            raise DatasetError(f"mapping: org_fields must be {{kind, from}}, got {of!r}")
        self.org_kind: str | None = of["kind"] if of else None
        self.org_from: str | None = of["from"] if of else None
        self.entries: dict[str, dict[str, Any]] = {}
        for e in r["fields"]:
            p = e["public"]
            if p in self.entries:
                raise DatasetError(f"mapping: {p} listed twice")
            self.entries[p] = e
        self.meta_entries: list[dict[str, Any]] = list(r["meta"])
        self.kinds: dict[str, KindSpec] = {}
        self._build_kinds()
        self.meta_type = self._meta_type()
        self.root = self._build_tree()
        self.schema = pa.schema([self._field(n) for n in self.root.children])
        self.enums = self._enums()
        self.transforms = sorted(e["transform"] for e in self.entries.values() if "transform" in e)

    # -- kinds
    def _build_kinds(self) -> None:
        for p, e in self.entries.items():
            if not p.startswith("annotations.") or p.count(".") != 1 or p.endswith("[]"):
                continue
            kind = p.split(".")[1]
            m = re.fullmatch(r"annotations\.(\w+)\.payload", e["internal"])
            if m is None or m.group(1) not in REGISTRY:
                raise DatasetError(f"mapping {p}: internal {e['internal']!r} is not annotations.<registered wf>.payload")
            wf = m.group(1)
            spec = REGISTRY[wf]
            renames = {k: v for k, v in (e.get("renames") or {}).items()}
            internal_t = spec.payload_schema
            if kind == self.org_kind:
                if wf != "affiliation":
                    raise DatasetError(f"mapping: org_fields kind {kind!r} is {wf}; the organisation fields are "
                                       "defined for the affiliation payload only")
                internal_t = with_org_fields(internal_t)
            ptype, names = _keep_type(internal_t, list(e["keep"]), renames, e.get("docs") or {}, p)
            self.kinds[kind] = KindSpec(kind, wf, e, ptype, names, spec.version)
        if self.org_kind is not None and (self.org_kind not in self.kinds or self.org_from not in self.kinds
                                          or self.kinds[self.org_from].workflow != "affiliation_orgs"):
            raise DatasetError(f"mapping: org_fields {{kind: {self.org_kind}, from: {self.org_from}}} needs the kind "
                               "columns of affiliation and of affiliation_orgs")
        known = {w for k in self.kinds.values() for w in (k.workflow,)}
        missing = sorted(set(REGISTRY) - known - self.dropped)
        if missing:
            raise DatasetError(f"mapping: registry workflows {missing} are neither mapped nor in dropped_workflows")
        meta_kinds = next(m for m in self.meta_entries if m["name"] == "kind")["enum"]["values"]
        if list(self.kinds) != list(meta_kinds):
            raise DatasetError(f"mapping: meta.kind enum {meta_kinds} differs from the kind columns {list(self.kinds)}")

    def _meta_type(self) -> pa.StructType:
        ann = {f.name: f for f in next(f for f in DOCUMENT_FIELDS if f.name == "annotations").type}
        common = {f.name: f for f in ann["screen"].type}
        out = []
        for m in self.meta_entries:
            if "internal" in m:
                f = common[m["internal"]]
                out.append(pa.field(m["name"], f.type, f.nullable, metadata=_desc(m.get("doc"))))
            else:
                out.append(pa.field(m["name"], parse_type(m["type"]), bool(m["nullable"]), metadata=_desc(m.get("doc"))))
        return pa.struct(out)

    # -- the node tree
    def _build_tree(self) -> Node:
        root = Node("", "", "group", {})
        nodes: dict[str, Node] = {"": root}
        internal_of: dict[str, str] = {"": ""}
        for p, e in self.entries.items():
            if p.startswith("annotations."):
                continue  # the kinds and other[] are built from self.kinds (annotations_type)
            par = _parent(p)
            if par not in nodes:
                raise DatasetError(f"mapping: {p} is listed before its parent {par}")
            parent = nodes[par]
            name = _segments(p)[-1].removesuffix("[]")
            is_list = p.endswith("[]")
            if p == "annotations":
                n = Node(p, name, "annotations", e)
            elif parent.kind == "transform":
                continue  # doc-only children of a transform struct (release.cuts[].path)
            elif "transform" in e:
                n = Node(p, name, "transform", e, ftype=parse_type(e["type"]), nullable=bool(e["nullable"]))
            elif "internal" in e:
                base = internal_of[par]
                ip = e["internal"]
                if base and not ip.startswith(base + "."):
                    raise DatasetError(f"mapping {p}: internal {ip!r} is not under its parent's internal {base!r}")
                rel = ip[len(base) + 1:] if base else ip
                f = _internal_field(ip)
                kind = "list" if is_list else ("struct" if pa.types.is_struct(f.type) else "leaf")
                n = Node(p, name, kind, e, rel=rel.removesuffix("[]"), ftype=f.type, nullable=f.nullable)
                internal_of[p] = ip.removesuffix("[]") if not is_list else ip
            else:
                n = Node(p, name, "group", e, nullable=False)
                internal_of[p] = internal_of[par]
            if is_list and n.kind not in ("list", "transform"):
                raise DatasetError(f"mapping {p}: a list entry needs an internal list path or a transform")
            parent.children.append(n)
            nodes[p] = n
        for n in nodes.values():
            if n.kind in ("struct", "list") and not n.children and n.path != "":
                n.kind = "leaf"  # a struct kept whole
        return root

    def _field(self, n: Node) -> pa.Field:
        doc = n.entry.get("doc")
        if n.kind == "leaf":
            return pa.field(n.name, n.ftype, n.nullable, metadata=_desc(doc))
        if n.kind == "transform":
            t = n.ftype
            assert t is not None
            kids = {c["public"].rsplit(".", 1)[-1]: c for p, c in self.entries.items() if _parent(p) == n.path}
            if kids and pa.types.is_list(t) and pa.types.is_struct(t.value_type):
                st = pa.struct([pa.field(f.name, f.type, f.nullable, metadata=_desc(kids.get(f.name, {}).get("doc")))
                                for f in t.value_type])
                t = pa.list_(st)
            return pa.field(n.name, t, n.nullable, metadata=_desc(doc))
        if n.kind == "annotations":
            return pa.field(n.name, self.annotations_type(), False, metadata=_desc(doc))
        st = pa.struct([self._field(c) for c in n.children])
        if n.kind == "list":
            return pa.field(n.name, pa.list_(pa.field("element", st, _elem_nullable(n.ftype))), n.nullable,
                            metadata=_desc(doc))
        return pa.field(n.name, st, n.nullable, metadata=_desc(doc))

    def annotations_type(self) -> pa.StructType:
        cols = []
        for k in self.kinds.values():
            st = pa.struct([pa.field("metadata", self.meta_type, False,
                                     metadata=_desc("The record's metadata (the same struct type for every kind).")),
                            pa.field("payload", k.ptype, True, metadata=_desc(
                                "The payload; on a failed record its lists are NULL."))])
            cols.append(pa.field(k.kind, st, True, metadata=_desc(k.entry.get("doc"))))
        o = self.entries["annotations.other[]"]
        other = pa.struct([pa.field("metadata", self.meta_type, False),
                           pa.field("payload", pa.string(), False,
                                    metadata=_desc(self.entries["annotations.other[].payload"].get("doc"))),
                           pa.field("schema", pa.string(), False,
                                    metadata=_desc(self.entries["annotations.other[].schema"].get("doc")))])
        cols.append(pa.field("other", pa.list_(other), False, metadata=_desc(o.get("doc"))))
        return pa.struct(cols)

    # -- enums: public path -> (values, open)
    def _enums(self) -> dict[str, tuple[list[str], bool]]:
        out: dict[str, tuple[list[str], bool]] = {}

        def put(path: str, spec: Any) -> None:
            if isinstance(spec, dict):
                out[path] = ([str(v) for v in spec["values"]], bool(spec.get("open")))
            else:
                out[path] = ([str(v) for v in spec], False)

        for p, e in self.entries.items():
            if "enum" in e:
                put(p, e["enum"])
        for m in self.meta_entries:
            if "enum" in m:
                put(f"annotations.*.metadata.{m['name']}", m["enum"])
        for k in self.kinds.values():
            for leaf, spec in (k.entry.get("enums") or {}).items():
                put(f"annotations.{k.kind}.payload.{leaf}", spec)
        return out

    def capped_paths(self) -> list[str]:
        return self.quote_paths + self.surface_paths


def _elem_nullable(t: pa.DataType | None) -> bool:
    return t.value_field.nullable if t is not None and pa.types.is_list(t) else True


def _keep_tree(keeps: Sequence[str]) -> dict[str, Any]:
    """``["a[].b", "a[].c", "d"]`` -> ``{"a": {"b": None, "c": None}, "d": None}`` (None = kept whole)."""
    tree: dict[str, Any] = {}
    for k in keeps:
        node = tree
        parts = [p.removesuffix("[]") for p in k.split(".")]
        for part in parts[:-1]:
            if node.get(part) is None:
                node[part] = {}
            node = node[part]
        node.setdefault(parts[-1], None)
    return tree


def _keep_type(t: pa.StructType, keeps: Sequence[str], renames: Mapping[str, str], docs: Mapping[str, str],
               where: str) -> tuple[pa.StructType, dict[str, str]]:
    """The payload struct ``t`` restricted to ``keeps`` (public leaf paths, ``a[].b``; a kept struct is kept whole),
    ``renames`` (public path -> internal path, same level) applied, ``docs[path]`` as descriptions. Returns the type
    and the name map (public path, ``[]`` on list levels -> internal field name). Refuses a keep that names nothing
    and an explicitly kept leaf without a doc."""
    for pub, internal in renames.items():
        if _parent(pub) != _parent(internal):
            raise DatasetError(f"mapping {where}: rename {pub} <- {internal} moves a field between levels")
    names: dict[str, str] = {}

    def sub(st: pa.StructType, tree: dict[str, Any] | None, prefix: str) -> pa.StructType:
        here = {pub.rsplit(".", 1)[-1]: internal.rsplit(".", 1)[-1] for pub, internal in renames.items()
                if _parent(pub) == prefix}
        back = {v: k for k, v in here.items()}
        items = list(tree.items()) if tree is not None else [(back.get(f.name, f.name), None) for f in st]
        out = []
        for pub, subtree in items:
            iname = here.get(pub, pub)
            path = f"{prefix}.{pub}" if prefix else pub
            idx = st.get_field_index(iname)
            if idx < 0:
                raise DatasetError(f"mapping {where}: keep {path!r} names no field of the internal payload")
            f = st.field(idx)
            names[path] = iname
            doc = (docs.get(path) or docs.get(path + "[]")) if tree is not None else None
            if tree is not None and subtree is None and not doc:
                raise DatasetError(f"mapping {where}: kept leaf {path!r} has no docs entry")
            ft = f.type
            if pa.types.is_list(ft) and pa.types.is_struct(ft.value_type):
                ft = pa.list_(pa.field(ft.value_field.name, sub(ft.value_type, subtree, path + "[]"),
                                       ft.value_field.nullable))
            elif pa.types.is_struct(ft):
                ft = sub(ft, subtree, path)
            elif subtree:
                raise DatasetError(f"mapping {where}: keep {path!r} goes below a scalar")
            out.append(pa.field(pub, ft, f.nullable, metadata=_desc(doc)))
        return pa.struct(out)

    return sub(t, _keep_tree(keeps), ""), names


def with_org_fields(t: pa.StructType) -> pa.StructType:
    """The affiliation payload type ``t`` with the release's organisation fields (:data:`ORG_FIELDS`) on each
    ``items[].affiliations[]`` element, nullable (NULL where the document has no ``affiliation_orgs`` record)."""
    def relist(f: pa.Field, elem: pa.DataType) -> pa.Field:
        return f.with_type(pa.list_(pa.field(f.type.value_field.name, elem, f.type.value_field.nullable)))

    items = t.field("items")
    item_t = items.type.value_type
    affs = item_t.field("affiliations")
    aff_t = pa.struct(list(affs.type.value_type) + [pa.field("org_ids", pa.list_(pa.string()), True),
                                                     pa.field("org_status", pa.string(), True),
                                                     pa.field("org_candidates", pa.list_(pa.list_(pa.string())), True)])
    item_t = pa.struct([relist(f, aff_t) if f.name == "affiliations" else f for f in item_t])
    return pa.struct([relist(f, item_t) if f.name == "items" else f for f in t])


def with_orgs(payload: Mapping[str, Any] | None, orgs: Mapping[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    """An internal affiliation ``payload`` with :data:`ORG_FIELDS` on each affiliation, copied from ``orgs`` (the
    payload of the document's ``affiliation_orgs`` record: items by ``item_key``, affiliations by position; None =
    no record, the fields stay NULL). Returns the payload and a problem (the record does not line up), or None."""
    if payload is None:
        return None, None
    items = payload.get("items")
    if items is None:
        return dict(payload), None
    by_key = {it.get("item_key"): it.get("affiliations") or [] for it in (orgs or {}).get("items") or []}
    problem = None
    out_items = []
    for it in items:
        affs = it.get("affiliations")
        if affs is not None:
            got = by_key.get(it.get("item_key")) if orgs is not None else None
            if orgs is not None and (got is None or len(got) != len(affs)):
                problem = (f"affiliation_orgs item {it.get('item_key')!r}: {len(got) if got is not None else 'no'} "
                           f"answers for {len(affs)} affiliations")
                got = None
            affs = [{**a, "org_ids": list(o["org_ids"]) if o else None, "org_status": o["status"] if o else None,
                     "org_candidates": [list(c) for c in o.get("ambiguous") or []] if o else None}
                    for a, o in zip(affs, got or [None] * len(affs), strict=True)]
        out_items.append({**it, "affiliations": affs})
    return {**payload, "items": out_items}, problem


def project_payload(value: Any, t: pa.DataType, names: Mapping[str, str], path: str = "") -> Any:
    """``value`` (an internal payload or part of one) projected to public type ``t`` via ``names``."""
    if value is None:
        return None
    if pa.types.is_struct(t):
        out = {}
        for f in t:
            p = f"{path}.{f.name}" if path else f.name
            out[f.name] = project_payload(value.get(names.get(p, f.name)), f.type, names, p)
        return out
    if pa.types.is_list(t):
        return [project_payload(v, t.value_type, names, path + "[]") for v in value]
    return value


# ---------------------------------------------------------------- cutting, scrubbing, walking capped paths
def cut_string(s: str, cap: int) -> str:
    """``s`` cut to at most ``cap`` code points (mapping ``quote_cut``): unchanged when it fits; else the longest
    prefix of at most ``cap - 1`` code points that ends at a word boundary (whitespace stripped), plus ``…``; with no
    boundary in reach, the first ``cap - 1`` code points plus ``…``."""
    if len(s) <= cap:
        return s
    limit = cap - 1
    head = s[:limit]
    if not s[limit].isspace():
        m = re.search(r"\s\S*$", head)
        if m is not None and m.start() > 0:
            head = head[:m.start()]
    head = head.rstrip()
    if not head:
        head = s[:limit]
    return head + ELLIPSIS


def walk_strings(obj: Any, path: str, prefix: str = "") -> Iterator[tuple[str, Any, Any, str]]:
    """Every string at public path ``path`` (relative to ``obj``; ``a[].b``, ``c[]`` = list of strings) as
    ``(indexed path, holder, key, value)``; ``holder[key] = new`` replaces it."""
    if obj is None:
        return
    head, _, rest = path.partition(".")
    is_list = head.endswith("[]")
    key = head.removesuffix("[]")
    if not isinstance(obj, dict) or obj.get(key) is None:
        return
    here = f"{prefix}.{key}" if prefix else key
    val = obj[key]
    if is_list:
        for i, v in enumerate(val):
            ip = f"{here}[{i}]"
            if not rest:
                if isinstance(v, str):
                    yield ip, val, i, v
            else:
                yield from walk_strings(v, rest, ip)
    elif not rest:
        if isinstance(val, str):
            yield here, obj, key, val
    else:
        yield from walk_strings(val, rest, here)


def _ann_path(p: str) -> tuple[str, str]:
    """``annotations.<kind>.payload.<rest>`` -> (kind, rest)."""
    m = re.fullmatch(r"annotations\.(\w+)\.payload\.(.+)", p)
    if m is None:
        raise DatasetError(f"mapping: capped/prose path {p!r} is not annotations.<kind>.payload.<path>")
    return m.group(1), m.group(2)


def withhold_abstract_paths(row: dict[str, Any], paths: Sequence[str]) -> None:
    """Set each of ``paths`` (``annotations.<kind>.payload.<a.b>``) to NULL in a
    release row, in place; a kind or struct that is NULL stays so (``Mapping_.abstract_paths``)."""
    for p in paths:
        kind, rest = _ann_path(p)
        node = ((row.get("annotations") or {}).get(kind) or {}).get("payload")
        *parents, leaf = rest.split(".")
        for k in parents:
            node = node.get(k) if isinstance(node, dict) else None
        if isinstance(node, dict) and leaf in node:
            node[leaf] = None


def _scrub(s: str, rules: Sequence[Mapping[str, Any]]) -> str:
    for r in rules:
        def rep(m: re.Match[str], r: Mapping[str, Any] = r) -> str:
            out = str(r["replace"])
            if r.get("keep_case") and m.group(0)[:1].isupper():
                out = out[:1].upper() + out[1:]
            return out
        s = re.sub(r["pattern"], rep, s)
    return s


def scrub_url(url: str) -> str:
    """``url`` without the query parameters a fetch adds and a document link does not need (D787): the contact
    parameters (:data:`URL_CONTACT_PARAMS`) and a signed URL's signature (:data:`URL_SIGNED_PREFIXES`,
    :data:`URL_SIGNATURE_PARAMS`, :data:`URL_SAS_PARAMS`, each beside its marker). Every other byte is kept: the
    remaining parameters in order and as encoded, the fragment; ``?`` goes when no parameter is left. A URL with
    nothing to drop comes back unchanged (the same string)."""
    base, hsh, frag = url.partition("#")
    head, q, query = base.partition("?")
    if not query:
        return url
    parts = query.split("&")
    names = [urllib.parse.unquote_plus(p.partition("=")[0]).strip().lower() for p in parts]
    have = set(names)

    def drop(n: str) -> bool:
        if n in URL_CONTACT_PARAMS:
            return True
        if any(n.startswith(pre) and {pre + "signature", pre + "credential"} & have for pre in URL_SIGNED_PREFIXES):
            return True
        if n in URL_SIGNATURE_PARAMS and "signature" in have:
            return True
        return n in URL_SAS_PARAMS and {"sig", "sv"} <= have

    kept = [p for p, n in zip(parts, names, strict=True) if not drop(n)]
    if len(kept) == len(parts):
        return url
    kept = [p for p in kept if p]
    return head + ("?" + "&".join(kept) if kept else "") + hsh + frag


# ---------------------------------------------------------------- the build's inputs
def _strip_provider(model: str | None) -> str | None:
    """The bare model name: without a provider qualifier (``anthropic:claude-opus-5-5``, D722) and without the
    vendor prefix (``openai/gpt-6-luna``), so a config id keeps its four ``:``-separated parts."""
    if model:
        from stack.config import split_model_ref

        model = split_model_ref(model)[1]
    return model.split("/", 1)[1] if model and "/" in model else model


def _git_commit() -> str:
    """The checkout's commit (``+dirty`` with tracked changes); without git or a ``.git`` (a container image built
    from a pinned ref), ``STACK_CODE_COMMIT`` (the image's build arg, docs/DEPLOY-railway.md) cut to 12 characters
    with ``+image``; else ``unknown``."""
    try:
        head = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--short=12", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        image = os.environ.get("STACK_CODE_COMMIT", "").strip()
        return f"{image[:12]}+image" if re.fullmatch(r"[0-9a-f]{7,40}", image) else "unknown"
    return head + ("+dirty" if dirty else "")


@dataclass
class ConfigRow:
    config_id: str
    kinds: list[str]
    stage: str
    prompt_name: str | None
    prompt_version: str | None
    model: str | None
    provider: str | None
    parameters: str  # JSON
    taxonomy: dict[str, Any] | None
    components: str  # JSON
    defined_in: str | None
    code_digest: str | None
    config_digest: str
    release_ids: list[str]
    override_ids: list[str]
    note: str | None


CONFIGS_SCHEMA = pa.schema([
    pa.field("config_id", pa.string(), False, metadata=_desc("The id annotations.<kind>.metadata.config_id carries.")),
    pa.field("kinds", pa.list_(pa.string()), False, metadata=_desc("The kinds this call or override produced.")),
    pa.field("stage", pa.string(), False, metadata=_desc("Internal producer: annotate.<workflow>, or overrides.")),
    pa.field("prompt_name", pa.string(), metadata=_desc("Prompt name (the production prompt file of the pipeline "
                                                         "repository; its digests are in components).")),
    pa.field("prompt_version", pa.string(), metadata=_desc("Version token of the prompt name.")),
    pa.field("model", pa.string(), metadata=_desc("Primary model as the provider names it (fallbacks in parameters).")),
    pa.field("provider", pa.string(), metadata=_desc("Model provider.")),
    pa.field("parameters", pa.string(), False, metadata=_desc("JSON: the plan pin's parameters and the LLM role's "
                                                                 "(model, fallbacks, max_tokens, temperature, reasoning_effort) "
                                                                 "as the run recorded them; role null when it recorded none "
                                                                 "(see note).")),
    pa.field("taxonomy", pa.struct([pa.field("name", pa.string()), pa.field("version", pa.string()),
                                    pa.field("sha256", pa.string()), pa.field("path", pa.string())]),
             metadata=_desc("The vocabulary file the call was pinned to (taxonomies/).")),
    pa.field("components", pa.string(), False, metadata=_desc("JSON: deterministic components and pinned files.")),
    pa.field("defined_in", pa.string(), metadata=_desc("Pipeline module that implements the call.")),
    pa.field("code_digest", pa.string(), metadata=_desc("The pipeline code digest of the run that recorded this config.")),
    pa.field("config_digest", pa.string(), False, metadata=_desc("The pipeline's full config digest (64 hex); for an "
                                                                    "override config the overrides rows' fingerprint.")),
    pa.field("release_ids", pa.list_(pa.string()), False, metadata=_desc("Releases that used it.")),
    pa.field("override_ids", pa.list_(pa.string()), False, metadata=_desc("For override:<ids> configs: the rows "
                                                                          "(config/overrides.jsonl).")),
    pa.field("note", pa.string(), metadata=_desc("Plain-language note.")),
])


@dataclass
class Inputs:
    """Everything the projection reads besides the corpus rows (module docstring, "Inputs")."""

    store_root: Path
    build: dict[str, Any]
    build_status: dict[str, Any]
    corpus_header: dict[str, str]
    families: dict[str, dict[str, tuple[list[str], bool]]]  # wf -> doc_id -> (override_ids, no evidence left)
    evidence_model: dict[str, dict[str, str | None]]  # producer wf -> evidence_id -> model
    pins: dict[str, dict[str, Any]]  # config_digest -> {workflow, prompt_pin, run_id, code_digest}
    overrides_head: str  # file id of the applied chain head
    chain: list[tuple[Path, dict[str, str], list[Override]]]  # chain parts up to the head, oldest first: new rows each
    overrides: dict[str, Override]  # every row of the applied head
    build_configs: list[str] = field(default_factory=list)  # config digests of the build's annotate runs
    #: annotate stage -> its run's ``selection`` and the target's :func:`stack.annotate.stage.config_name` (D758)
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)


def _read_rec(entry: Any) -> dict[str, Any]:
    rec = entry.record
    return rec if isinstance(rec, dict) else rec.model_dump(mode="python")


def load_inputs(store_root: Path, build_id: str, status: str, corpus_header: Mapping[str, str]) -> Inputs:
    idx = store_index(store_root)
    b = idx.build(build_id)
    if b is None:
        raise DatasetError(f"build {build_id}: no build record")
    rec = b.record
    files = rec.get("files") or {}
    runs = {e.run_id: e for e in idx.runs}
    pins: dict[str, dict[str, Any]] = {}
    heads: set[str] = set()
    for e in idx.runs:  # every local run record with a prompt pin: a carried answer may name an older run's config
        r = e.record
        if r.prompt_pin and r.config_digest:
            pp = r.prompt_pin
            from stack.annotate.stage import PIN_DIGEST_KEYS

            if H({k: pp[k] for k in PIN_DIGEST_KEYS}) != r.config_digest:
                raise DatasetError(f"run {r.run_id}: its prompt_pin does not hash to its config_digest")
            pins.setdefault(r.config_digest, {"workflow": pp["workflow"], "prompt_pin": pp, "run_id": r.run_id,
                                              "code_digest": r.code_digest})
    for rid in rec.get("runs") or []:
        run = runs.get(rid)
        if run is None:
            raise DatasetError(f"build {build_id}: run record {rid} is not in the store")
        ov = (run.record.inputs or {}).get("overrides")
        if ov:
            heads.add(ov)
    if len(heads) != 1:
        raise DatasetError(f"build {build_id}: its runs read {len(heads)} overrides heads {sorted(heads)}; a dataset "
                           "release needs exactly one (one chain head per build)")
    head = heads.pop()
    chain = _overrides_chain(store_root, head)
    overrides = {o.override_id: o for _p, _h, rows in chain for o in rows}
    families: dict[str, dict[str, tuple[list[str], bool]]] = {}
    evidence_model: dict[str, dict[str, str | None]] = {}
    for wf in REGISTRY:
        f = files.get(wf)
        if f is None:
            continue
        tbl, _h = read_family_fields(data_file(store_root, f["path"], wf), ["doc_id", "override_ids", "evidence_id"])
        per: dict[str, tuple[list[str], bool]] = {}
        for d, ids, ev in zip(tbl.column("doc_id").to_pylist(), tbl.column("override_ids").to_pylist(),
                              tbl.column("evidence_id").to_pylist(), strict=True):
            old, none_left = per.get(d, ([], True))
            merged = list(dict.fromkeys([*old, *(ids or [])]))
            per[d] = (merged, none_left and ev is None)
        families[wf] = per
        ef = files.get(f"{REGISTRY[wf].produced_by}_evidence")
        if ef is not None and REGISTRY[wf].produced_by not in evidence_model:
            et, _h = read_family_fields(data_file(store_root, ef["path"], f"{REGISTRY[wf].produced_by}_evidence"),
                                         ["evidence_id", "model"])
            evidence_model[REGISTRY[wf].produced_by] = dict(zip(et.column("evidence_id").to_pylist(),
                                                               et.column("model").to_pylist(), strict=True))
    build_configs = sorted({runs[r].record.config_digest for r in rec.get("runs") or []
                            if runs[r].record.stage.startswith("annotate.") and runs[r].record.prompt_pin})
    return Inputs(store_root, rec, build_status(store_root, build_id, status), dict(corpus_header), families,
                  evidence_model, pins, head, chain, overrides, build_configs,
                  stage_selections([runs[r].record for r in rec.get("runs") or []]))


def stage_selections(records: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """Per ``annotate.<wf>`` stage of the runs ``records`` (the build's, in order: the last run of a stage wins), the
    ``selection`` its run record carries (D758) and its target's name (:func:`stack.annotate.stage.config_name` of the
    run's pin). A run recorded before D758 has none: its stage selects its own config only (``accepted`` empty, the
    selection digest computed here)."""
    from stack.annotate.stage import config_name
    from stack.plan import selection_digest

    out: dict[str, dict[str, Any]] = {}
    for r in records:
        if not r.stage.startswith("annotate.") or not r.prompt_pin:
            continue
        sel = r.selection or {"digest": selection_digest(r.config_digest, []), "target": r.config_digest, "accepted": []}
        out[r.stage] = {"selection": sel, "name": config_name(r.prompt_pin)}
    return out


def _overrides_chain(store_root: Path, head_file_id: str) -> list[tuple[Path, dict[str, str], list[Override]]]:
    """The parts of the overrides chain up to ``head_file_id``, oldest first, each with the rows it added (the parts
    are cumulative full files: a part's new rows are those not in the part before it). Refuses a chain that is not
    linked (each part's ``prior`` names the one before), a part that changes an earlier row, and a missing part."""
    idx = store_index(store_root)
    for _e, _k, rec in idx.data_outputs():  # a mirror pulls on read: fetch every recorded overrides file first
        if str(rec["path"]).startswith("data/overrides/"):
            idx.fetch_output(rec)
    files = sorted((store_root / "data" / "overrides").glob("*/*.parquet"))
    by_id: dict[str, tuple[Path, dict[str, str]]] = {}
    for p in files:
        h = read_header(p)
        by_id[h["file_id"]] = (p, dict(h))
    if head_file_id not in by_id:
        raise DatasetError(f"overrides head {head_file_id} is not in data/overrides/ of the store")
    order: list[str] = []
    cur: str | None = head_file_id
    while cur is not None:
        if cur not in by_id:
            raise DatasetError(f"overrides chain: part {cur} (prior of {order[-1]}) is not local; the per-run export "
                               "needs every part of the chain up to the head")
        order.append(cur)
        prior = json.loads(by_id[cur][1].get("prior") or "[]")
        if len(prior) > 1:
            raise DatasetError(f"overrides chain: part {cur} has {len(prior)} priors")
        cur = prior[0] if prior else None
    order.reverse()
    out = []
    seen: dict[str, str] = {}
    for fid in order:
        p, h = by_id[fid]
        tbl = pq.read_table(p)
        rows = [Override.from_row(r) for r in tbl.to_pylist()]
        new = []
        for o in rows:
            j = jcs(o.to_row() | {"created_at": o.created_at.isoformat()})
            if o.override_id in seen:
                if seen[o.override_id] != j:
                    raise DatasetError(f"overrides chain: part {fid} changes row {o.override_id}")
            else:
                seen[o.override_id] = j
                new.append(o)
        gone = set(seen) - {o.override_id for o in rows}
        if gone:
            raise DatasetError(f"overrides chain: part {fid} drops rows {sorted(gone)[:3]}")
        out.append((p, h, sorted(new, key=lambda o: (o.created_at, o.override_id))))
    return out


def jcs(v: Any) -> str:
    from stack.core.hashes import jcs as _jcs

    return _jcs(v)


# ---------------------------------------------------------------- the projection
@dataclass
class Projection:
    """State of one variant's projection: the mapping, inputs, policy, and what the rows produced."""

    m: Mapping_
    inp: Inputs
    variant: str
    release_id: str
    cap: int | None
    text_get: Callable[[str], bytes] | None = None  # the inline-text variant's blob reader
    configs: dict[str, ConfigRow] = field(default_factory=dict)
    remap: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    cuts_total: int = 0
    prose_scrubbed: int = 0
    #: URL values :func:`scrub_url` changed (:meth:`scrub_urls`)
    urls_scrubbed: int = 0
    counts: dict[str, Counter[str]] = field(default_factory=dict)
    #: Shipped affiliations per ``org_status``, and ``with_org_ids`` (those naming at least one organisation).
    org_counts: Counter[str] = field(default_factory=Counter)
    #: sha256 → the repo path of each vocabulary file a config row names (:func:`taxonomy_source`)
    taxonomy_src: dict[str, str] = field(default_factory=dict)
    #: config digest → the documents whose producer kind (the workflow's own payload) is under it
    config_docs: Counter[str] = field(default_factory=Counter)
    #: config id → the same documents by the config id they carry (one digest can have a model and a ``rule`` id)
    config_id_docs: Counter[str] = field(default_factory=Counter)
    def internal_payload(self, kind: str, value: Any, annotations: Mapping[str, Any] | None = None,
                         doc: str = "?") -> Any:
        """The internal payload of ``kind`` as the release projects it: for the mapping's ``org_fields.kind``, with
        :data:`ORG_FIELDS` from the document's decided ``org_fields.from`` record in ``annotations`` (the row's internal
        block; None, as for an override's value: the fields stay NULL), counted in :attr:`org_counts`."""
        if kind != self.m.org_kind or self.m.org_from is None:
            return value
        orec = (annotations or {}).get(self.m.kinds[self.m.org_from].workflow)
        orgs = orec.get("payload") if orec is not None and orec.get("status") in ("ok", "empty") else None
        out, problem = with_orgs(value, orgs)
        if problem:
            self.problems.append(f"{doc}: {problem}")
        if annotations is not None and out is not None:
            for it in out.get("items") or []:
                for af in it.get("affiliations") or []:
                    self.org_counts[af["org_status"] or "no_record"] += 1
                    self.org_counts["with_org_ids"] += bool(af["org_ids"])
        return out

    def text_body(self, r: Mapping[str, Any]) -> str | None:
        """``content.text.body``: the converted text, read by its sha256 and checked against it (inline variant);
        NULL in a variant that distributes no text."""
        if self.m.variants[self.variant]["texts"] != "inline":
            return None
        d = (r.get("content") or {}).get("current_text_sha256")
        if d is None or self.text_get is None:
            raise DatasetError(f"{r['doc_id']}: the inline variant needs the text and its reader")
        data = self.text_get(d)
        if hashlib.sha256(data).hexdigest() != d:
            raise DatasetError(f"text {d[:12]}… of {r['doc_id']}: its bytes do not hash to content.text.sha256")
        return data.decode("utf-8")

    # -- configs
    def config_id(self, kind: str, digest: str, computed: bool) -> str:
        pin = self.inp.pins.get(digest)
        if pin is None:
            raise DatasetError(f"config_digest {digest[:12]}… ({kind}): no local run record carries its prompt pin")
        pp = pin["prompt_pin"]
        prod = PRODUCER_KIND[kind]
        model = "rule" if computed else _strip_provider((pp.get("models") or [None])[0]) or "none"
        cid = f"{prod}:{pp.get('prompt_name') or 'none'}:{model}:{digest[:8]}"
        if kind in self.m.kinds and self.m.kinds[kind].workflow == pp["workflow"]:
            self.config_docs[digest] += 1
            self.config_id_docs[cid] += 1
        row = self.configs.get(cid)
        if row is None:
            row = self.configs[cid] = self._config_row(cid, prod, pp, pin, digest, computed)
        if kind not in row.kinds:
            row.kinds.append(kind)
        return cid

    def _config_row(self, cid: str, prod: str, pp: Mapping[str, Any], pin: Mapping[str, Any], digest: str,
                    computed: bool) -> ConfigRow:
        wf = pp["workflow"]
        name = pp.get("prompt_name")
        version = (re.search(r"-(v\d[\w.\-]*)$", name or "") or [None, None])[1] if name else None
        plan_pin = {k: v for k, v in (pp.get("plan_pin") or {}).items() if k not in ("name",)}
        # the LLM role as the run resolved it (prompt_pin.role_params, D758), never today's llm.yaml under an old config;
        # a run that recorded none (before D758) exports null and says so; a computed workflow has no role
        role = pp.get("role_params") if pp.get("models") else None
        unrecorded = bool(pp.get("models")) and "role_params" not in pp
        models = [_strip_provider(x) for x in pp.get("models") or []]
        taxonomy = None
        comps: dict[str, Any] = {"template_digests": pp.get("template_digests")}
        if wf in TAXONOMY_FILES:
            taxonomy = self.taxonomy_of(cid, pp)
        if wf == "model_roles":
            comps["name_matcher"] = "deterministic catalogue patterns (workflows/annotate/model_roles/match.py)"
        if computed:
            comps["method"] = COMPUTED_METHOD[wf]
        return ConfigRow(cid, [], f"annotate.{wf}", None if computed else name, None if computed else version,
                         None if computed else (models[0] if models else None),
                         None if computed else ((pp.get("models") or [""])[0].split("/", 1)[0] or None),
                         jcs({"plan_pin": plan_pin, "role": role, "models": models}), taxonomy, jcs(comps),
                         f"workflows/annotate/{wf}/main.py", pin.get("code_digest"), digest, [self.release_id], [],
                         "computed by the stage without a model call" if computed else
                         ROLE_UNRECORDED if unrecorded else None)

    def taxonomy_of(self, label: str, pp: Mapping[str, Any]) -> dict[str, Any]:
        """The vocabulary file a stored prompt pin names (:func:`taxonomy_source`) as a config row's ``taxonomy``
        (``name, version, sha256, path`` in the bundle), its repo path remembered for :func:`write_taxonomies`."""
        wf = pp["workflow"]
        tname = TAXONOMY_FILES[wf][0]
        try:
            tpath, sha = taxonomy_source(wf, pp.get("plan_pin") or {}, pp.get("template_digests") or [])
        except DatasetError as exc:
            raise DatasetError(f"{label}: {exc}") from None
        ver = str((yamlio.load((REPO_ROOT / tpath).read_text("utf-8")) or {}).get("version") or "")
        self.taxonomy_src[sha] = tpath
        return {"name": tname, "version": ver, "sha256": sha, "path": f"taxonomies/{tname}-{ver}.yaml"}

    def build_taxonomies(self) -> list[dict[str, Any]]:
        """The vocabularies of the build's own annotate runs' configs (:attr:`Inputs.build_configs`), shipped even when
        no row is under them (every selected row accepted from an earlier config, D758)."""
        out = []
        for d in self.inp.build_configs:
            pin = self.inp.pins.get(d)
            if pin is not None and pin["workflow"] in TAXONOMY_FILES:
                out.append(self.taxonomy_of(f"build config {d[:12]}…", pin["prompt_pin"]))
        return out

    def override_config(self, kind: str, used: Sequence[Override], digest: str) -> str:
        cid = "override:" + "+".join(o.override_id for o in used)
        row = self.configs.get(cid)
        if row is None:
            authors = sorted({o.author or "" for o in used})
            model = next((a.removeprefix("ai:") for a in authors if a.startswith("ai:")), None)
            row = self.configs[cid] = ConfigRow(
                cid, [], "overrides", None, None, model, "anthropic" if model and model.startswith("claude") else None,
                jcs({}), None, jcs({"overrides_file": OVERRIDES_FILE}), None, None, digest, [self.release_id],
                [o.override_id for o in used], f"payload supplied by overrides; see {OVERRIDES_FILE}")
        if kind not in row.kinds:
            row.kinds.append(kind)
        return cid

    # -- one row
    def row(self, r: dict[str, Any]) -> dict[str, Any]:
        ctx: dict[str, Any] = {"row": r, "cuts": [], "quote_chars": 0}
        ann = self.annotations(r)
        self.cap_and_scrub(ann, ctx)
        out: dict[str, Any] = self._project(self.m.root, r, ctx)
        self.scrub_urls(out)
        out["annotations"] = ann
        return out

    def scrub_urls(self, out: dict[str, Any]) -> None:
        """:func:`scrub_url` over the projected row's :data:`URL_FIELDS`, in place, counted in :attr:`urls_scrubbed`."""
        for path in URL_FIELDS:
            for _ip, holder, key, s in list(walk_strings(out, path)):
                new = scrub_url(s)
                if new != s:
                    holder[key] = new
                    self.urls_scrubbed += 1

    def _project(self, n: Node, obj: Any, ctx: dict[str, Any]) -> Any:
        if n.kind == "transform":
            return TRANSFORMS[n.entry["transform"]](self, ctx)
        if n.kind == "annotations":
            return None  # filled by row()
        if n.kind == "group":
            return {c.name: self._project(c, obj, ctx) for c in n.children}
        v = _get(obj, n.rel)
        if n.kind == "leaf" or v is None:
            return copy.deepcopy(v) if n.kind == "leaf" else None
        if n.kind == "struct":
            return {c.name: self._project(c, v, ctx) for c in n.children}
        ex = n.entry.get("exclude") or {}
        out = []
        for el in v:
            if any(el.get(k) in vals for k, vals in ex.items()):
                continue
            out.append({c.name: self._project(c, el, ctx) for c in n.children})
        return out

    def annotations(self, r: dict[str, Any]) -> dict[str, Any]:
        a = r["annotations"]
        doc = r["doc_id"]
        text_sha = (r.get("content") or {}).get("current_text_sha256")
        out: dict[str, Any] = {}
        internal: dict[str, Any] = {}
        for kind, k in self.m.kinds.items():
            rec = a.get(k.workflow)
            if rec is not None and rec["basis_kind"] == "chained" and rec["status"] == "skipped" \
                    and rec.get("error_class") == "no_basis":
                rec = None  # a chained kind whose read was never decided: not run, like a document out of scope
            if rec is None:
                out[kind] = None
                self.counts.setdefault(kind, Counter())["not_run"] += 1
                continue
            internal[kind] = rec
            spec = REGISTRY[k.workflow]
            if spec.basis_kind == "chained":
                want, what = chained_basis(text_sha, a, spec.reads), "chained basis over " + ", ".join(spec.reads)
            elif spec.basis_kind == "fields":  # D755 (translation): the metadata fields' digest
                want, what = (r.get("metadata") or {}).get("fields_digest"), "metadata fields_digest"
            else:
                want, what = text_sha, "text sha256"
            if rec["basis_kind"] != spec.basis_kind or rec["basis_digest"] != want or rec["is_basis_current"] is not True:
                self.problems.append(f"{doc} {kind}: basis gate (basis_kind {spec.basis_kind}, basis_digest = the "
                                     f"{what}, is_basis_current) fails")
            payload = project_payload(self.internal_payload(kind, rec.get("payload"), a, doc), k.ptype, k.names)
            if payload is None:
                payload = {f.name: None for f in k.ptype}
            out[kind] = {"metadata": self.meta(kind, k, rec, doc), "payload": payload}
        for o in a.get("other") or []:
            if o.get("workflow") not in self.m.json_kinds:
                raise DatasetError(f"{doc}: annotations.other has {o.get('workflow')!r}, which has no json_kinds entry "
                                   "(registry entry, mapping entry and JSON Schema first)")
        out["other"] = []
        self.apply_remap(doc, out, internal)
        for kind, v in out.items():
            if kind != "other" and v is not None:
                self.counts.setdefault(kind, Counter())[v["metadata"]["status"]] += 1
        return out

    def meta(self, kind: str, k: KindSpec, rec: Mapping[str, Any], doc: str) -> dict[str, Any]:
        wf = k.workflow
        spec = REGISTRY[wf]
        ids, none_left = self.inp.families.get(wf, {}).get(doc, ([], False))
        used = [self.inp.overrides[i] for i in ids if i in self.inp.overrides]
        if len(used) != len(ids):
            missing = [i for i in ids if i not in self.inp.overrides]
            raise DatasetError(f"{doc} {kind}: override ids {missing} are not in the applied overrides chain")
        edits = [o for o in used if o.kind in ("row", "field")]
        if not ids:
            prov = "pipeline"
        elif (spec.grain == "document" and any(o.kind == "row" for o in edits)) or \
                (spec.grain == "item" and none_left and edits):
            prov = "override"
        else:
            prov = "mixed"
        # a computed workflow's answer; an LLM workflow's row without evidence (not_attempted in an incomplete build,
        # select stamps it origin computed) is not one: it names its config as any row of that workflow does
        computed = rec["origin"] in ("computed", "rule") and spec.produced_by in COMPUTED_METHOD
        annotator: str | None
        if prov == "override":
            want = rows_digest(edits)
            if want is None or rec["config_digest"] != want:
                raise DatasetError(f"{doc} {kind}: overrides supplied the payload but the corpus carries config digest "
                                   f"{rec['config_digest'][:12]}…, not their fingerprint (folded before D717?); "
                                   "rebuild the annotations")
            cid = self.override_config(kind, edits, want)
            authors = {o.author or "" for o in edits}
            ai = sorted(a.removeprefix("ai:") for a in authors if a.startswith("ai:"))
            annotator = ai[0] if len(ai) == 1 and len(authors) == 1 else ("human" if not ai else "+".join(ai))
        else:
            cid = self.config_id(kind, rec["config_digest"], computed)
            if kind == "model_name_matches":
                annotator = "matcher"
            elif computed:
                annotator = "rule"
            else:
                em = self.inp.evidence_model.get(spec.produced_by, {}).get(rec.get("evidence_id") or "")
                annotator = _strip_provider(em) or self.configs[cid].model
        fields: list[str] = []
        for o in edits:
            if o.kind == "field" and o.field:
                fields.append(o.field)
            elif spec.grain == "item" and o.item_key:
                fields.append(f"items[{o.item_key}]")
        return {"kind": kind, "version": rec["version"], "status": rec["status"], "error_class": rec["error_class"],
                "config_id": cid, "decided_at": rec["decided_at"], "origin": rec["origin"], "annotator": annotator,
                "provenance": prov, "override_ids": list(ids), "override_fields": list(dict.fromkeys(fields))}

    def apply_remap(self, doc: str, out: dict[str, Any], internal: Mapping[str, Any]) -> None:
        rules = {r["id"]: r for r in self.m.remap_rules}

        def change(kind: str, fieldname: str, new: Any, rule: str) -> None:
            md = out[kind]["metadata"]
            old = md[fieldname]
            if old == new:
                return
            md[fieldname] = new
            self.remap.append({"release_id": self.release_id, "doc_id": doc, "kind": kind, "field": fieldname,
                               "old": old, "new": new, "rule": rule, "reason": rules[rule]["reason"]})

        if "override_empty" in rules:
            for kind in ("safety_techniques", "model_roles"):
                v = out.get(kind)
                if v is None or v["metadata"]["provenance"] != "override" or v["metadata"]["status"] != "ok":
                    continue
                if producer_empty(kind, internal[kind].get("payload")):
                    change(kind, "status", "empty", "override_empty")
        if "producer_call_failed" in rules:
            v, mr = out.get("model_name_matches"), out.get("model_roles")
            if v is not None and mr is not None and v["metadata"]["status"] == "failed_permanent":
                base = internal["model_roles"]
                failed = (base["status"] == "failed_permanent" and base["error_class"] == v["metadata"]["error_class"]) \
                    or mr["metadata"]["provenance"] == "override"
                if failed:
                    change("model_name_matches", "error_class", "producer_call_failed", "producer_call_failed")
                else:
                    self.problems.append(f"{doc}: model_name_matches failed_permanent without a failed producing call")

    def cap_and_scrub(self, ann: dict[str, Any], ctx: dict[str, Any]) -> None:
        cap = self.cap
        for p in self.m.quote_paths + self.m.surface_paths:
            kind, rest = _ann_path(p)
            v = ann.get(kind)
            if v is None:
                continue
            is_quote = p in self.m.quote_paths
            for ip, holder, key, s in list(walk_strings(v["payload"], rest, f"annotations.{kind}.payload")):
                if cap is not None and len(s) > cap:
                    new = cut_string(s, cap)
                    holder[key] = new
                    ctx["cuts"].append({"path": ip, "chars": len(s)})
                    self.cuts_total += 1
                    s = new
                if is_quote:
                    ctx["quote_chars"] += len(s)
        for sc in self.m.prose_scrub:
            kind, rest = _ann_path(sc["path"])
            v = ann.get(kind)
            if v is None:
                continue
            for _ip, holder, key, s in list(walk_strings(v["payload"], rest)):
                new = _scrub(s, sc["rules"])
                if new != s:
                    holder[key] = new
                    self.prose_scrubbed += 1


def producer_empty(kind: str, payload: Mapping[str, Any] | None) -> bool:
    """The producer's own empty test for an internal payload (mapping ``status_remap`` ``override_empty``)."""
    if payload is None:
        return False
    if kind == "safety_techniques":
        return not (payload.get("techniques") or []) and not (payload.get("extra_techniques") or [])
    if kind == "model_roles":
        graded = any(any((v or 0) >= 1 for v in (m.get("roles") or {}).values()) for m in payload.get("models") or [])
        return not graded and not (payload.get("variant_models") or []) and not (payload.get("extra_models") or [])
    raise DatasetError(f"producer_empty: no empty rule for {kind}")


@functools.lru_cache(maxsize=32)
def _file_sha256(path: str) -> str | None:
    p = REPO_ROOT / path
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def taxonomy_source(wf: str, plan_pin: Mapping[str, Any], template_digests: Sequence[str]) -> tuple[str, str]:
    """``(repo path, sha256)`` of the vocabulary file a stored pin names (D758): the plan pin's path key
    (:data:`TAXONOMY_PIN_KEYS`) when it has one, else the catalogue's default (:data:`TAXONOMY_FILES`) or one of its
    vendored per-version files (:data:`VENDORED_LOCK` entries under ``taxonomies/<name>/``), whichever hashes to one
    of the pin's template digests; refused (:class:`DatasetError`) when none does. So a row judged under an earlier
    vocabulary (models 1.0.0 under ``configs/models.yaml`` before it became 1.1.0) exports with that version's file."""
    tname, default = TAXONOMY_FILES[wf]
    key = TAXONOMY_PIN_KEYS.get(wf)
    if key and plan_pin.get(key):
        cands = [str(plan_pin[key])]
    else:
        lock = (yamlio.load(VENDORED_LOCK.read_text("utf-8")) or {}).get("files") or {}
        cands = [default, *sorted(p for p, e in lock.items()
                                  if p != default and Path(str(e.get("source", ""))).parent.name == tname)]
    pinned = set(template_digests)
    for path in cands:
        sha = _file_sha256(path)
        if sha is not None and sha in pinned:
            return path, sha
    raise DatasetError(f"annotate.{wf}: no file of {cands} hashes to a template digest the run pinned "
                       f"({sorted(d[:12] for d in pinned)}): its vocabulary is not in the repository")


#: ``config/configs.json`` ``note`` of a config whose run recorded no role parameters (D758).
ROLE_UNRECORDED = ("parameters.role is null: the run that recorded this config did not record its LLM role's "
                   "parameters (runs before D758), and today's llm.yaml is not substituted under an earlier config; "
                   "the role's name is plan_pin.role (or the workflow's default)")


def _get(obj: Any, rel: str | None) -> Any:
    if rel is None or obj is None:
        return obj
    for part in rel.split("."):
        if obj is None:
            return None
        obj = obj.get(part)
    return obj


# ---------------------------------------------------------------- transforms (mapping `transform` names)
def _current_snapshot(r: Mapping[str, Any]) -> dict[str, Any]:
    c = r.get("content") or {}
    snaps = [s for s in c.get("snapshots") or [] if s["snapshot_id"] == c.get("current_snapshot_id")]
    if len(snaps) != 1:
        raise DatasetError(f"{r['doc_id']}: {len(snaps)} snapshots carry content.current_snapshot_id")
    return dict(snaps[0])


def _is_year_only(v: dt.datetime) -> bool:
    from workflows.gather.merge import is_year_only

    return is_year_only(v)


def _t_abstract(p: Projection, ctx: dict[str, Any]) -> str:
    r = ctx["row"]
    has = r["metadata"].get("abstract") is not None
    listed = any(fs["field"] == "abstract" for fs in r["metadata"]["field_sources"])
    if has != listed:
        p.problems.append(f"{r['doc_id']}: abstract present={has} but field_sources lists it={listed}")
    if not has:
        return "none"
    return "shipped" if p.m.variants[p.variant]["abstract"] == "keep" else "withheld_pending_rights_review"


TRANSFORMS: dict[str, Callable[[Projection, dict[str, Any]], Any]] = {
    "release_id": lambda p, ctx: p.release_id,
    "published_date": lambda p, ctx: (lambda v: v.astimezone(dt.UTC).date() if v is not None else None)(
        ctx["row"]["metadata"].get("published_at")),
    "published_precision": lambda p, ctx: (lambda v: "unknown" if v is None else (
        "year_inferred" if _is_year_only(v) else "day"))(ctx["row"]["metadata"].get("published_at")),
    "canonical_url": lambda p, ctx: next((m.get("canonical_url") for m in ctx["row"]["identity"]["members"]
                                         if m["source_id"] == ctx["row"]["identity"]["anchor_source_id"]), None),
    "content.source.url": lambda p, ctx: _current_snapshot(ctx["row"]).get("url"),
    "content.source.sha256": lambda p, ctx: (_current_snapshot(ctx["row"]).get("raw") or {}).get("sha256"),
    "content.source.bytes": lambda p, ctx: (_current_snapshot(ctx["row"]).get("raw") or {}).get("bytes"),
    "content.source.media_type": lambda p, ctx: (_current_snapshot(ctx["row"]).get("raw") or {}).get("media_type"),
    "content.source.captured_at": lambda p, ctx: _current_snapshot(ctx["row"])["captured_at"],
    "content.source.revision": lambda p, ctx: _current_snapshot(ctx["row"]).get("revision"),
    "content.text.bytes": lambda p, ctx: (_current_snapshot(ctx["row"]).get("text") or {}).get("bytes"),
    "content.text.media_type": lambda p, ctx: (_current_snapshot(ctx["row"]).get("text") or {}).get("media_type"),
    "content.text.converter": lambda p, ctx: (_current_snapshot(ctx["row"]).get("quality") or {}).get("converter"),
    "release.abstract": _t_abstract,
    "release.text": lambda p, ctx: p.m.variants[p.variant]["texts"],
    "content.text.body": lambda p, ctx: p.text_body(ctx["row"]),
    "release.quote_chars": lambda p, ctx: ctx["quote_chars"],
    "release.cuts": lambda p, ctx: ctx["cuts"],
    "annotations.other[].schema": lambda p, ctx: None,  # empty in this format (json_kinds)
}


def check_transforms(m: Mapping_) -> None:
    want, have = set(m.transforms), set(TRANSFORMS)
    if want != have:
        raise DatasetError(f"mapping transforms {sorted(want - have)} are not implemented; implemented but unmapped "
                           f"{sorted(have - want)}")


def row_drop_reason(r: Mapping[str, Any]) -> str | None:
    """mapping ``rows.drop``: retired, masked, the anchor member's class ``shadow``, sensitivity (that precedence)."""
    if r["state"] == "retired":
        return "retired"
    if r["identity"]["is_masked"]:
        return "masked"
    anchor = r["identity"]["anchor_source_id"]
    if any(m["source_id"] == anchor and m.get("license_class") == "shadow" for m in r["identity"]["members"]):
        return "shadow_anchor"
    if r["metadata"].get("sensitivity"):
        return "sensitivity"
    return None


# ---------------------------------------------------------------- overrides export
class OverridesPolicy:
    def __init__(self, path: Path) -> None:
        self.raw = yamlio.load(path.read_text("utf-8"))
        self.markers = [re.compile(p) for p in self.raw["internal_markers"]]

    def author(self, o: Override) -> tuple[str, str]:
        a = o.author or ""
        authors = self.raw["authors"]
        if a in authors:
            spec = authors[a]
        elif a.startswith("ai:") and "ai:*" in authors:
            spec = authors["ai:*"]
        else:
            raise DatasetError(f"override {o.override_id}: author {a!r} has no public author/method "
                               "(overrides-public.yaml authors)")
        model = a.removeprefix("ai:")
        return spec["author"].format(model=model), spec["method"]

    def kind(self, o: Override) -> str:
        tgt = "annotation" if o.target in REGISTRY else o.target
        for k in self.raw["kinds"]:
            if k["target"] == tgt and k["operation"] == o.kind and k.get("tier", o.tier) == o.tier and \
                    ("value" not in k or k["value"] == o.value):
                return str(k["kind"])
        raise DatasetError(f"override {o.override_id}: no public kind for target {o.target} operation {o.kind}")

    def marked(self, s: str) -> bool:
        return any(m.search(s) for m in self.markers)

    def note(self, o: Override, kind: str) -> str:
        model = (o.author or "").removeprefix("ai:")
        fb = str(self.raw["fallback_note"][kind]).format(model=model)
        if o.override_id in (self.raw.get("note_overrides") or {}):
            return str(self.raw["note_overrides"][o.override_id])
        if kind in self.raw.get("always_fallback", []) or not o.note:
            return fb
        s = o.note
        for a, b in (self.raw.get("note_replacements") or {}).items():
            s = s.replace(a, b)
        # parentheticals with a marker go first, then clauses (split at ';' and sentence ends outside parentheses);
        # a clause with a marker keeps what precedes its last ': ' before the marker, when that is a phrase
        s = re.sub(r"\s*\([^()]*\)", lambda mm: "" if self.marked(mm.group(0)) else mm.group(0), s)
        kept = []
        for sent in _split_top(s, sentence=True):
            clauses = []
            for c in _split_top(sent, sentence=False):
                c = c.strip()
                if not c:
                    continue
                if self.marked(c):
                    first = min(mm.start() for mk in self.markers for mm in [mk.search(c)] if mm is not None)
                    cut = c.rfind(": ", 0, first)
                    c = c[:cut] if cut >= 12 else ""
                if c and not self.marked(c):
                    clauses.append(c.rstrip("."))
            if clauses:
                kept.append("; ".join(clauses))
        if not kept or len(kept[0]) < 12:
            return fb
        out = kept[0].rstrip(" ,:;")
        return out + ("" if out.endswith((".", "?", "!")) else ".")


def _split_top(s: str, *, sentence: bool) -> list[str]:
    """``s`` split at sentence ends (``. `` before a capital or digit) or at ``;``, outside parentheses only."""
    out, depth, cur, i = [], 0, "", 0
    while i < len(s):
        ch = s[i]
        depth += ch == "("
        depth -= ch == ")" and depth > 0
        if depth == 0 and not sentence and ch == ";":
            out.append(cur)
            cur = ""
        elif depth == 0 and sentence and ch in ".!?" and i + 2 < len(s) and s[i + 1] == " " and \
                (s[i + 2].isupper() or s[i + 2].isdigit()):
            out.append(cur + ch)
            cur = ""
            i += 1
        else:
            cur += ch
        i += 1
    out.append(cur)
    return [x for x in out if x.strip()]


def overrides_value(p: Projection, o: Override) -> tuple[Any, list[dict[str, Any]]]:
    """An override's value as shipped: an annotation row override's payload projected like the row's payload (cut and
    scrubbed per the variant, cuts listed); a manual source's bibliographic fields; else the value as is."""
    if o.target in REGISTRY and o.kind == "row":
        kind = next((k for k, s in p.m.kinds.items() if s.workflow == o.target), None)
        if kind is None:
            return None, []
        spec = p.m.kinds[kind]
        payload = project_payload(p.internal_payload(kind, o.value), spec.ptype, spec.names)  # org fields NULL
        ann = {kind: {"metadata": {}, "payload": payload}}
        ctx: dict[str, Any] = {"cuts": [], "quote_chars": 0}
        p.cap_and_scrub(ann, ctx)
        cuts = [{"path": c["path"].replace(f"annotations.{kind}.payload", "value", 1), "chars": c["chars"]}
                for c in ctx["cuts"]]
        return ann[kind]["payload"], cuts
    if o.target == "manual_sources":
        return {k: v for k, v in o.payload.items() if k != "note"}, []
    return o.value, []


@dataclass(frozen=True)
class ActiveOverrides:
    """The overrides this release ships (:func:`active_overrides`): each row with its run, and what was left out."""

    rows: list[tuple[Override, str]]  # (row, run id of the chain part that added it), chain order
    chain_rows: int  # every row of the applied chain, revoke rows included
    revoke_rows: int
    revoked: list[str]  # rows a revoke retired
    superseded: list[str]  # rows another row of their group outranks (stack.overrides.winners)
    stale: list[str]  # <wf> rows of a document whose build record does not use them (scope no longer matched)


def active_overrides(inp: Inputs) -> ActiveOverrides:
    """The overrides whose value is what this release ships (D725): the effective rule of :mod:`stack.overrides` over
    the applied chain (revokes applied, then per group the winning row; revoke rows themselves are not values), minus
    the ``<wf>`` rows a document's build record does not list although the document was annotated (their scope no
    longer matched, so the stage's own value stands). A revoke the effective rule ignores (dangling, of a revoke,
    under-tier) refuses the export: ``overrides.add`` refuses all three, so the chain was written some other way."""
    run_of: dict[str, str] = {}
    rows: list[Override] = []
    for _p, h, part in inp.chain:
        for o in part:
            run_of[o.override_id] = h["run_id"]
            rows.append(o)
    live, warnings = drop_revoked(rows)
    if warnings:
        raise DatasetError("overrides chain: revokes the effective rule ignores: " + "; ".join(warnings[:3]))
    win = {o.override_id for o in winners(live)}
    stale = []
    for o in live:
        if o.override_id in win and o.target in REGISTRY:
            used = inp.families.get(o.target, {}).get(o.key)
            if used is not None and o.override_id not in used[0]:
                stale.append(o.override_id)
    keep = win - set(stale)
    live_ids = {o.override_id for o in live}
    return ActiveOverrides([(o, run_of[o.override_id]) for o in rows if o.override_id in keep], len(rows),
                           sum(o.kind == "revoke" for o in rows),
                           [o.override_id for o in rows if o.kind != "revoke" and o.override_id not in live_ids],
                           [o.override_id for o in live if o.override_id not in win], stale)


def export_overrides(p: Projection, policy: OverridesPolicy, act: ActiveOverrides) -> list[dict[str, Any]]:
    """``config/overrides.jsonl``: one line per active override (:data:`OVERRIDES_FIELDS`), chain order."""
    recs = []
    for o, run_id in act.rows:
        kind = policy.kind(o)
        author, method = policy.author(o)
        note = policy.note(o, kind)
        if policy.marked(note) or len(note) > int(policy.raw["max_note_chars"]):
            raise DatasetError(f"override {o.override_id}: public note {note!r} keeps an internal marker or is long")
        value, cuts = overrides_value(p, o)
        target = next((k for k, s in p.m.kinds.items() if s.workflow == o.target), o.target)
        recs.append({"override_id": o.override_id, "run_id": run_id, "kind": kind, "target": target,
                     "operation": o.kind, "key": o.key, "item_key": o.item_key, "field": o.field, "value": value,
                     "cuts": cuts, "note": note, "author": author, "method": method,
                     "created_at": o.created_at.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")})
    return recs


#: The fields of a line of ``config/overrides.jsonl`` (FORMAT's table; every line has exactly these).
OVERRIDES_FIELDS: dict[str, str] = {
    "override_id": "Id; referenced by annotations.<kind>.metadata.override_ids, identity.override_ids and "
                   "identity.members[].override_id, and listed in the override_ids of an override:<ids> config.",
    "run_id": "The pipeline run that added the row to the overrides chain.",
    "kind": "What the row is: scope_decision, stand_in_annotation, identity_merge, metadata_fix, manual_source, ...",
    "target": "What it changes: an annotation kind, identity, fields or manual_sources.",
    "operation": "row (a whole payload or record), field (one field), quarantine.",
    "key": "What the row applies to: a doc_id; for manual_source `<key_kind>=<key_value>` of the added record; for "
           "identity_merge a JSON array (as text) of the two doc_ids merged.",
    "item_key": "Item within the document, when any.",
    "field": "The field set by a field operation.",
    "value": "The value set (a payload is projected and capped like the corpus).",
    "cuts": "Quotes in value this release cut (as release.cuts).",
    "note": "One plain sentence on what the row does and why.",
    "author": "Who decided: maintainers, or the model id of a model-made row.",
    "method": "How the decision was prepared and approved.",
    "created_at": "When it entered the overrides chain (UTC).",
}


# ---------------------------------------------------------------- JSON Schema from arrow
def json_schema(t: pa.DataType, nullable: bool, desc: str | None = None,
                enums: Mapping[str, tuple[list[str], bool]] | None = None, path: str = "") -> dict[str, Any]:
    def typ(base: str) -> Any:
        return [base, "null"] if nullable else base

    if pa.types.is_struct(t):
        out: dict[str, Any] = {"type": typ("object"), "additionalProperties": False, "properties": {},
                               "required": [f.name for f in t]}
        for f in t:
            d = (f.metadata or {}).get(b"description")
            out["properties"][f.name] = json_schema(f.type, f.nullable, d.decode() if d else None, enums,
                                                    f"{path}.{f.name}" if path else f.name)
    elif pa.types.is_list(t):
        out = {"type": typ("array"), "items": json_schema(t.value_type, t.value_field.nullable, None, enums, path + "[]")}
    elif pa.types.is_string(t) or pa.types.is_large_string(t):
        out = {"type": typ("string")}
    elif pa.types.is_boolean(t):
        out = {"type": typ("boolean")}
    elif pa.types.is_integer(t):
        out = {"type": typ("integer")}
    elif pa.types.is_floating(t):
        out = {"type": typ("number")}
    elif pa.types.is_date(t):
        out = {"type": typ("string"), "format": "date"}
    elif pa.types.is_timestamp(t):
        out = {"type": typ("string"), "format": "date-time"}
    else:
        raise DatasetError(f"json_schema: no mapping for {t}")
    if desc:
        out["description"] = desc
    if enums is not None:
        star = re.sub(r"^annotations\.[^.]+\.metadata\.", "annotations.*.metadata.", path)
        hit = enums.get(path) or enums.get(path.removesuffix("[]")) or enums.get(star)
        if hit is not None and not hit[1]:
            vals: list[Any] = list(hit[0])
            out_t = out if "items" not in out else out["items"]
            out_t["enum"] = vals + ([None] if nullable else [])
    return out


def _meta_enums(m: Mapping_, prefix: str) -> dict[str, tuple[list[str], bool]]:
    """The mapping's enums keyed by paths relative to ``prefix`` (``annotations.*.metadata`` paths included)."""
    out = {}
    for p, v in m.enums.items():
        p2 = p.replace("annotations.*.metadata", "metadata")
        if prefix and p2.startswith(prefix + "."):
            out[p2[len(prefix) + 1:]] = v
        elif not prefix:
            out[p] = v
    return out


# ---------------------------------------------------------------- writing
def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _write_text(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, "utf-8")


def _jsonl(rows: Iterable[Mapping[str, Any]]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=False, default=_json_default) + "\n" for r in rows)


def _json_default(v: Any) -> Any:
    if isinstance(v, dt.datetime):
        return v.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ").replace(".000000Z", "Z")
    if isinstance(v, dt.date):
        return v.isoformat()
    raise TypeError(f"not JSON-serialisable: {type(v).__name__}")


#: Parallel downloads of the inline variant's texts into a mirror (one GET each, ~0.2 s apart when serial).
PREFETCH_WORKERS = 16


def prefetch_texts(store_root: Path, digests: Iterable[str | None]) -> int:
    """Download the text blobs of ``digests`` an S3 store's mirror lacks, :data:`PREFETCH_WORKERS` at a time (each
    sha256-checked by :meth:`stack.store.blobs.BlobStore.path`); returns how many were fetched. A local-only store,
    or a mirror that holds them all, fetches nothing. Serially, a fresh mirror took ~2.4 h for 39k texts."""
    from concurrent.futures import ThreadPoolExecutor

    from stack.store.blobs import open_blobs

    blobs = open_blobs(store_root)
    if blobs.store.remote is None:
        return 0
    todo = sorted({d for d in digests if d and not blobs.local(d).is_file()})
    with ThreadPoolExecutor(PREFETCH_WORKERS) as ex:
        for _ in ex.map(blobs.path, todo):
            pass
    return len(todo)


def _text_source(store_root: Path, blobs_dir: Path | None) -> Callable[[str], bytes]:
    if blobs_dir is not None:
        def get(d: str) -> bytes:
            p = blobs_dir / d
            if not p.is_file():
                raise DatasetError(f"text {d[:12]}…: not in {blobs_dir.name}/")
            return p.read_bytes()
        return get
    from stack.store.blobs import open_blobs

    blobs = open_blobs(store_root)
    return blobs.get


#: FORMAT's DuckDB macros over the release's view ``d`` (the pipeline's ``stack.query`` macros of the same names,
#: docs/CONSUME.md, adapted: a release record keeps status and config_id under ``metadata``), and two table macros, the
#: long technique and model tables (D759). ``name`` must be a string literal.
RELEASE_MACROS = """\
-- a kind's status, its verdict when ok (screen, stack_relevance, shallow_review_relevance, text_usability), and
-- the record only when it was made under a given config_id (config/configs.json)
CREATE OR REPLACE MACRO ann_status(a, name) AS struct_extract(a, name).metadata.status;
CREATE OR REPLACE MACRO ann_ok(a, name) AS coalesce(struct_extract(a, name).metadata.status = 'ok', false);
CREATE OR REPLACE MACRO ann_verdict(a, name) AS
    CASE WHEN struct_extract(a, name).metadata.status = 'ok' THEN struct_extract(a, name).payload.verdict END;
CREATE OR REPLACE MACRO ann_pinned(a, name, config_id) AS
    CASE WHEN struct_extract(a, name).metadata.config_id = config_id THEN struct_extract(a, name) END;
-- one row per (document, catalogue technique), with the first quote and whether any quote was found
CREATE OR REPLACE MACRO technique_rows() AS TABLE
    SELECT d.doc_id, t.slug, t.roles, t.flags, list_contains(t.flags, 'quote_unverified') AS quote_unverified,
           t.quotes[1].text AS first_quote, d.annotations.safety_techniques.payload.doc_kind AS doc_kind,
           d.annotations.safety_techniques.metadata.config_id AS config_id
    FROM d, unnest(d.annotations.safety_techniques.payload.techniques) AS u(t);
-- one row per (document, catalogue model); count a model as present where roles.mentioned >= 1
CREATE OR REPLACE MACRO model_rows() AS TABLE
    SELECT d.doc_id, m.node_id, m.roles, d.annotations.model_roles.metadata.config_id AS config_id
    FROM d, unnest(d.annotations.model_roles.payload.models) AS u(m);"""
#: Example queries over :data:`RELEASE_MACROS` (FORMAT prints them; the tests run them).
RELEASE_MACRO_EXAMPLES = """\
SELECT doc_id, ann_verdict(annotations, 'stack_relevance') AS stack,
       ann_verdict(annotations, 'shallow_review_relevance') AS shallow_review
FROM d WHERE ann_ok(annotations, 'screen');
-- documents per technique in own use (strength >= 2), and how many of those rows have no quote found
SELECT slug, count(*) AS documents, count(*) FILTER (quote_unverified) AS quote_unverified
FROM technique_rows() WHERE roles.used >= 2 GROUP BY ALL ORDER BY documents DESC;
-- techniques and models of one document
SELECT slug, roles FROM technique_rows() WHERE doc_id = 'ssepg4p9vmv5w';
SELECT node_id, roles FROM model_rows() WHERE doc_id = 'ssepg4p9vmv5w' AND roles.mentioned >= 1;"""


def records_recipe_sql(kinds: Sequence[str]) -> str:
    """FORMAT's recipe for one long table of every kind's metadata (one row per document and kind; a kind not run
    has no row), over the view ``d`` of the corpus. A table, not a view: DuckDB 2.0.0 fails with an internal error
    when a filter on such a view is pushed into the union's struct unnests."""
    parts = [f"SELECT doc_id, unnest(annotations.{k}.metadata) FROM d WHERE annotations.{k} IS NOT NULL" for k in kinds]
    return "CREATE TABLE records AS\n" + "\nUNION ALL ".join(parts) + ";"


@dataclass(frozen=True)
class DatasetResult:
    path: Path
    release_json: dict[str, Any]
    problems: list[str]


def run_dataset_export(store_root: str | Path, corpus_key: str, config: str | Path, out: str | Path, *,
                       release_id: str | None = None, blobs_dir: str | Path | None = None,
                       allow_incomplete: bool = False, now: dt.datetime | None = None) -> DatasetResult:
    """``stack export --config dataset-<variant>``: write one variant of the dataset release to ``out`` (module
    docstring). Refuses (nothing kept) on any check problem."""
    root = Path(store_root)
    cfg = load_dataset_config(config)
    m = Mapping_(cfg.path(cfg.mapping))
    check_transforms(m)
    when = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    rid = release_id or when.date().isoformat()  # the release id is the export's UTC date (format id = release id)
    dest = check_out_dir(out, root)
    tbl, h, _cpath = corpus_input(root, corpus_key, fold_config="build")
    bid, status = release_gate(root, h["file_id"], allow_incomplete=allow_incomplete)
    inp = load_inputs(root, bid, status, h)
    rescored, tvs = release_quality(root, inp.build, tbl, cfg.judge_eval)  # D728: from the build's own tables
    variant = m.variants[cfg.variant]
    proj = Projection(m, inp, cfg.variant, rid, variant.get("quote_cap_chars"),
                      _text_source(root, Path(blobs_dir) if blobs_dir else None) if variant["texts"] == "inline"
                      else None)
    picked = filter_rows(tbl, resolve_filter(m.raw["rows"]["filter"]))
    rows_in, filtered_out = tbl.num_rows, tbl.num_rows - picked.num_rows
    del tbl
    if variant["texts"] == "inline" and not blobs_dir:
        prefetch_texts(root, picked.column("content").combine_chunks().field("current_text_sha256").to_pylist())
    stage = dest.parent / f".{dest.name}.{os.getpid()}.tmp"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "data").mkdir(parents=True)
    dropped: Counter[str] = Counter()
    schema = m.schema.with_metadata({b"stack.format": rid.encode(), b"release_id": rid.encode(),
                                     b"variant": cfg.variant.encode(),
                                     b"quote_cap_chars": str(proj.cap).encode()})
    n_out = 0
    groups: list[int] = []
    try:
        with pq.ParquetWriter(stage / "data" / "corpus.parquet", schema, compression="zstd") as w:
            buf: list[dict[str, Any]] = []
            buf_bytes = 0

            def flush() -> None:
                nonlocal buf, buf_bytes
                if buf:
                    w.write_table(pa.Table.from_pylist(buf, schema=schema), row_group_size=len(buf))
                    groups.append(len(buf))
                buf, buf_bytes = [], 0

            for batch in picked.to_batches(max_chunksize=BATCH_ROWS):
                for r in batch.to_pylist():
                    why = row_drop_reason(r)
                    if why:
                        dropped[why] += 1
                        continue
                    out_row = proj.row(r)
                    if variant["abstract"] != "keep":
                        out_row["abstract"] = None
                        withhold_abstract_paths(out_row, m.abstract_paths)
                    body = out_row["content"]["text"]["body"]
                    buf.append(out_row)
                    buf_bytes += len(body.encode("utf-8")) if body else 0
                    n_out += 1
                    if len(buf) >= ROW_GROUP_ROWS or buf_bytes >= ROW_GROUP_TEXT_BYTES:
                        flush()
            flush()
        del picked
        if proj.problems:
            raise DatasetError(f"{len(proj.problems)} row problem(s): " + "; ".join(proj.problems[:5]))
        policy = OverridesPolicy(cfg.path(cfg.overrides_public))
        act = active_overrides(inp)
        ov_rows = export_overrides(proj, policy, act)
        counts: dict[str, int] = {"data/corpus.parquet": n_out}
        _write_text(stage / OVERRIDES_FILE, _jsonl(ov_rows))
        counts[OVERRIDES_FILE] = len(ov_rows)
        cfg_rows = [r.__dict__ for r in sorted(proj.configs.values(), key=lambda c: c.config_id)]
        pa.Table.from_pylist(cfg_rows, schema=CONFIGS_SCHEMA)  # the rows have the documented types
        _write_text(stage / CONFIGS_FILE, json.dumps(cfg_rows, indent=2, ensure_ascii=False) + "\n")
        counts[CONFIGS_FILE] = len(cfg_rows)
        taxonomies = write_taxonomies(stage, proj)
        write_row_schema(stage, m)
        observed = write_vocab(stage, m, cfg)
        tdir = cfg.path(cfg.templates)
        shutil.copyfile(tdir / "LICENSE", stage / "LICENSE")
        jev = judge_eval(cfg.judge_eval, bid, rescored)
        doc: dict[str, Any] = {
            "release_id": rid, "format": rid, "variant": cfg.variant, "hf_repo": cfg.hf_repo,
            "created_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "parent_release": None,
            "build": {"build_id": bid, "status": inp.build_status.get("status"), "plan": inp.build.get("plan"),
                      "corpus_file_id": h["file_id"], "corpus_code_digest": h.get("code_digest"),
                      "release_code_commit": _git_commit()},
            "rows": {"in": rows_in, "filtered_out": filtered_out, "dropped": dict(sorted(dropped.items())),
                     "out": n_out},
            "taxonomies": taxonomies,
            "stages": stages_record(proj),
            "orgs": orgs_record(proj),
            "configs": {"path": CONFIGS_FILE, "count": len(cfg_rows)},
            "annotations": {k: dict(sorted(v.items())) for k, v in proj.counts.items()},
            "overrides": {"head": inp.overrides_head, "export_policy": policy.raw["export_policy"],
                          "path": OVERRIDES_FILE, "rows": len(ov_rows),
                          "chain": {"rows": act.chain_rows, "revoke_rows": act.revoke_rows,
                                    "revoked": len(act.revoked), "superseded": len(act.superseded),
                                    "stale": len(act.stale)},
                          "runs": [{"run_id": h2["run_id"], "rows": sum(r["run_id"] == h2["run_id"] for r in ov_rows)}
                                   for _p, h2, _r in inp.chain],
                          "kinds": dict(Counter(r["kind"] for r in ov_rows).most_common()),
                          "applied": _applied(inp)},
            "remap": {"values": len(proj.remap), "by_rule": dict(Counter(r["rule"] for r in proj.remap))},
            "policy": {"abstract": "withheld_pending_rights_review" if variant["abstract"] != "keep" else "shipped",
                       "text": variant["texts"], "quote_cap_chars": proj.cap, "quote_cut": m.raw["quote_cut"],
                       "quote_paths": m.quote_paths, "surface_paths": m.surface_paths, "prose_paths": m.prose_paths,
                       "abstract_paths": m.abstract_paths,
                       "prose_scrub": [s["path"] for s in m.prose_scrub], "cuts": proj.cuts_total,
                       "prose_scrubbed": proj.prose_scrubbed, "url_fields": list(URL_FIELDS),
                       "urls_scrubbed": proj.urls_scrubbed,
                       "corpus_row_groups": {"count": len(groups), "max_rows": ROW_GROUP_ROWS,
                                             "max_text_bytes": ROW_GROUP_TEXT_BYTES,
                                             "rows_min": min(groups, default=0), "rows_max": max(groups, default=0)}},
            "quality": {"judge_eval": jev.record, "triage_vs_screen": tvs},
            "licence": LICENCE, "contact": CONTACT,
        }
        stats = release_stats(stage, m, doc)
        stats["overrides_summary"] = overrides_summary(ov_rows)
        doc["stand_ins"] = {"documents": stats["standin_docs"], "annotations": stats["standins"]}
        _write_text(stage / "FORMAT.md", format_md(m, cfg, doc, tdir, counts, stage, stats, observed, ov_rows))
        _write_text(stage / "README.md", card_md(m, cfg, doc, tdir, stats, stage, counts, jev))
        files = []
        for p in sorted(stage.rglob("*")):
            if p.is_file() and p.name != "release.json":
                rel = p.relative_to(stage).as_posix()
                e: dict[str, Any] = {"path": rel, "bytes": p.stat().st_size, "sha256": _sha256_file(p)}
                if rel in counts:
                    e["rows"] = counts[rel]
                files.append(e)
        doc["files"] = files
        _write_text(stage / "release.json", json.dumps(doc, indent=2, ensure_ascii=False, default=_json_default) + "\n")
        problems = check_bundle(stage, m, cfg)
        if problems:
            raise DatasetError(f"{len(problems)} bundle check problem(s), nothing kept: " + "; ".join(problems[:8]))
        check_out_dir(dest, root)
        if dest.exists():
            dest.rmdir()
        os.rename(stage, dest)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return DatasetResult(dest, doc, [])


def _applied(inp: Inputs) -> dict[str, int]:
    out: Counter[str] = Counter()
    for wf, per in inp.families.items():
        for ids, _n in per.values():
            if ids:
                out[wf] += 1
    return dict(sorted(out.items()))


def version_key(v: str) -> tuple[int, ...]:
    """A dotted version as numbers (``1.10.0`` after ``1.9.0``)."""
    return tuple(int(x) for x in re.findall(r"\d+", v))


def write_taxonomies(stage: Path, proj: Projection) -> dict[str, list[dict[str, Any]]]:
    """Copy one file per (name, version) the shipped configs and the build's own configs pin (module docstring,
    Bundle); ``release.json`` ``taxonomies``: per name its versions, oldest first. Two sha256 for one name and version
    are refused (one bundle path cannot hold both)."""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for t in [c.taxonomy for c in proj.configs.values() if c.taxonomy] + proj.build_taxonomies():
        key = (t["name"], t["version"])
        have = found.setdefault(key, t)
        if have["sha256"] != t["sha256"]:
            raise DatasetError(f"taxonomies: {t['name']} {t['version']} pinned with two sha256 ({have['sha256'][:12]}…, "
                               f"{t['sha256'][:12]}…): one bundle file per name and version")
    out: dict[str, list[dict[str, Any]]] = {}
    for (name, ver), t in sorted(found.items(), key=lambda kv: (kv[0][0], version_key(kv[0][1]))):
        dst = stage / t["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / proj.taxonomy_src[t["sha256"]], dst)
        if _sha256_file(dst) != t["sha256"]:
            raise DatasetError(f"taxonomy {t['path']}: copied bytes differ from the pinned sha256")
        out.setdefault(name, []).append({"version": ver, "sha256": t["sha256"], "path": t["path"]})
    return out


def newest_taxonomy(doc: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    """The newest bundled version of taxonomy ``name`` in ``release.json`` ``taxonomies`` (a list per name; a release
    before D758 had one entry, a mapping), or ``None``."""
    t = (doc.get("taxonomies") or {}).get(name)
    if isinstance(t, list):
        return t[-1] if t else None
    return t or None


#: The layout of a taxonomy file before the catalogue envelope (``version``, ``meta``, one list ``entries``): techniques
#: 1.0.0 and models 1.0.0, shipped beside their envelope versions for rows accepted from configs that pinned them (D758);
#: FORMAT's sentence for each (:func:`taxonomy_layouts`).
EARLIER_LAYOUT = {
    "techniques": ("techniques", "the older layout: top-level lists `techniques` and `group_nodes`, each entry keyed by "
                                 "`slug` (the envelope's `id`), with no `node` field"),
    "models": ("nodes", "the older layout: top-level `nodes`, a mapping from node id (the envelope's `id`) to the node, "
                        "whose `maker` is a maker id that `meta.makers` names"),
}


def taxonomy_layout(name: str, data: Mapping[str, Any], path: str) -> str:
    """``envelope`` (a top-level ``entries`` list) or ``earlier`` (:data:`EARLIER_LAYOUT`); any other shape refused."""
    if isinstance(data.get("entries"), list):
        return "envelope"
    key = EARLIER_LAYOUT.get(name, ("", ""))[0]
    if key and data.get(key):
        return "earlier"
    raise DatasetError(f"{path}: neither the catalogue envelope (`entries`) nor the earlier {name} layout")


def taxonomy_sizes(name: str, data: Mapping[str, Any], path: str) -> dict[str, int]:
    """The card's counts of a taxonomy file in either layout (:func:`taxonomy_layout`): ``techniques`` and ``groups``
    (techniques), ``models`` (every model node: model, family and group); a zero count is refused (the card would
    say "0 techniques")."""
    env = taxonomy_layout(name, data, path) == "envelope"
    if name == "techniques":
        if env:
            nodes = Counter(e.get("node") for e in data["entries"])
            out = {"techniques": nodes["technique"], "groups": nodes["group"]}
        else:
            out = {"techniques": len(data.get("techniques") or []), "groups": len(data.get("group_nodes") or [])}
    elif name == "models":
        out = {"models": len(data["entries"] if env else data["nodes"])}
    else:
        raise DatasetError(f"{path}: the card counts no taxonomy {name!r}")
    zero = [k for k, v in out.items() if not v]
    if zero:
        raise DatasetError(f"{path}: the card would count 0 {', '.join(zero)} ({name} file read as "
                           f"{'the envelope' if env else 'the earlier layout'})")
    return out


def taxonomy_layouts(doc: Mapping[str, Any], stage: Path) -> str:
    """FORMAT's paragraph on the shipped taxonomy files (``release.json`` ``taxonomies``): how many, that the newest of
    each name is in the catalogue envelope (an earlier-layout newest file is refused), and the layout of each earlier
    version shipped beside it and why (D758)."""
    files, older = [], []
    for name, ts in (doc.get("taxonomies") or {}).items():
        ts = ts if isinstance(ts, list) else [ts]
        for i, t in enumerate(ts):
            files.append(f"`{t['path']}`")
            lay = taxonomy_layout(name, yamlio.load((stage / t["path"]).read_text("utf-8")) or {}, t["path"])
            if i == len(ts) - 1 and lay != "envelope":
                raise DatasetError(f"{t['path']}: the newest {name} file is not in the catalogue envelope")
            if i < len(ts) - 1:
                older.append(f"`{t['path']}` is in " + EARLIER_LAYOUT[name][1] if lay == "earlier" else
                             f"`{t['path']}` is in the catalogue envelope too")
    n = len(files)
    out = (f"This release ships {n} taxonomy file{'s' if n != 1 else ''} ({', '.join(files)}). The newest version of "
           "each vocabulary is in the catalogue envelope: `version`; `meta` with `schema` (the JSON Schema it follows, "
           "in the taxonomies repository), `status`, `updated`, `description`; and one list `entries`, each entry with "
           "`id`, `name` and the vocabulary's own fields.")
    if older:
        out += (" An earlier version ships beside the newest where rows carry annotations made under an earlier config "
                "that pinned it and accepted for the current config (listed under \"Annotations kept from an earlier config\" below; "
                "`config/configs.json` `taxonomy` names each config's file): " + "; ".join(older) + ".")
    return out


def stages_record(proj: Projection) -> dict[str, dict[str, Any]]:
    """``release.json`` ``stages`` (module docstring, Bundle; D758): per annotate stage of the build, ``selection_digest``,
    ``target`` ``{config, name, config_id, documents}`` and ``accepted`` (the plan line's entries in preference order,
    each with ``config_id`` and ``documents``): ``documents`` = the shipped documents whose kind of that stage (the
    workflow's own payload) is under the config; ``config_id`` the id most of them carry (a config whose rows are
    partly computed has a model id and a ``rule`` id; ``config_ids`` gives each id's documents, most first), null for a
    config no shipped row names."""
    ids: dict[str, list[str]] = {}
    for c in proj.configs.values():
        ids.setdefault(c.config_digest, []).append(c.config_id)

    def named(digest: str) -> dict[str, Any]:
        by = sorted(((proj.config_id_docs[i], i) for i in ids.get(digest, [])), key=lambda x: (-x[0], x[1]))
        return {"config_id": by[0][1] if by else None, "config_ids": {i: n for n, i in by},
                "documents": proj.config_docs[digest]}

    out: dict[str, dict[str, Any]] = {}
    for stage, s in sorted(proj.inp.stages.items()):
        sel, target = s["selection"], s["selection"]["target"]
        out[stage] = {"selection_digest": sel["digest"],
                      "target": {"config": target, "name": s["name"], **named(target)},
                      "accepted": [{**e, **named(e["config"])} for e in sel.get("accepted") or []]}
    return out


def accepted_sentences(doc: Mapping[str, Any]) -> str:
    """FORMAT's sentence per accepted config (``release.json`` ``stages``), or the sentence that there is none."""
    lines = []
    for stage, s in (doc.get("stages") or {}).items():
        t = s["target"]
        for e in s["accepted"]:
            lines.append(f"- `{stage}`: {_fmt(e['documents'])} documents carry an annotation of `{e['name']}` "
                         f"(`{e['config_id'] or e['config'][:8]}`), kept for the target `{t['name']}` "
                         f"(`{t['config'][:8]}`, {_fmt(t['documents'])} documents) by {e['decision']}. "
                         + (e.get("note") or "").strip() + "\n")
    if not lines:
        return ("No annotation in this release was kept from an earlier config: every one was judged under its "
                "stage's target config (`release.json` `stages`).\n")
    return ("Annotations kept from an earlier config (`release.json` `stages.<stage>.accepted`): a config change "
            "declared not to change the answers, so the earlier, decided answers stand, each labelled with the config "
            "that produced it (its `config_id`, and so its taxonomy version in `config/configs.json`).\n\n"
            + "".join(lines))


def orgs_record(proj: Projection) -> dict[str, Any] | None:
    """``release.json`` ``orgs``: the shipped affiliations by ``org_status`` (``no_record``: the document has no
    ``affiliation_orgs`` record) and how many name an organisation."""
    if proj.m.org_kind is None:
        return None
    c = proj.org_counts
    keys = (*orgvocab.STATUSES, "no_record")
    return {"kind": proj.m.org_kind, "from": proj.m.org_from, "affiliations": sum(c[k] for k in keys),
            "with_org_ids": c["with_org_ids"], "by_status": {k: c[k] for k in keys}}


def row_json_schema(m: Mapping_) -> dict[str, Any]:
    """``schema.json``: the JSON Schema of one row, self-contained (every kind's record and payload inline, no
    ``$ref``); a kind's record is ``properties.annotations.properties.<kind>``, its payload that record's
    ``properties.payload``."""
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": ROW_SCHEMA_FILE,
            "title": "The Safety Stack: one document row", **json_schema(pa.struct(list(m.schema)), False, None, m.enums)}


def write_row_schema(stage: Path, m: Mapping_) -> None:
    _write_text(stage / ROW_SCHEMA_FILE, json.dumps(row_json_schema(m), indent=2, ensure_ascii=False) + "\n")


def kind_schemas(row_schema: Mapping[str, Any], kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """A kind's record and payload subschemas of ``schema.json`` (each with the draft named, for a validator)."""
    rec = dict(row_schema["properties"]["annotations"]["properties"][kind])
    pay = dict(rec["properties"]["payload"])
    draft = {"$schema": row_schema["$schema"]}
    return draft | rec, draft | pay


# ---------------------------------------------------------------- what occurs in the release (vocab, card, FORMAT)
def _fmt(n: Any) -> str:
    return f"{n:,}" if isinstance(n, int) else str(n)


def values_sql(schema: pa.Schema, path: str) -> str | None:
    """A SELECT of the distinct values (as text) at vocab ``path`` over view ``d`` (``a[].b``: list levels; a list leaf
    is unnested); None when the path names no field of ``schema``."""
    parts = path.split(".")
    t: pa.DataType = pa.struct(list(schema))
    expr: str | None = None
    froms = ["d"]
    for i, part in enumerate(parts):
        name = part.removesuffix("[]")
        if not pa.types.is_struct(t) or t.get_field_index(name) < 0:
            return None
        t = t.field(name).type
        expr = name if expr is None else f"{expr}.{name}"
        if pa.types.is_list(t) and (part.endswith("[]") or i == len(parts) - 1):
            alias = f"x{i}"
            froms.append(f"unnest({expr}) AS u{i}({alias})")
            expr, t = alias, t.value_type
    return f"SELECT DISTINCT CAST({expr} AS VARCHAR) FROM {', '.join(froms)}"


def observed_values(corpus: Path, schema: pa.Schema, kinds: Sequence[str], paths: Iterable[str]) -> dict[str, set[str]]:
    """The values that occur in the written corpus at each vocab/enum path (``annotations.*.metadata.<f>`` = over every
    kind). Refuses a path that names no field (a vocab entry for nothing)."""
    con = duck.connect()
    con.execute("SET memory_limit='2GB'; SET threads=2")
    cols = [f.name for f in schema if not (f.name == "content")]
    con.execute(f"CREATE VIEW d AS SELECT {', '.join(cols)}, struct_pack(source_id := content.source_id, "
                f"role := content.role, quality := content.quality) AS content FROM read_parquet('{corpus}')")
    lite = pa.schema([f for f in schema if f.name != "content"] + [pa.field("content", pa.struct([
        schema.field("content").type.field(n) for n in ("source_id", "role", "quality")]))])
    out: dict[str, set[str]] = {}
    for p in paths:
        subs = [p.replace("annotations.*.", f"annotations.{k}.") for k in kinds] if "annotations.*." in p else [p]
        got: set[str] = set()
        for q in subs:
            sql = values_sql(lite, q)
            if sql is None:
                raise DatasetError(f"vocab.yaml: {p} names no field of the release")
            got |= {r[0] for r in con.execute(sql).fetchall() if r[0] is not None}
        out[p] = got
    return out


def _form_regex(form: str) -> re.Pattern[str]:
    """A pattern form (``menu_gap:<surface>``) as a regex over values."""
    return re.compile("".join(".+" if part.startswith("<") else re.escape(part)
                              for part in re.split(r"(<[^>]+>)", form)) + r"\Z")


def shipped_vocab(vocab: Mapping[str, Any], observed: Mapping[str, set[str]]) -> dict[str, Any]:
    """vocab.yaml as shipped: every field and pattern keeps only the values that occur in the release; the others are
    listed by name under ``reserved`` (defined, not used here)."""
    out = copy.deepcopy(dict(vocab))
    for sect in ("fields", "patterns"):
        for path, e in (out.get(sect) or {}).items():
            seen = observed.get(path, set())
            vals = e.get("values") or {}
            keep, reserved = {}, []
            for k, v in vals.items():
                hit = (str(k) in seen) if sect == "fields" else any(_form_regex(str(k)).match(x) for x in seen)
                if hit:
                    keep[k] = v
                else:
                    reserved.append(str(k))
            e["values"] = keep
            if reserved:
                e["reserved"] = reserved
    return out


#: The year the paper collectors' searches start; earlier documents come from forums, blog archives and manual sources.
EARLY_BEFORE = 2015
#: How the card and FORMAT name each public override kind (counts generated).
OVERRIDE_KIND_PHRASES = {"manual_source": "add a source by hand", "scope_decision": "set screen or relevance fields",
                         "stand_in_annotation": "are Claude stand-in annotations",
                         "identity_merge": "merge two records of one work", "identity_separation": "separate records",
                         "metadata_fix": "fix a bibliographic field", "annotation_replacement": "replace an annotation",
                         "quarantine": "withhold an annotation"}


def overrides_summary(ov_rows: Sequence[Mapping[str, Any]]) -> str:
    """One plain sentence on what the overrides did, with counts by kind (card and FORMAT)."""
    kinds = Counter(r["kind"] for r in ov_rows)
    total = sum(kinds.values())
    if not total:
        return "No overrides were applied to this release."
    keys = {k: len({r["key"] for r in ov_rows if r["kind"] == k}) for k in kinds}
    parts = [f"{_fmt(n)} {OVERRIDE_KIND_PHRASES.get(k, k)}" + (f" (of {_fmt(keys[k])} documents)" if keys[k] < n else "")
             for k, n in kinds.most_common()]
    return (f"Of the {_fmt(total)} override rows applied to this release, " + ", ".join(parts[:-1])
            + (" and " if len(parts) > 1 else "") + parts[-1] + ". Most of the maintainers' rows come from lists an "
            "AI assistant prepared and the maintainers approved as a whole, not row by row; each row's `method` "
            "says how it was prepared.")


def release_stats(stage: Path, m: Mapping_, doc: Mapping[str, Any]) -> dict[str, Any]:
    """Counts the card and FORMAT print, from the written corpus: coverage, languages, stand-ins, known
    inconsistencies, taxonomy sizes."""
    con = duck.connect()
    con.execute("SET memory_limit='2GB'; SET threads=2")
    con.execute(f"CREATE VIEW d AS SELECT * EXCLUDE (content), struct_pack(role := content.role) AS content "
                f"FROM read_parquet('{stage / 'data' / 'corpus.parquet'}')")
    def q(sql: str) -> list[Any]:
        return list(con.execute(sql).fetchall())

    st: dict[str, Any] = {"n_docs": q("SELECT count(*) FROM d")[0][0]}
    st["doc_types"] = q("SELECT coalesce(doc_type, 'unknown'), count(*) FROM d GROUP BY 1 ORDER BY 2 DESC")
    y = q("SELECT min(year(published_date)), max(year(published_date)), count(*) FILTER (published_date IS NULL) FROM d")[0]
    st["year_min"], st["year_max"], st["undated"] = y
    st["recent_from"] = int(doc["release_id"][:4]) - 1
    st["recent"] = q(f"SELECT count(*) FROM d WHERE year(published_date) >= {st['recent_from']}")[0][0]
    early = dict(q("SELECT CASE WHEN mm.channel IN ('lesswrong', 'alignmentforum') THEN 'forum' "
                   "WHEN mm.channel LIKE '%\\_blog' ESCAPE '\\' THEN 'blog' "
                   "WHEN mm.channel LIKE 'manual\\_%' ESCAPE '\\' OR mm.channel LIKE 'links\\_%' ESCAPE '\\' "
                   "THEN 'manual' ELSE 'other' END, count(*) FROM d, unnest(identity.members) AS u(mm) "
                   f"WHERE mm.source_id = identity.anchor_source_id AND year(published_date) < {EARLY_BEFORE} "
                   "GROUP BY 1"))
    st["early"] = {k: early.get(k, 0) for k in ("forum", "blog", "manual", "other")}
    st["channels"] = q("SELECT mm.channel, count(*) FROM d, unnest(identity.members) AS u(mm) "
                       "WHERE mm.source_id = identity.anchor_source_id GROUP BY 1 ORDER BY 2 DESC")
    st["languages"] = q("SELECT annotations.doc_class.payload.primary_lang, count(*) FROM d "
                        "WHERE annotations.doc_class.payload.primary_lang IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1")
    st["standin_docs"] = q("SELECT count(DISTINCT doc_id) FROM d WHERE " + " OR ".join(
        f"(annotations.{k}.metadata.provenance = 'override' AND annotations.{k}.metadata.annotator LIKE 'claude-%')"
        for k in m.kinds))[0][0]
    st["standins"] = q("SELECT count(*) FROM (" + " UNION ALL ".join(
        f"SELECT doc_id FROM d WHERE annotations.{k}.metadata.provenance = 'override' "
        f"AND annotations.{k}.metadata.annotator LIKE 'claude-%'" for k in m.kinds) + ")")[0][0]
    inc: list[tuple[str, str]] = []
    r = q("SELECT list(doc_id ORDER BY doc_id) FROM d WHERE annotations.screen.payload.verdict = 'relevant' "
          "AND annotations.stack_relevance.payload.verdict <> 'relevant' "
          "AND annotations.shallow_review_relevance.payload.verdict <> 'relevant'")[0][0] or []
    inc.append((f"{_fmt(len(r))} document(s)", "screen.verdict is `relevant` while neither question's verdict is "
                f"(the call-level rule says it should be the more inclusive of the two): {', '.join(r[:5])}"))
    r2 = q("SELECT count(*), count(DISTINCT doc_id) FROM d, unnest(annotations.model_roles.payload.models) AS u(mm) "
           "WHERE mm.roles.mentioned = 0 AND len(list_filter(annotations.model_roles.payload.flags, "
           "f -> split_part(f, ':', 2) = mm.node_id)) <> 1")[0]
    inc.append((f"{_fmt(r2[0])} model rows in {_fmt(r2[1])} documents", "an all-zero candidate row of model_roles "
                "carries no flag or several, though the rubric asks for exactly one"))
    r3 = q("SELECT count(*) FROM d WHERE annotations.model_roles.metadata.config_id "
           "<> annotations.model_name_matches.metadata.config_id")[0][0]
    inc.append((f"{_fmt(r3)} documents", "model_name_matches does not share model_roles' config_id and decided_at: an "
                "override (stand-in) replaced the model_roles answer and the matcher record keeps the original call's"))
    r5 = q("SELECT count(*), count(DISTINCT doc_id) FROM d, unnest(annotations.model_roles.payload.flags) AS u(f) "
           "WHERE NOT regexp_matches(f, '^(subsumed|bib_only|matcher_fp|not_found):')")[0]
    inc.append((f"{_fmt(r5[0])} flags in {_fmt(r5[1])} documents", "a model_roles document flag does not start with "
                "one of the documented prefixes `subsumed:`, `bib_only:`, `matcher_fp:`, `not_found:` (e.g. a role name "
                "or a bare word)"))
    r4 = q("SELECT count(*) FROM d WHERE content.role IS DISTINCT FROM annotations.screen.payload.basis_role")[0][0]
    inc.append((f"{_fmt(r4)} documents", "screen.basis_role differs from content.role: two snapshots converted to "
                "identical text; the text hash the screen judged is content.text.sha256 (checked by the build)"))
    st["inconsistencies"] = inc
    st["quote_unverified"] = q(
        "SELECT coalesce(d.annotations.safety_techniques.payload.doc_kind, 'none'), count(*), "
        "count(*) FILTER (list_contains(t.flags, 'quote_unverified')) "
        "FROM d, unnest(d.annotations.safety_techniques.payload.techniques) AS u(t) GROUP BY 1 ORDER BY 2 DESC")
    sizes: dict[str, int] = {}
    for name in ("techniques", "models"):
        t = newest_taxonomy(doc, name)
        if t is None:
            raise DatasetError(f"the card counts the {name} taxonomy, but the release ships none")
        sizes |= taxonomy_sizes(name, yamlio.load((stage / t["path"]).read_text("utf-8")) or {}, t["path"])
    st["n_techniques"], st["n_groups"], st["n_models"] = sizes["techniques"], sizes["groups"], sizes["models"]
    sv, rv = "annotations.stack_relevance.payload.verdict", "annotations.shallow_review_relevance.payload.verdict"
    st["q_both"], st["q_s_only"], st["q_r_only"] = q(
        f"SELECT count(*) FILTER ({sv} = 'relevant' AND {rv} = 'relevant'), "
        f"count(*) FILTER ({sv} = 'relevant' AND {rv} IS DISTINCT FROM 'relevant'), "
        f"count(*) FILTER ({rv} = 'relevant' AND {sv} IS DISTINCT FROM 'relevant') FROM d")[0]
    st["q_neither"] = st["n_docs"] - st["q_both"] - st["q_s_only"] - st["q_r_only"]
    return st


# ---------------------------------------------------------------- the judge evaluation (the card's quality table)
#: The judges of a judge eval set as its scores key them, with the names the card prints.
JUDGES = {"opus": "Claude Opus 5.5", "sol": "GPT Sol 6.1", "sonnet": "Claude Sonnet 5.5"}
#: The sets of a judge eval set the card reports (each re-scored against the release's build).
JUDGE_SETS = ("triage_w", "screen", "affiliation", "model_roles", "technique_staged")
#: The score keys of the judgements the card reports (a renamed key fails the export, it is not skipped).
SCREEN_KEEP = "overall keep (relevant|borderline vs irrelevant)"
SET_UNITS = {"affiliation": "(author, verbatim surface) [normalised: case, spacing, edge markers]",
             "model_roles": "(node, role) >= 2", "technique_staged": "slug presence"}
#: The judges production's technique presence is compared with one by one (D725: no majority, no consensus there).
TECHNIQUE_JUDGES = ("opus", "sol")
#: How the card names a reason a sampled document is not scored (:func:`stack.evals.release_quality.production_labels`).
DROP_WORDS = {"missing": "no longer in the build", "input_changed": "its input changed since the judges read it",
              "verdict_mismatch": "its stored triage verdict disagrees with its evidence"}


@dataclass(frozen=True)
class JudgeEval:
    """The card's quality section and the release.json record, from one judge eval set re-scored on the build."""

    section: str
    record: dict[str, Any]


class _Scores:
    """One set's re-scored dict (as its ``scores.json``) with lookups that name the missing key."""

    def __init__(self, data: Any, rel: str) -> None:
        self.data = data
        self.rel = rel

    def __call__(self, *keys: str) -> Any:
        cur: Any = self.data
        for i, k in enumerate(keys):
            if not isinstance(cur, Mapping) or k not in cur:
                raise DatasetError(f"judge eval: {self.rel} has no {' > '.join(keys[:i + 1])}")
            cur = cur[k]
        return cur


def _f2(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def _ci2(c: Sequence[float | None]) -> str:
    return f"[{_f2(c[0])}, {_f2(c[1])}]"


def _count(x: float) -> str:
    return _fmt(round(x))


def _wrap_md(text: str, width: int = 118) -> str:
    """Markdown prose filled to ``width`` (paragraphs and ``- `` bullets; tables and code blocks kept as they are)."""
    import textwrap

    def fill(s: str, first: str = "", rest: str = "") -> str:
        return textwrap.fill(" ".join(s.split()), width, initial_indent=first, subsequent_indent=rest,
                             break_long_words=False, break_on_hyphens=False)

    blocks = []
    for b in text.strip("\n").split("\n\n"):
        if b.startswith(("|", "```")):
            blocks.append(b)
        elif b.startswith("- "):
            items = re.split(r"\n(?=- )", b)
            blocks.append("\n".join(fill(i[2:], "- ", "  ") for i in items))
        else:
            blocks.append(fill(b))
    return "\n\n".join(blocks) + "\n"


def release_quality(store_root: Path, build_rec: Mapping[str, Any], corpus: pa.Table, judge_eval_dir: str
                    ) -> tuple[dict[str, Any], dict[str, Any]]:
    """The judge eval set ``judge_eval_dir`` re-scored against the release's build and triage's agreement with the
    screen (:mod:`stack.evals.release_quality`, D728), from the build's corpus (``corpus``, the export's table) and
    its ``documents`` and ``triage_evidence`` tables; a missing or inconsistent input refuses the export."""
    from stack.evals import release_quality as rq

    try:
        bt = rq.load_build(store_root, build_rec, corpus)
        return rq.rescore(REPO_ROOT, judge_eval_dir, bt), rq.triage_vs_screen(bt)
    except rq.QualityError as exc:
        raise DatasetError(str(exc)) from exc


def _dropped_sentence(coverage: Mapping[str, Mapping[str, Any]]) -> str:
    n = sum(c["items"] for s, c in coverage.items() if s in JUDGE_SETS)
    k = sum(c["items"] - c["scored"] for s, c in coverage.items() if s in JUDGE_SETS)
    if not k:
        return f"Every one of the {_fmt(n)} sampled documents is scored."
    why: Counter[str] = Counter()
    for s, c in coverage.items():
        if s in JUDGE_SETS:
            for r, m in c["dropped"].items():
                why[DROP_WORDS.get(r, "no longer in the judged population")] += m
    return (f"{_fmt(k)} of the {_fmt(n)} sampled documents are not scored ("
            + "; ".join(f"{_fmt(m)} {w}" for w, m in why.most_common()) + ").")


def judge_eval(rel_dir: str, release_build: str, rescored: Mapping[str, Any]) -> JudgeEval:
    """The card's quality section from ``rescored`` (:func:`stack.evals.release_quality.rescore` of the judge eval
    set ``rel_dir`` against build ``release_build``: every number is computed there from the stored judge labels and
    this build's production labels, none is typed in a template): one table of production's precision, recall and
    F1 with their 95 % intervals and n (triage fetch, screen keep, affiliations and models against the 3-judge
    majority; technique presence against Opus and against Sol), the estimated relevant documents triage drops, the
    scored coverage, two caveats and where the full tables are. Refuses a missing key or interval, a result for
    another build or eval set, and judges other than :data:`JUDGES`; the record lists every eval file read with its
    sha256 and the scored coverage per set."""
    if rescored.get("judge_eval") != rel_dir:
        raise DatasetError(f"judge eval: re-scored {rescored.get('judge_eval')!r}, the config names {rel_dir!r}")
    scored_build = (rescored.get("build") or {}).get("build")
    if scored_build != release_build:
        raise DatasetError(f"judge eval: re-scored on build {scored_build!r}, the release is build {release_build!r}")
    sets = rescored.get("sets") or {}
    sc = {s: _Scores(sets.get(s), f"{rel_dir}/{s} (re-scored on {release_build})") for s in JUDGE_SETS}
    build = rescored["judge_build"]
    tw, scr = sc["triage_w"], sc["screen"]
    if list(tw("judges")) != list(JUDGES):
        raise DatasetError(f"judge eval: {rel_dir}/triage_w judges {tw('judges')}, expected {list(JUDGES)}")

    def prf(point: Mapping[str, Any], ci: Any, keys: Sequence[str]) -> list[str]:
        return [f"{_f2(point[k])} {_ci2(ci(k))}" for k in keys]

    majority = "majority of the 3 judges"
    rows: list[tuple[str, str, str, list[str]]] = []
    est = ("estimates", "triage_w", "majority")
    rows.append(("Triage: fetch the full text (relevant or borderline vs not relevant), from a record's title and "
                 "metadata", majority, f"{_fmt(tw(*est, 'n_scored'))} of {_fmt(tw(*est, 'n_items'))} records, weighted",
                 prf(tw(*est, "point"), lambda k: tw(*est, "ci95", k), ("fetch_precision", "fetch_recall", "fetch_f1"))))
    keep = ("scores", SCREEN_KEEP, "production_vs_majority")
    rows.append(("Screen: keep (relevant or borderline vs irrelevant), from the text", majority,
                 f"{_fmt(scr(*keep, 'n'))} documents",
                 prf(scr(*keep, "per_class", "keep"), lambda k: scr(*keep, "per_class_ci95", "keep", k),
                     ("precision", "recall", "f1"))))
    unit_sets = [("affiliation", "Affiliations: (author, affiliation as written) pairs", "majority"),
                 ("model_roles", "Models: (model, role) pairs at strength 2 or more of 4", "majority"),
                 *[("technique_staged", "Techniques: presence (taxonomy slug, any role)", j) for j in TECHNIQUE_JUDGES]]
    for s, what, ref in unit_sets:
        k = ("scores", SET_UNITS[s], f"production_vs_{ref}")
        rows.append((what, majority if ref == "majority" else JUDGES[ref], f"{_fmt(sc[s](*k, 'n_items'))} documents",
                     prf(sc[s](*k), lambda m, k=k, s=s: sc[s](*k, "ci95", m), ("precision", "recall", "f1"))))

    # the relevant documents triage drops: production's not_relevant strata (Wilson per stratum; the bootstrap
    # interval has zero width where no miss was sampled)
    strata = tw("design", "strata")
    nr = sorted((k for k in strata if k.startswith("not_relevant|c")), key=lambda k: ("<=" not in k, k))
    n_not_rel = sum(strata[k]["N_h"] for k in nr)
    per = []
    for k in nr:
        e = tw("missed_by_stratum", f"majority|triage_w|{k}")
        per.append((k.split("|c", 1)[1].replace("<=", "up to "), e["relevant"], e["scored"],
                    tw("missed_by_stratum", f"majority|triage_w|{k}", "missed_relevant_wilson95")))
    point, ci = tw(*est, "point", "missed_relevant"), tw(*est, "ci95", "missed_relevant")
    if not any(r for _c, r, _n, _w in per):
        missed = (f"none of the {_fmt(sum(n for _c, _r, n, _w in per))} sampled ones is relevant by the majority, so "
                  f"the estimate is {_count(point)}; the Wilson 95 % upper bounds in its confidence strata are "
                  + ", ".join(f"{_count(w[1])} (confidence {c})" for c, _r, _n, w in per) + ".")
    else:
        missed = (f"an estimated {_count(point)} [{_count(ci[0])}, {_count(ci[1])}] of them are relevant by the "
                  "majority (Wilson 95 % per confidence stratum: "
                  + ", ".join(f"[{_count(w[0])}, {_count(w[1])}] (confidence {c})" for c, _r, _n, w in per) + ").")
    t_pair = sc["technique_staged"]("scores", SET_UNITS["technique_staged"], "opus_vs_sol", "f1")
    coverage = rescored.get("coverage") or {}
    if set(JUDGE_SETS) - set(coverage):
        raise DatasetError(f"judge eval: the re-scoring has no coverage for {sorted(set(JUDGE_SETS) - set(coverage))}")
    who = ("How far this release's annotations (production: gpt-6-luna) agree with three language-model judges, "
           "Claude Opus 5.5, GPT Sol 6.1 and Claude Sonnet 5.5, ")
    if build == release_build:
        who += f"who labelled samples of this release's build `{build}` blind, reading the whole document. "
    else:
        who += (f"who labelled samples of build `{build}` blind, reading the whole document; the numbers compare "
                f"their labels with this release's build `{release_build}` on the same documents (a document no "
                "longer in the build, or whose input changed since, is not scored). ")
    out = (who + _dropped_sentence(coverage) + " The reference is the label at least 2 of the 3 judges give; for "
           "technique presence production is compared with Opus and with Sol separately. Intervals are 95 % "
           "document-bootstrap intervals; the triage row is a population estimate from a weighted sample of the "
           "records triage judged, weighted by this build's population. The stand-in annotations are not in the "
           "samples.\n\n"
           "| Judgement | Reference | n | Precision | Recall | F1 |\n|---|---|---|---|---|---|\n"
           + "".join(f"| {_md(a)} | {b} | {c} | {d[0]} | {d[1]} | {d[2]} |\n" for a, b, c, d in rows)
           + f"\nRelevant documents triage drops: of the {_fmt(n_not_rel)} eligible records triage marked not "
           "relevant, whose full text was therefore not fetched, " + missed + "\n\n"
           "- The judges are language models, not human annotators.\n"
           "- How far the judges agree with each other bounds what these numbers can show (technique presence F1 "
           f"between Opus and Sol: {_f2(t_pair)}).\n\n"
           f"The judges' labels, the samples, the method and the full tables for build `{build}` are in `{rel_dir}/` "
           "of the pipeline repository; `release.json` (`quality.judge_eval`) has this release's numbers and "
           "coverage per set.\n")
    table = [{"judgement": a, "reference": b, "n": c, "precision": d[0], "recall": d[1], "f1": d[2]}
             for a, b, c, d in rows]
    record = {"path": rel_dir, "build": build, "scored_build": release_build, "judges": list(JUDGES),
              "reference": {"headline": "majority (2 of 3 judges)",
                            "technique_presence": "each of " + " and ".join(TECHNIQUE_JUDGES)},
              "table": table, "missed_relevant": {"point": point, "ci95": ci[:2], "not_relevant_records": n_not_rel},
              "coverage": coverage, "triage_frame": rescored.get("triage_frame"),
              "store_tables": rescored.get("build"), "files": rescored.get("files") or []}
    return JudgeEval(_wrap_md(out), record)


def write_vocab(stage: Path, m: Mapping_, cfg: DatasetConfig) -> dict[str, set[str]]:
    """VOCABULARY.md: ``vocab.yaml`` as shipped (:func:`shipped_vocab`: only the values that occur) rendered; returns
    the observed values per vocab path (FORMAT's enum lists use them)."""
    vocab = yamlio.load(cfg.path(cfg.vocab).read_text("utf-8"))
    paths = [*(vocab.get("fields") or {}), *(vocab.get("patterns") or {})]
    observed = observed_values(stage / "data" / "corpus.parquet", m.schema, list(m.kinds), paths)
    _write_text(stage / VOCABULARY_FILE, render_vocab(shipped_vocab(vocab, observed)))
    return observed


# ---------------------------------------------------------------- FORMAT.md and the card
def _type_str(t: pa.DataType) -> str:
    if pa.types.is_struct(t):
        return "struct"
    if pa.types.is_list(t):
        return f"list<{_type_str(t.value_type)}>"
    if pa.types.is_timestamp(t):
        return "timestamp[us, UTC]"
    if pa.types.is_date(t):
        return "date"
    if pa.types.is_large_string(t):
        return "string"
    return str(t)


def _md(s: Any) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def _uniform_scalars(t: pa.DataType) -> pa.DataType | None:
    """The one scalar type of every field of struct ``t`` with no field descriptions (the role structs), else None."""
    if not pa.types.is_struct(t) or t.num_fields < 2:
        return None
    types = {f.type for f in t}
    if len(types) != 1 or any((f.metadata or {}).get(b"description") for f in t):
        return None
    only = next(iter(types))
    return None if pa.types.is_struct(only) or pa.types.is_list(only) else only


def _values_note(path: str, m: Mapping_, observed: Mapping[str, set[str]] | None) -> str:
    en = m.enums.get(path) or m.enums.get(path + "[]")
    if not en:
        return ""
    vals, is_open = en
    if observed is not None and path in observed:
        seen = [v for v in vals if v in observed[path]] + sorted(observed[path] - set(vals))
        reserved = [v for v in vals if v not in observed[path]]
        out = (f" Values: {', '.join(f'`{v}`' for v in seen)}{' (an open list: other values may appear)' if is_open else ''}."
               if seen else " No value occurs in this release.")
        if reserved:
            out += f" Reserved (defined, not in this release): {', '.join(f'`{v}`' for v in reserved)}."
        return out
    return f" Values: {', '.join(f'`{v}`' for v in vals)}{' (an open list: other values may appear)' if is_open else ''}."


def field_rows(t: pa.DataType, prefix: str, m: Mapping_, enum_prefix: str | None = None,
               observed: Mapping[str, set[str]] | None = None, top: bool = True) -> list[str]:
    """Table rows for the fields of struct ``t``: a struct of identically typed undescribed scalars (the role structs)
    is one row; a field without a description is not a row of its own (its parent describes it)."""
    out = []
    for f in (t if pa.types.is_struct(t) else []):
        p = f"{prefix}.{f.name}" if prefix else f.name
        ft = f.type
        d = (f.metadata or {}).get(b"description")
        ep = p if enum_prefix is None else p.replace(prefix, enum_prefix, 1)
        lp = p + ("[]" if pa.types.is_list(ft) else "")
        inner = ft.value_type if pa.types.is_list(ft) else ft
        uni = _uniform_scalars(inner)
        ts = (f"struct of {inner.num_fields} {_type_str(uni)}" if uni is not None else _type_str(ft))
        if d or top:
            names = f" Fields: {', '.join(x.name for x in inner)}." if uni is not None else ""
            out.append(f"| `{lp}` | {ts} | {'yes' if f.nullable else 'no'} | "
                       f"{_md(d.decode() if d else '')}{_md(names)}{_md(_values_note(ep, m, observed))} |")
        if uni is None and pa.types.is_struct(inner) and not (p.startswith("annotations.") and p.count(".") == 1):
            out += field_rows(inner, lp, m, None if enum_prefix is None else lp.replace(prefix, enum_prefix, 1),
                              observed, top=False)
    return out


TABLE_HEAD = "| Field | Type | Nullable | Description |\n|---|---|---|---|\n"


def files_table(m: Mapping_, stage: Path, counts: Mapping[str, int]) -> str:
    """FORMAT's and the card's Files table, from mapping ``files``; refuses a shipped file no entry covers."""
    files = m.raw.get("files") or {}
    shipped = sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file())
    uncovered = [p for p in shipped if p not in files and not any(k.endswith("/") and p.startswith(k) for k in files)]
    if uncovered:
        raise DatasetError(f"mapping.yaml files: no line for the shipped file(s) {uncovered[:5]}")
    rows = ["| Path | Rows | What |\n|---|---|---|\n"]
    late = {"FORMAT.md", "README.md", "release.json"}  # written after this table
    for path, what in files.items():
        if path not in late and not any(p == path or (path.endswith("/") and p.startswith(path)) for p in shipped):
            continue
        n = counts.get(path)
        if path.endswith("/"):
            k = [c for c in counts if c.startswith(path)]
            n = sum(counts[c] for c in k) if k else None
        rows.append(f"| `{path}` | {_fmt(n) if n is not None else ''} | {_md(what)} |\n")
    return "".join(rows)


def _fill(text: str, vals: Mapping[str, Any]) -> str:
    """``{{name}}`` placeholders of a template; refuses an unknown or unfilled one."""
    def one(mm: re.Match[str]) -> str:
        if mm.group(1) not in vals:
            raise DatasetError(f"template placeholder {{{{{mm.group(1)}}}}} has no value")
        return str(vals[mm.group(1)])
    return re.sub(r"\{\{(\w+)\}\}", one, text)


def _variant_note(cfg: DatasetConfig) -> str:
    return cfg.internal_variant_note or ""


def remap_sentence(m: Mapping_, doc: Mapping[str, Any]) -> str:
    """FORMAT's one sentence on the status remap: its rules and how many values each changed in this release."""
    by = doc["remap"]["by_rule"]
    rules = "; ".join(f"{_fmt(by.get(r['id'], 0))} records get {', '.join(f'`{k} = {v}`' for k, v in r['set'].items())}: "
                      f"{r['reason']}" for r in m.remap_rules)
    return (f"{_fmt(doc['remap']['values'])} status values differ from the pipeline's (the status remap; release.json "
            f"`remap`): {rules}.")


def quote_unverified_sentence(stats: Mapping[str, Any]) -> str:
    """FORMAT's counts of ``quote_unverified`` technique rows, overall and by ``doc_kind``."""
    rows = stats["quote_unverified"]
    n, k = sum(r[1] for r in rows), sum(r[2] for r in rows)
    if not n:
        return "No technique row is in this release."
    def pct(a: int, b: int) -> str:
        return f"{100 * a / b:.1f} %" if b else "-"
    by = "; ".join(f"{dk} {pct(f, t)} of {_fmt(t)}" for dk, t, f in rows if t >= 100)
    return (f"In this release {_fmt(k)} of {_fmt(n)} technique rows ({pct(k, n)}) carry it; by `doc_kind` (kinds with "
            f"at least 100 rows): {by}.")


def runs_table(ov_rows: Sequence[Mapping[str, Any]]) -> str:
    """FORMAT's table of ``config/overrides.jsonl`` by the run that added the rows."""
    out = ["| Run | Rows | What | Author | Method |\n|---|---|---|---|---|\n"]
    for run in dict.fromkeys(r["run_id"] for r in ov_rows):
        recs = [r for r in ov_rows if r["run_id"] == run]
        kinds = Counter(f"{r['kind']} ({r['target']})" for r in recs)
        out.append(f"| `{run}` | {_fmt(len(recs))} | " + "; ".join(f"{_fmt(n)} {k}" for k, n in kinds.most_common())
                   + f" | {', '.join(sorted({r['author'] for r in recs}))} | "
                   + _md(" / ".join(sorted({r["method"] for r in recs}))) + " |\n")
    return "".join(out)


def format_md(m: Mapping_, cfg: DatasetConfig, doc: Mapping[str, Any], tdir: Path, counts: Mapping[str, int],
              stage: Path, stats: Mapping[str, Any], observed: Mapping[str, set[str]],
              ov_rows: Sequence[Mapping[str, Any]]) -> str:
    head = _fill((tdir / "FORMAT.head.md").read_text("utf-8"), {
        "variant_note": _variant_note(cfg), "overrides_summary": stats["overrides_summary"],
        "files_table": files_table(m, stage, counts), "records_recipe": records_recipe_sql(list(m.kinds)),
        "remap_sentence": remap_sentence(m, doc), "standin_docs": _fmt(stats["standin_docs"]),
        "quote_unverified_counts": quote_unverified_sentence(stats),
        "release_macros": RELEASE_MACROS, "release_macro_examples": RELEASE_MACRO_EXAMPLES,
        "doc_type_null": _fmt(dict(stats["doc_types"]).get("unknown", 0)),
        "standins": _fmt(stats["standins"]), **_orgs_vals(doc), **_taxonomy_files(doc),
        "taxonomy_layouts": taxonomy_layouts(doc, stage),
        "quote_cap": (f"This release caps quotes and surfaces at {doc['policy']['quote_cap_chars']} characters "
                      "(`release.json` `policy.quote_cap_chars`): a longer one is cut at a word boundary and ends with "
                      "`…` (listed in `release.cuts`); `anchored` and `offset` describe the original excerpt."
                      if doc["policy"]["quote_cap_chars"] else
                      "This variant ships quotes and surfaces whole (`release.json` `policy.quote_cap_chars` is null); "
                      "the public variant cuts them.")})
    head = re.sub(r"\n{3,}", "\n\n", head)
    s: list[str] = [head.rstrip() + "\n"]
    s.append("\n## Generated reference (search, don't read)\n\nGenerated from the release mapping (`configs/releases/"
             f"dataset/mapping.yaml` in the pipeline repository) and the data of release `{doc['release_id']}`. "
             "Enumerated fields list the values that occur in this release; values defined but not used here are "
             f"listed as reserved. Every value word has a sentence in [{VOCABULARY_FILE}]({VOCABULARY_FILE}).\n")
    s.append("\n### Top-level columns\n\n" + TABLE_HEAD)
    s += [r + "\n" for r in field_rows(pa.struct([f for f in m.schema if f.name != "annotations"]), "", m,
                                       observed=observed)]
    s.append("\n### `annotations`: the metadata struct (every kind)\n\n" + TABLE_HEAD)
    s += [r + "\n" for r in field_rows(m.meta_type, "annotations.<kind>.metadata", m, "annotations.*.metadata",
                                       observed=observed)]
    for k in m.kinds.values():
        s.append(f"\n### `annotations.{k.kind}.payload` (version {k.version}; internal `{k.workflow}`)\n\n"
                 f"{k.entry.get('doc') or ''} JSON Schema: `{ROW_SCHEMA_FILE}`, "
                 f"`properties.annotations.properties.{k.kind}` (the record), its `properties.payload` (the "
                 "payload).\n\n" + TABLE_HEAD)
        s += [r + "\n" for r in field_rows(k.ptype, f"annotations.{k.kind}.payload", m, observed=observed)]
    s.append(f"\n### `{CONFIGS_FILE}`: one entry per `config_id`\n\nA JSON array of objects, one per `config_id`; "
             "`parameters` and `components` are JSON text inside them (decode them a second time).\n\n"
             + TABLE_HEAD)
    s += [r + "\n" for r in field_rows(pa.struct(list(CONFIGS_SCHEMA)), "", m)]
    s.append("\n### Annotations kept from an earlier config\n\n" + accepted_sentences(doc))
    s.append(f"\n### `{OVERRIDES_FILE}`: one line per active override\n\nActive: in force for this release by the "
             "pipeline's effective rule; a row a later row of the same target, key and field superseded, a row a revoke "
             "retired and a row the build did not apply are left out. A row whose document is not in this release "
             f"stays (a verdict that excluded the document explains its absence; a row can also admit a document this build did not ship). {_fmt(len(ov_rows))} lines.\n\n"
             "| Field | Description |\n|---|---|\n")
    s += [f"| `{k}` | {_md(d)} |\n" for k, d in OVERRIDES_FIELDS.items()]
    s.append("\nBy the run that added them:\n\n" + runs_table(ov_rows))
    cols = sorted({st for v in doc["annotations"].values() for st in v})
    s.append("\n### Coverage (records per kind and status)\n\n| Kind | " + " | ".join(cols) + " |\n")
    s.append("|---|" + "---|" * len(cols) + "\n")
    for k, v in doc["annotations"].items():
        s.append(f"| `{k}` | " + " | ".join(_fmt(v.get(c, 0)) for c in cols) + " |\n")
    s.append("\n### Known inconsistencies\n\nCounted in this release; none is repaired by the release code.\n\n"
             "| Rows | What |\n|---|---|\n")
    s += [f"| {n} | {_md(w)} |\n" for n, w in stats["inconsistencies"]]
    cap = doc["policy"]["quote_cap_chars"]
    s.append(f"\n### Capped strings\n\nCap: {f'{cap} characters' if cap else 'none (quotes uncapped)'}; rule: "
             f"{m.raw['quote_cut']}. Quote paths:\n\n")
    s += [f"- `{p}`\n" for p in m.quote_paths]
    s.append("\nSurface paths (verbatim strings from the document, capped like quotes, not counted in "
             "`release.quote_chars`):\n\n")
    s += [f"- `{p}`\n" for p in m.surface_paths]
    s.append("\nModel-written prose (never capped):\n\n")
    s += [f"- `{p}`\n" for p in m.prose_paths]
    s.append("\nProse scrub: " + "; ".join(
        f"`{sc['path']}`: " + ", ".join(f"`{r['pattern']}` → `{r['replace']}`" for r in sc["rules"])
        for sc in m.prose_scrub) + f" ({_fmt(doc['policy']['prose_scrubbed'])} strings changed in this file).\n")
    return "".join(s)


def render_vocab(v: Mapping[str, Any]) -> str:
    out = ["# The Safety Stack: vocabulary\n\nEvery value word of the release with a sentence. Values defined but not "
           "used in this release are listed as reserved. References such as \"rubric-v0.9-c §3\" point to the grading "
           "rubrics in the pipeline repository (public at https://github.com/thesafetystack/stack-data-pipeline).\n"]
    for sect in ("fields", "patterns"):
        for path, e in (v.get(sect) or {}).items():
            out.append(f"\n**`{path}`**: {_md(e.get('doc'))}\n\n")
            out += [f"- `{k}`: {_md(d)}\n" for k, d in (e.get("values") or {}).items()]
            if e.get("reserved"):
                out.append(f"- reserved (not in this release): {', '.join(f'`{x}`' for x in e['reserved'])}\n")
    sc = v.get("scale") or {}
    out.append(f"\n**Strength scale (0-4)**: {_md(sc.get('doc'))}\n\n")
    out += [f"- `{k}`: {_md(d)}\n" for k, d in (sc.get("values") or {}).items()]
    for kind, e in (v.get("roles") or {}).items():
        out.append(f"\n**Roles of `{kind}`**: {_md(e.get('doc'))}\n\n")
        out += [f"- `{k}`: {_md(d)}\n" for k, d in (e.get("values") or {}).items()]
    out.append("\n**Glossary**\n\nSome of the terms used in the project. They also appear in the models' reasoning "
               "traces (the reasoning and notes in the annotations).\n\n")
    out += [f"- **{k}**: {_md(d)}\n" for k, d in (v.get("glossary") or {}).items()]
    return "".join(out)


TYPE_LABELS = {"preprint": "preprints", "forum_post": "forum posts", "paper": "papers", "lab_blog": "lab blog posts",
               "conference": "conference papers", "journal": "journal articles", "system_card": "system cards",
               "model_card": "model cards", "framework_policy": "safety frameworks", "book": "books"}


def _size_category(n: int) -> str:
    for lim, name in ((1_000, "n<1K"), (10_000, "1K<n<10K"), (100_000, "10K<n<100K"), (1_000_000, "100K<n<1M")):
        if n < lim:
            return name
    return "n>1M"


#: The card's hub configuration: the corpus only (one row per document, every annotation; D725).
HF_CONFIGS = ({"config_name": "corpus", "data_files": "data/corpus.parquet", "default": True},)


def _taxonomy_files(doc: Mapping[str, Any]) -> dict[str, str]:
    """FORMAT's ``techniques_file`` and ``models_file``: the newest bundled file's path (``release.json``
    ``taxonomies``; an earlier version beside it is named by the FORMAT sentence of the acceptance that kept its
    rows), or the name pattern when the release ships no such file."""
    return {f"{k}_file": (newest_taxonomy(doc, k) or {}).get("path") or f"taxonomies/{k}-<version>.yaml"
            for k in ("techniques", "models")}


def _orgs_vals(doc: Mapping[str, Any]) -> dict[str, str]:
    """The templates' organisation placeholders (``orgs_file``, ``orgs_clause``, ``orgs_coverage``) from
    ``release.json`` ``orgs`` and ``taxonomies.orgs``; refused when the release resolved no organisations."""
    o, t = doc.get("orgs"), newest_taxonomy(doc, "orgs")
    if not o or not t:
        raise DatasetError("the templates name the organisation vocabulary, but the mapping has no org_fields or no "
                           "affiliation_orgs record pinned one")
    n, w, by = o["affiliations"], o["with_org_ids"], {k: v for k, v in o["by_status"].items() if v or k != "no_record"}
    pct = f"{100 * w / n:.1f} %" if n else "none"
    return {"orgs_file": t["path"],
            "orgs_clause": f", each linked to the organisations of our organisation vocabulary it names ({pct} of "
                           f"{_fmt(n)} affiliations name at least one)",
            "orgs_coverage": f"Affiliations: {_fmt(n)}, of which {_fmt(w)} ({pct}) name at least one organisation "
                             f"of `{t['path']}` (`org_ids`); by `org_status`: " + ", ".join(
                                 f"{s} {_fmt(c)}" for s, c in by.items()) + "."}


def card_md(m: Mapping_, cfg: DatasetConfig, doc: Mapping[str, Any], tdir: Path, stats: Mapping[str, Any],
            stage: Path, counts: Mapping[str, int], jev: JudgeEval) -> str:
    import yaml

    configs = [dict(c) for c in HF_CONFIGS]
    langs = [lang for lang, _n in stats["languages"]]
    front = yaml.safe_dump({"pretty_name": "The Safety Stack", "license": "cc0-1.0", "language": langs or ["en"],
                            "annotations_creators": ["machine-generated"],
                            "size_categories": [_size_category(stats["n_docs"])],
                            "tags": ["ai-safety", "alignment", "annotations"], "configs": configs},
                           sort_keys=False, allow_unicode=True)
    table = TABLE_HEAD + "".join(r + "\n" for r in field_rows(pa.struct(list(m.schema)), "", m)
                                 if r.split("`")[1].count(".") < 1)
    n = stats["n_docs"]
    types = [(t, c) for t, c in stats["doc_types"] if t in TYPE_LABELS]
    cards = sum(c for t, c in types if t in ("system_card", "model_card"))
    parts = [f"{_fmt(c)} {TYPE_LABELS[t]}" for t, c in types[:4]]
    if cards:
        parts.append(f"{_fmt(cards)} system and model cards")
    coverage = ["| Document type (providers') | Documents |\n|---|---|\n"]
    coverage += [f"| {t if t != 'unknown' else 'none given (NULL)'} | {_fmt(c)} |\n" for t, c in stats["doc_types"]]
    coverage.append(f"\nPublished {stats['year_min']}-{stats['year_max']} ({_fmt(stats['undated'])} undated). "
                    "Sources (the anchor record's channel): " + ", ".join(
                        f"{ch} {_fmt(c)}" for ch, c in stats["channels"][:12])
                    + (f", and {len(stats['channels']) - 12} more channels" if len(stats["channels"]) > 12 else "")
                    + ". Document languages (model-assigned): " + ", ".join(
                        f"{lang} {_fmt(c)}" for lang, c in stats["languages"]) + ".\n")
    org_vals = _orgs_vals(doc)
    coverage.append(f"\n{org_vals['orgs_coverage']}\n")
    pol = doc["policy"]
    record = (f"- Release `{doc['release_id']}`, variant `{doc['variant']}`, created {doc['created_at']}.\n"
              f"- Built from pipeline build `{doc['build']['build_id']}` (plan `{doc['build']['plan']}`, "
              f"corpus `{doc['build']['corpus_file_id']}`) by release code `{doc['build']['release_code_commit']}`.\n"
              f"- Rows: {_fmt(doc['rows']['in'])} documents with text in the build, {_fmt(doc['rows']['out'])} "
              "screened relevant and shipped.\n"
              f"- Policy: abstracts `{pol['abstract']}`; texts `{pol['text']}`; quote cap "
              f"{pol['quote_cap_chars'] or 'none'}.\n"
              f"- Overrides: {_fmt(doc['overrides']['rows'])} active rows in `{OVERRIDES_FILE}`.\n"
              "- `release.json` is the full record (every other file with size and sha256).\n")
    vals = {"release_id": doc["release_id"], "n_docs": _fmt(n), "year_min": stats["year_min"],
            "year_max": stats["year_max"], "recent_share": f"{_fmt(stats['recent'])}",
            "recent_from": stats["recent_from"], "type_summary": ", ".join(parts),
            "n_techniques": _fmt(stats["n_techniques"]), "n_models": _fmt(stats["n_models"]),
            "n_groups": _fmt(stats["n_groups"]), "q_both": _fmt(stats["q_both"]), "q_s_only": _fmt(stats["q_s_only"]),
            "q_r_only": _fmt(stats["q_r_only"]),
            "q_neither": (f", and {_fmt(stats['q_neither'])} to neither (FORMAT.md, known inconsistencies)"
                          if stats["q_neither"] else ""),
            "standin_docs": _fmt(stats["standin_docs"]), "hf_repo": doc["hf_repo"], "variant_note": _variant_note(cfg),
            "coverage": "".join(coverage), "overrides_summary": stats["overrides_summary"],
            "early_total": _fmt(sum(stats["early"].values())), "early_forum": _fmt(stats["early"]["forum"]),
            "early_blog": _fmt(stats["early"]["blog"]), "early_manual": _fmt(stats["early"]["manual"]),
            "early_other": _fmt(stats["early"]["other"]), "early_before": EARLY_BEFORE,
            "files_table": files_table(m, stage, counts), "release_record": record, "field_table": table,
            "rows_in": _fmt(doc["rows"]["in"]),
            "quality_section": jev.section.rstrip(), **org_vals,
            # the bundle: what is written so far, plus this card and release.json
            "n_files": sum(p.is_file() for p in stage.rglob("*")) + 2}
    body = re.sub(r"\n{3,}", "\n\n", _fill((tdir / "README.card.md").read_text("utf-8"), vals))
    return f"---\n{front}---\n\n" + body


# ---------------------------------------------------------------- checks over the written stage
def check_bundle(stage: Path, m: Mapping_, cfg: DatasetConfig) -> list[str]:
    """mapping ``generate.checks`` over the written bundle (module docstring); a list of problems."""
    probs: list[str] = []
    corpus = stage / "data" / "corpus.parquet"
    pf = pq.ParquetFile(corpus)
    if not pf.schema_arrow.remove_metadata().equals(m.schema.remove_metadata(), check_metadata=False):
        probs.append("data/corpus.parquet: its arrow schema differs from the mapping's")
    on_disk = {p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file()}
    probs += bundle_files_problems(on_disk, m.raw.get("files") or {})
    con = duck.connect()
    con.execute("SET memory_limit='3GB'; SET threads=4")
    con.execute(f"CREATE VIEW d AS SELECT * FROM read_parquet('{corpus}')")
    variant = m.variants[cfg.variant]
    cap = variant.get("quote_cap_chars")

    def q(sql: str) -> Any:
        return con.execute(sql).fetchall()

    n = q("SELECT count(*), count(DISTINCT doc_id) FROM d")[0]
    if n[0] != n[1]:
        probs.append(f"doc_id not unique ({n[0]} rows, {n[1]} ids)")
    # closed enums (top-level, metadata, payload scalar/list-of-scalar leaves one level deep)
    for path, (vals, is_open) in m.enums.items():
        if is_open:
            continue
        sel = _enum_sql(path)
        if sel is None:
            continue
        got = {r[0] for r in q(sel)} - {None}
        bad = got - set(vals)
        if bad:
            probs.append(f"{path}: values {sorted(bad)} not in the closed enum")
    # kind metadata consistent
    for k in m.kinds:
        bad = q(f"SELECT count(*) FROM d WHERE annotations.{k} IS NOT NULL AND (annotations.{k}.metadata.kind <> '{k}' "
                f"OR annotations.{k}.payload IS NULL)")[0][0]
        if bad:
            probs.append(f"annotations.{k}: {bad} records with a wrong metadata.kind or a NULL payload")
    # config ids resolve; every configs row used
    cfg_rows = json.loads((stage / CONFIGS_FILE).read_text("utf-8"))
    cfgs = {c["config_id"] for c in cfg_rows}
    used: set[str] = set()
    for k in m.kinds:
        used |= {r[0] for r in q(f"SELECT DISTINCT annotations.{k}.metadata.config_id FROM d "
                                 f"WHERE annotations.{k} IS NOT NULL")}
    if used - cfgs:
        probs.append(f"config_ids {sorted(used - cfgs)[:3]} have no configs row")
    if cfgs - used:
        probs.append(f"configs rows {sorted(cfgs - used)[:3]} are not used")
    # override ids resolve in config/overrides.jsonl (from the corpus and from the override configs); its lines have
    # exactly the documented fields
    ov_lines = [json.loads(line) for line in (stage / OVERRIDES_FILE).read_text("utf-8").splitlines()]
    ov_ids = {r["override_id"] for r in ov_lines}
    if len(ov_ids) != len(ov_lines):
        probs.append(f"{OVERRIDES_FILE}: override ids repeat")
    if any(list(r) != list(OVERRIDES_FIELDS) for r in ov_lines):
        probs.append(f"{OVERRIDES_FILE}: lines whose fields are not the documented ones")
    for c in cfg_rows:
        if c["config_id"].startswith("override:") and (
                c["config_id"] != "override:" + "+".join(c["override_ids"]) or set(c["override_ids"]) - ov_ids):
            probs.append(f"{CONFIGS_FILE} {c['config_id']}: override_ids do not resolve in {OVERRIDES_FILE}")
    refs = set()
    for k in m.kinds:
        refs |= {r[0] for r in q(f"SELECT DISTINCT unnest(annotations.{k}.metadata.override_ids) FROM d "
                                 f"WHERE annotations.{k} IS NOT NULL")}
    refs |= {r[0] for r in q("SELECT DISTINCT unnest(identity.override_ids) FROM d")}
    refs |= {r[0] for r in q("SELECT DISTINCT m.override_id FROM d, unnest(identity.members) AS u(m) "
                             "WHERE m.override_id IS NOT NULL")}
    if refs - ov_ids:
        probs.append(f"override ids {sorted(refs - ov_ids)[:3]} are not in {OVERRIDES_FILE}")
    # caps, cuts, quote_chars
    table = pq.read_table(corpus, columns=["doc_id", "annotations", "release", "abstract"])
    rows = table.to_pylist()
    del table
    over = mismatch = cutbad = 0
    for r in rows:
        cuts = {c["path"]: c["chars"] for c in r["release"]["cuts"]}
        total = 0
        for p in m.quote_paths + m.surface_paths:
            kind, rest = _ann_path(p)
            v = r["annotations"].get(kind)
            if v is None:
                continue
            for ip, _h, _k, s in walk_strings(v["payload"], rest, f"annotations.{kind}.payload"):
                if cap is not None and len(s) > cap:
                    over += 1
                if ip in cuts and (not s.endswith(ELLIPSIS) or cuts[ip] <= len(s)):
                    cutbad += 1
                if p in m.quote_paths:
                    total += len(s)
        if total != r["release"]["quote_chars"]:
            mismatch += 1
        if cap is None and cuts:
            cutbad += 1
        if variant["abstract"] != "keep" and r["abstract"] is not None:
            probs.append(f"{r['doc_id']}: an abstract in a variant that withholds them")
        if variant["abstract"] != "keep":
            for ap in m.abstract_paths:
                kind, rest = _ann_path(ap)
                v = r["annotations"].get(kind)
                if any(True for _ in walk_strings(v["payload"] if v else None, rest)):
                    probs.append(f"{r['doc_id']}: {ap} in a variant that withholds abstracts")
        for sc in m.prose_scrub:
            kind, rest = _ann_path(sc["path"])
            v = r["annotations"].get(kind)
            for _ip, _h, _k, s in walk_strings(v["payload"] if v else None, rest):
                if any(re.search(f, s) for f in sc["forbid"]):
                    probs.append(f"{r['doc_id']}: {sc['path']} keeps scrubbed vocabulary")
    del rows
    if over:
        probs.append(f"{over} capped strings over {cap} characters")
    if mismatch:
        probs.append(f"{mismatch} rows whose release.quote_chars differs from the shipped quotes")
    if cutbad:
        probs.append(f"{cutbad} release.cuts entries that do not match their string")
    # inline texts: every body hashes to content.text.sha256 (inline variant); NULL everywhere otherwise
    pfc = pq.ParquetFile(corpus)
    bad_body = 0
    for rg in range(pfc.num_row_groups):
        t = pfc.read_row_group(rg, columns=["content"]).column("content").combine_chunks()
        for txt in t.to_pylist():
            body, digest = txt["text"]["body"], txt["text"]["sha256"]
            if variant["texts"] == "inline":
                if body is None or hashlib.sha256(body.encode("utf-8")).hexdigest() != digest:
                    bad_body += 1
            elif body is not None:
                bad_body += 1
    if bad_body:
        probs.append(f"{bad_body} rows whose content.text.body breaks the variant's text policy ({variant['texts']})")
    # manifest: every other file with its size and sha256
    doc = json.loads((stage / "release.json").read_text("utf-8"))
    listed = {f["path"] for f in doc["files"]}
    if listed != on_disk - {"release.json"}:
        probs.append(f"release.json files differ from the bundle: {sorted(listed ^ (on_disk - {'release.json'}))[:5]}")
    for fe in doc["files"]:
        fp = stage / fe["path"]
        if not fp.is_file() or _sha256_file(fp) != fe["sha256"] or fp.stat().st_size != fe["bytes"]:
            probs.append(f"release.json: size or sha256 of {fe['path']} does not match")
    # organisations: every org id is an entry of the bundled vocabulary, org_status a matcher status consistent with
    # its ids; the fields are NULL exactly where the document has no decided affiliation_orgs record; the records'
    # vocab_version is the bundled file's version
    if m.org_kind is not None:
        k, kf = m.org_kind, m.org_from
        tes = (doc.get("taxonomies") or {}).get("orgs") or []
        if not tes or not all((stage / t_["path"]).is_file() for t_ in tes):
            probs.append(f"release.json taxonomies.orgs: no bundled organisation vocabulary for {k}")
        else:
            ovfs = [orgvocab.parse((stage / t_["path"]).read_bytes()) for t_ in tes]
            te = {"path": ", ".join(t_["path"] for t_ in tes)}
            ids = {o.id for ovf in ovfs for o in ovf.orgs}
            src = f"FROM d, unnest(annotations.{k}.payload.items) AS u(it), unnest(it.affiliations) AS v(a)"
            affs = f"{src} WHERE annotations.{k} IS NOT NULL"
            used = {r[0] for r in q(f"SELECT DISTINCT o {src}, unnest(a.org_ids) AS w(o)")}
            if used - ids:
                probs.append(f"{k} org_ids {sorted(used - ids)[:3]} are not in {te['path']}")
            st = {r[0] for r in q(f"SELECT DISTINCT a.org_status {affs}")}
            if st - set(orgvocab.STATUSES):
                probs.append(f"{k} org_status values {sorted(st - set(orgvocab.STATUSES))} are not matcher statuses")
            bad = q(f"SELECT count(*) {affs} AND ((a.org_status = 'matched' AND len(a.org_ids) = 0) OR "
                    "(a.org_status IN ('non_org', 'generic', 'unmatched') AND len(a.org_ids) > 0))")[0][0]
            if bad:
                probs.append(f"{k}: {bad} affiliations whose org_status disagrees with their org_ids")
            dec = f"annotations.{kf}.metadata.status IN ('ok', 'empty')"
            bad = q(f"SELECT count(*) {affs} AND ((a.org_status IS NULL) = coalesce({dec}, false) "
                    "OR (a.org_status IS NULL) <> (a.org_ids IS NULL) "
                    "OR (a.org_status IS NULL) <> (a.org_candidates IS NULL) "
                    "OR (len(a.org_candidates) > 0) <> (a.org_status = 'ambiguous'))")[0][0]
            if bad:
                probs.append(f"{k}: {bad} affiliations whose org fields are NULL where {kf} is decided, or set where "
                             "it is not")
            ver = {r[0] for r in q(f"SELECT DISTINCT annotations.{kf}.payload.vocab_version FROM d WHERE {dec}")}
            if ver - {ovf.version for ovf in ovfs}:
                probs.append(f"{kf}: vocab_version {sorted(ver - {ovf.version for ovf in ovfs})} is not the bundled "
                             f"{te['path']}")
    # URL fields: nothing scrub_url would still drop (a URL key, which is not scrubbed, included)
    bad_urls = [(path, v) for path in URL_CHECKED for (v,) in q(_url_sql(path)) if scrub_url(v) != v]
    if bad_urls:
        probs.append(f"{len(bad_urls)} URL value(s) carry a contact or signature query parameter (scrub_url), e.g. "
                     f"{bad_urls[0][0]}")
    # leaks in our own files (every file but the data tables)
    for fp in stage.rglob("*"):
        if not fp.is_file() or fp.suffix == ".parquet":
            continue
        rel = fp.relative_to(stage).as_posix()
        probs += leak_problems(rel, fp.read_text("utf-8", errors="replace"))
    # schema.json: rows validate; a kind's payload validates against its payload subschema, its record against the
    # record subschema, not crosswise
    import jsonschema  # type: ignore[import-untyped]

    row_js = json.loads((stage / ROW_SCHEMA_FILE).read_text("utf-8"))
    if "$ref" in json.dumps(row_js):
        probs.append(f"{ROW_SCHEMA_FILE}: not self-contained ($ref)")
    rows_js = [json.loads(json.dumps(r, default=_json_default)) for r in  # one row group, not the whole file
               pq.ParquetFile(corpus).read_row_group(0).slice(0, 200).to_pylist()]
    whole = jsonschema.Draft202012Validator(row_js)
    bad_rows = [r["doc_id"] for r in rows_js if not whole.is_valid(r)]
    if bad_rows:
        probs.append(f"{ROW_SCHEMA_FILE}: rows {bad_rows[:3]} do not validate")
    for spec in m.kinds.values():
        rs_js, ps_js = kind_schemas(row_js, spec.kind)
        ps, rs = jsonschema.Draft202012Validator(ps_js), jsonschema.Draft202012Validator(rs_js)
        for r in rows_js:
            rec = r["annotations"].get(spec.kind)
            if rec is None:
                continue
            if not ps.is_valid(rec["payload"]) or not rs.is_valid(rec) or ps.is_valid(rec):
                probs.append(f"{r['doc_id']} {spec.kind}: payload/record schema validation fails")
                break
    # vocab coverage: every enum value of the mapping has a vocab entry
    vocab = yamlio.load(cfg.path(cfg.vocab).read_text("utf-8"))
    vf = vocab.get("fields") or {}
    for path, (vals, _o) in m.enums.items():
        e = vf.get(path)
        if e is None:
            probs.append(f"vocab.yaml: no entry for {path}")
            continue
        missing = [v for v in vals if v not in {str(k) for k in (e.get("values") or {})}]
        if missing:
            probs.append(f"vocab.yaml {path}: no sentence for {missing}")
    return probs


def leak_problems(rel: str, text: str) -> list[str]:
    """The leak patterns ``text`` (the shipped file ``rel``) matches once :data:`LEAK_ALLOWED_PHRASES` are removed:
    :data:`PATH_LEAKS` and :data:`SHARED_LINKS` everywhere, :data:`STORE_LEAKS` outside ``taxonomies/``."""
    for phrase in LEAK_ALLOWED_PHRASES:
        text = text.replace(phrase, "")
    pats = PATH_LEAKS + SHARED_LINKS + (() if rel.startswith("taxonomies/") else STORE_LEAKS)
    return [f"{rel}: matches {p!r}" for p in pats if re.search(p, text)]


def _url_sql(path: str) -> str:
    """A SELECT of the non-NULL strings at :data:`URL_CHECKED` ``path`` over view ``d`` (one list level at most; a
    member's ``key_value`` only where its ``key_kind`` is ``url``)."""
    lst, sep, leaf = path.partition("[].")
    if not sep:
        return f"SELECT {path} FROM d WHERE {path} IS NOT NULL"
    cond = " AND e.key_kind = 'url'" if leaf == "key_value" else ""
    return f"SELECT e.{leaf} FROM d, unnest({lst}) AS u(e) WHERE e.{leaf} IS NOT NULL{cond}"


def bundle_files_problems(on_disk: Iterable[str], files: Mapping[str, Any]) -> list[str]:
    """The bundle holds exactly the mapping's ``files:`` (a ``<dir>/`` entry: one file or more under it)."""
    have = set(on_disk)
    exact = {k for k in files if not k.endswith("/")}
    probs = []
    if exact - have:
        probs.append(f"files of the mapping not written: {sorted(exact - have)}")
    extra = {p for p in have - exact if not any(k.endswith("/") and p.startswith(k) for k in files)}
    if extra:
        probs.append(f"files written that the mapping does not list: {sorted(extra)[:5]}")
    probs += [f"no file under {k}" for k in files if k.endswith("/") and not any(p.startswith(k) for p in have)]
    return probs


def _enum_sql(path: str) -> str | None:
    """A SELECT of the distinct values at enum ``path`` over view ``d`` (None: a path this check does not reach)."""
    if path.startswith("annotations.*.metadata."):
        f = path.rsplit(".", 1)[1]
        return None if f == "kind" else " UNION ".join(
            f"SELECT DISTINCT annotations.{k}.metadata.{f} FROM d" for k in PRODUCER_KIND)
    m = re.fullmatch(r"annotations\.(\w+)\.payload\.(\w+)", path)
    if m:
        return _payload_enum_sql(*m.groups())
    m = re.fullmatch(r"annotations\.(\w+)\.payload\.(\w+)\[\]\.(\w+)", path)
    if m:
        k, lst, f = m.groups()
        return f"SELECT DISTINCT x.{f} FROM d, unnest(annotations.{k}.payload.{lst}) AS u(x)"
    if "." not in path:
        return f"SELECT DISTINCT {path} FROM d"
    m = re.fullmatch(r"(\w+)\[\]\.(\w+)", path)
    if m:
        return f"SELECT DISTINCT x.{m.group(2)} FROM d, unnest({m.group(1)}) AS u(x)"
    m = re.fullmatch(r"(\w+)\.(\w+)\[\]\.(\w+)", path)
    if m:
        return f"SELECT DISTINCT x.{m.group(3)} FROM d, unnest({m.group(1)}.{m.group(2)}) AS u(x)"
    m = re.fullmatch(r"(\w+)\.(\w+)", path)
    if m:
        return f"SELECT DISTINCT {path} FROM d"
    return None


_LIST_FIELDS = {("screen", "buckets"), ("text_usability", "issues")}


def _payload_enum_sql(k: str, f: str) -> str:
    if (k, f) in _LIST_FIELDS:
        return f"SELECT DISTINCT unnest(annotations.{k}.payload.{f}) FROM d"
    return f"SELECT DISTINCT annotations.{k}.payload.{f} FROM d"


__all__ = [
    "DatasetConfig",
    "DatasetError",
    "Mapping_",
    "check_bundle",
    "cut_string",
    "is_dataset_config",
    "judge_eval",
    "leak_problems",
    "load_dataset_config",
    "newest_taxonomy",
    "producer_empty",
    "project_payload",
    "run_dataset_export",
    "scrub_url",
    "taxonomy_source",
    "version_key",
    "walk_strings",
    "with_org_fields",
    "with_orgs",
]
