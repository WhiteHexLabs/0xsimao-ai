#!/usr/bin/env python3
"""Spec validator and deterministic simulator for the 0xSimao AI scheduler.

Standard library only. This is a spec validator / simulator for the
scheduler described in SKILL.md — NOT a production runtime scheduler.

It:

1. validates references/orchestration-defaults.json against
   references/orchestration.schema.json (small built-in validator);
2. checks structural invariants (lenses exactly 1..12, class existence,
   preferredActive <= maxActive);
3. exercises the deep-merge and precedence semantics (config overlays,
   partial lens overrides, unknown-key rejection, flag precedence);
4. derives configured/runtime/effective concurrency and effective class
   limits;
5. simulates model-aware ROLLING / BOUNDED_BATCH / SEQUENTIAL scheduling
   and asserts the scheduler invariants, retry bounds, admission-refusal
   and model-fallback behavior;
6. validates the audit workspace contract from SKILL.md: cwd-anchored
   `.0xsimao-auditor-work` resolution, start-of-run reset scoping,
   zero-padded lens filenames, persistence of findings/bundles/report,
   discovery exclusion of the workspace, and the deprecated
   `--file-output` no-op;
7. checks the findings-index contract (whitehexlabs.audit-findings-index/v1
   YAML shape), scope-map primary sets, the 1600-character return note,
   and that SKILL.md stays a short brief.

Exit code 0 = every check passed.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS_PATH = ROOT / "references" / "orchestration-defaults.json"
SCHEMA_PATH = ROOT / "references" / "orchestration.schema.json"

LENS_IDS = list(range(1, 13))
MAX_LENS_ATTEMPTS = 2

# Config source application order, LOWEST precedence first (SKILL.md Turn 1b).
# A repo-local .0xsimao-ai.json is deliberately absent from this chain:
# audit-target configuration is untrusted and only enters through the
# explicit --config slot.
SOURCE_ORDER = [
    "defaults",
    "runtime profile",
    "explicit --config",
    "invocation flags",
]


class ValidationError(Exception):
    pass


# --------------------------------------------------------------------------
# Minimal JSON Schema validator (the subset used by orchestration.schema.json)
# --------------------------------------------------------------------------

def _type_ok(value, type_name):
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "null":
        return value is None
    raise ValidationError(f"unknown type keyword: {type_name}")


def validate_schema(value, schema, path="$", defs=None):
    """Supports: type, enum, required, properties, patternProperties,
    additionalProperties (False or subschema), minimum, maximum,
    minLength, minProperties, minItems, items, $ref (#/$defs/...)."""
    defs = {} if defs is None else defs
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/$defs/"):
            raise ValidationError(f"{path}: unsupported $ref {ref!r}")
        return validate_schema(value, defs[ref.split("/")[-1]], path, defs)

    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(value, t) for t in types):
            raise ValidationError(
                f"{path}: expected type {'|'.join(types)}, got {type(value).__name__}"
            )
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{path}: {value!r} not in enum {schema['enum']!r}")

    if isinstance(value, dict):
        for required in schema.get("required", []):
            if required not in value:
                raise ValidationError(f"{path}: missing required property {required!r}")
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            raise ValidationError(
                f"{path}: needs at least {schema['minProperties']} properties"
            )
        props = schema.get("properties", {})
        patterns = schema.get("patternProperties", {})
        additional = schema.get("additionalProperties", True)
        for key in value:
            if key in props:
                validate_schema(value[key], props[key], f"{path}.{key}", defs)
                continue
            for pattern, sub in patterns.items():
                if re.search(pattern, key):
                    validate_schema(value[key], sub, f"{path}.{key}", defs)
                    break
            else:
                if additional is False:
                    raise ValidationError(f"{path}: unknown property {key!r}")
                if isinstance(additional, dict):
                    validate_schema(value[key], additional, f"{path}.{key}", defs)

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ValidationError(f"{path}: needs at least {schema['minItems']} items")
        if "items" in schema:
            for index, item in enumerate(value):
                validate_schema(item, schema["items"], f"{path}[{index}]", defs)

    if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
        raise ValidationError(f"{path}: shorter than minLength {schema['minLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationError(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationError(f"{path}: {value} > maximum {schema['maximum']}")
    return True


# --------------------------------------------------------------------------
# Config loading, deep merge, precedence
# --------------------------------------------------------------------------

def load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def deep_merge(base, overlay, path="$"):
    """SKILL.md Turn 1b merge semantics: scalars replace, objects merge
    key-by-key, arrays replace wholesale, null is rejected."""
    if overlay is None:
        raise ValidationError(f"{path}: null values are not allowed in orchestration config")
    if isinstance(base, dict) and isinstance(overlay, dict):
        merged = dict(base)
        for key, value in overlay.items():
            if value is None:
                raise ValidationError(
                    f"{path}.{key}: null values are not allowed in orchestration config"
                )
            if key in merged:
                merged[key] = deep_merge(merged[key], value, f"{path}.{key}")
            else:
                merged[key] = value
        return merged
    if isinstance(base, list) or isinstance(overlay, list):
        return overlay
    return overlay


def validate_merged_config(cfg):
    """Structural checks the raw schema cannot express, on the MERGED config."""
    schema = load_json(SCHEMA_PATH)
    validate_schema(cfg, schema, defs=schema.get("$defs", {}))
    ids = sorted(int(key) for key in cfg["lensAssignments"])
    if ids != LENS_IDS:
        raise ValidationError(f"merged lensAssignments must cover lenses 1..12 exactly; got {ids}")
    classes = cfg["modelClasses"]
    if cfg["defaultModelClass"] not in classes:
        raise ValidationError(
            f"defaultModelClass {cfg['defaultModelClass']!r} is not a configured model class"
        )
    for lens_id, lens in cfg["lensAssignments"].items():
        cls = lens.get("modelClass", cfg["defaultModelClass"])
        if cls not in classes:
            raise ValidationError(
                f"lensAssignments.{lens_id}: class {cls!r} is not a configured model class"
            )
    for name, spec in classes.items():
        preferred = spec.get("preferredActive")
        maximum = spec.get("maxActive")
        # NOTE: maxActive <= maxConcurrency is deliberately NOT required —
        # class limits are clamped to the effective concurrency at resolve time.
        if preferred is not None and maximum is not None and preferred > maximum:
            raise ValidationError(
                f"modelClasses.{name}: preferredActive {preferred} > maxActive {maximum}"
            )
    return True


def apply_config_chain(defaults, overlays=(), flag=None):
    """Apply sources LOWEST -> HIGHEST (SOURCE_ORDER): defaults, then
    explicit --config overlays, then the invocation flag. Validates the
    merged config after every step so unknown keys fail early."""
    cfg = defaults
    for overlay in overlays:
        cfg = deep_merge(cfg, overlay)
        validate_merged_config(cfg)
    if flag is not None:
        cfg = deep_merge(cfg, {"maxConcurrency": flag})
    validate_merged_config(cfg)
    return cfg


# --------------------------------------------------------------------------
# Resolution: models, concurrency, class limits
# --------------------------------------------------------------------------

def concurrency_class(resolved_model, configured_cls, classes):
    """Derive the concurrency class from the model that will actually run."""
    for name, spec in classes.items():
        if spec.get("model") == resolved_model:
            return name
    return configured_cls


def resolve_lenses(cfg):
    """Resolve every lens to its full scheduler record (SKILL.md Turn 1b)."""
    classes = cfg["modelClasses"]
    default_cls = cfg["defaultModelClass"]
    records = {}
    for lens_id in LENS_IDS:
        assignment = cfg["lensAssignments"].get(str(lens_id), {})
        configured_cls = assignment.get("modelClass", default_cls)
        requested = (
            assignment.get("model")
            or classes[configured_cls].get("model")
            or classes[default_cls]["model"]
        )
        records[lens_id] = {
            "lens": lens_id,
            "priority": assignment.get("priority", 0),
            "configuredModelClass": configured_cls,
            "requestedModel": requested,
            "resolvedModel": requested,
            "concurrencyClass": concurrency_class(requested, configured_cls, classes),
            "attempt": 1,
            "state": "PENDING",
        }
    return records


def resolve_concurrency(cfg, flag=None, runtime_limit=None):
    """configuredConcurrency vs effectiveConcurrency."""
    configured = cfg["maxConcurrency"] if flag is None else flag
    effective = configured if runtime_limit is None else min(configured, runtime_limit)
    return configured, effective


def effective_limits(cfg, effective_concurrency):
    """Clamp configured class limits to the effective concurrency."""
    limits = {}
    for name, spec in cfg["modelClasses"].items():
        maximum = min(spec.get("maxActive", effective_concurrency), effective_concurrency)
        preferred = min(spec.get("preferredActive", maximum), maximum)
        limits[name] = {"preferred": preferred, "max": maximum}
    return limits


# --------------------------------------------------------------------------
# Scheduler simulator
# --------------------------------------------------------------------------

class Scheduler:
    """Deterministic model-aware scheduler simulator. State transitions
    are pure; fill() is always explicit, so drivers control when to
    refill (rolling: after every event; bounded batch: per group)."""

    def __init__(self, cfg, effective_concurrency, mode="ROLLING"):
        self.classes = cfg["modelClasses"]
        self.default_class = cfg["defaultModelClass"]
        self.fallbacks = cfg.get("modelFallbacks", {})
        self.effective = effective_concurrency
        self.mode = mode
        self.limits = effective_limits(cfg, effective_concurrency)
        self.lenses = resolve_lenses(cfg)
        self.spawn_log = []  # (lens, resolvedModel) per attempted spawn
        self.max_active_total = 0
        self.max_active_by_class = defaultdict(int)
        self.active_classes_seen = defaultdict(set)  # lens -> classes while ACTIVE

    # -- state helpers ------------------------------------------------------
    def by_state(self, state):
        return sorted(r["lens"] for r in self.lenses.values() if r["state"] == state)

    def active_ids(self):
        return self.by_state("ACTIVE")

    def active_of_class(self, cls):
        return sum(
            1 for r in self.lenses.values()
            if r["state"] == "ACTIVE" and r["concurrencyClass"] == cls
        )

    # -- fill ----------------------------------------------------------------
    def _choose(self):
        pending = [r for r in self.lenses.values() if r["state"] == "PENDING"]
        candidates = []
        for lens in pending:
            cls = lens["concurrencyClass"]
            cap = self.limits[cls]
            if self.active_of_class(cls) >= cap["max"]:
                continue  # not eligible: class hard cap
            others_have_work = any(
                other["concurrencyClass"] != cls
                and self.active_of_class(other["concurrencyClass"])
                < self.limits[other["concurrencyClass"]]["max"]
                for other in pending
                if other["lens"] != lens["lens"]
            )
            if others_have_work and self.active_of_class(cls) >= cap["preferred"]:
                continue  # preference: hold capped classes at/below preferred
            candidates.append(lens)
        if not candidates:
            return None
        return min(candidates, key=lambda r: (-r["priority"], r["lens"]))

    def fill(self):
        while len(self.active_ids()) < self.effective:
            lens = self._choose()
            if lens is None:
                break
            self._start(lens)
        self.check_invariants()

    def _start(self, lens):
        lens["state"] = "ACTIVE"
        self.spawn_log.append((lens["lens"], lens["resolvedModel"]))
        self.active_classes_seen[lens["lens"]].add(lens["concurrencyClass"])
        self._observe()

    def _observe(self):
        active_count = len(self.active_ids())
        self.max_active_total = max(self.max_active_total, active_count)
        for cls in self.limits:
            self.max_active_by_class[cls] = max(
                self.max_active_by_class[cls], self.active_of_class(cls)
            )

    # -- events (pure transitions; the driver calls fill()) ------------------
    def on_complete(self, lens_id):
        lens = self.lenses[lens_id]
        assert lens["state"] == "ACTIVE", f"lens {lens_id} is {lens['state']}, not ACTIVE"
        lens["state"] = "COMPLETED"
        self.check_invariants()

    def on_execution_failure(self, lens_id):
        """Worker started, then crashed / cancelled / no usable output."""
        lens = self.lenses[lens_id]
        assert lens["state"] == "ACTIVE", f"lens {lens_id} is {lens['state']}, not ACTIVE"
        if lens["attempt"] < MAX_LENS_ATTEMPTS:
            lens["state"] = "PENDING"
            lens["attempt"] += 1
        else:
            lens["state"] = "FAILED"
            lens["failure"] = f"execution failure on attempt {lens['attempt']}"
        self.check_invariants()

    def on_admission_refusal(self):
        """Runtime rejected a spawn; the lens never started. Clamp the
        effective concurrency to what is provably running. The lens stays
        PENDING with its attempt unchanged; with the reduced limit, a
        subsequent fill() does not re-attempt the identical spawn."""
        active_now = len(self.active_ids())
        self.effective = min(self.effective, active_now)
        self.check_invariants()

    def on_model_unavailable(self, lens_id):
        """Requested model rejected before start: apply the configured
        fallback chain once, recompute resolvedModel and concurrencyClass,
        keep the lens PENDING, attempt unchanged."""
        lens = self.lenses[lens_id]
        assert lens["state"] == "PENDING", f"lens {lens_id} is {lens['state']}, not PENDING"
        candidates = list(self.fallbacks.get(lens["resolvedModel"], []))
        candidates.append(self.classes[lens["configuredModelClass"]].get("model"))
        candidates.append(self.classes[self.default_class]["model"])
        for model in candidates:
            if model and model != lens["resolvedModel"]:
                lens["resolvedModel"] = model
                lens["concurrencyClass"] = concurrency_class(
                    model, lens["configuredModelClass"], self.classes
                )
                return lens
        lens["state"] = "FAILED"
        lens["failure"] = (
            f"model {lens['resolvedModel']} unavailable and no usable fallback"
        )
        return lens

    # -- invariants ------------------------------------------------------------
    def check_invariants(self):
        states = [r["state"] for r in self.lenses.values()]
        assert set(self.lenses) == set(LENS_IDS), "lens set must be exactly {1..12}"
        assert len(states) == 12
        for state in ("PENDING", "ACTIVE", "COMPLETED", "FAILED"):
            assert states.count(state) == len(self.by_state(state))
        active_count = states.count("ACTIVE")
        assert active_count <= self.effective, (
            f"active_total {active_count} exceeds effectiveConcurrency {self.effective}"
        )
        for cls, cap in self.limits.items():
            active_cls = self.active_of_class(cls)
            assert active_cls <= cap["max"], (
                f"active({cls}) = {active_cls} exceeds effectiveClassMax {cap['max']}"
            )

    def turn6_eligible(self):
        return (
            self.by_state("COMPLETED") == LENS_IDS
            and not self.by_state("ACTIVE")
            and not self.by_state("PENDING")
            and not self.by_state("FAILED")
        )


def describe_mode(mode, effective_concurrency):
    if mode == "ROLLING":
        return f"mode: rolling (effective concurrency {effective_concurrency})"
    if mode == "BOUNDED_BATCH":
        return (
            "Runtime does not expose independent completion events; using "
            f"bounded-batch mode with effective concurrency {effective_concurrency}."
        )
    return "mode: sequential (no subagent support; 12 isolated passes)"


def run_to_completion(sched):
    """ROLLING driver: initial fill, then complete the active lens with
    (highest priority, lowest id) until all 12 are COMPLETED."""
    sched.fill()
    while sched.by_state("PENDING") or sched.by_state("ACTIVE"):
        active = [
            sched.lenses[lens_id] for lens_id in sched.by_state("ACTIVE")
        ]
        assert active, "scheduler stalled with pending work"
        lens = min(active, key=lambda r: (-r["priority"], r["lens"]))
        sched.on_complete(lens["lens"])
        sched.fill()
    return sched


# --------------------------------------------------------------------------
# Audit workspace (SKILL.md "Audit workspace" + Turns 1/4/5/6)
# --------------------------------------------------------------------------

WORK_DIR_NAME = ".0xsimao-auditor-work"

# Mirrors the Mode Selection exclude pattern. The audit workspace is always
# excluded from discovery: generated audit state is never audit scope, even
# when future PoC .sol files land under poc/.
EXCLUDED_DIR_NAMES = {"interfaces", "lib", "mocks", "test", "script", WORK_DIR_NAME}


def work_dir(audit_root):
    """{work_dir} = {audit_root}/.0xsimao-auditor-work — anchored to the
    captured invocation directory, never to an audited file's directory
    and never to anything repository content can influence."""
    return Path(audit_root) / WORK_DIR_NAME


def lens_findings_path(audit_root, lens_id):
    return work_dir(audit_root) / "findings" / f"lens-{lens_id:02d}.md"


def lens_bundle_path(audit_root, lens_id):
    return work_dir(audit_root) / "bundles" / f"lens-{lens_id:02d}-bundle.md"


def report_path(audit_root):
    return work_dir(audit_root) / "report.md"


def initialize_workspace(audit_root, target_file=None):
    """Turn 1 init/reset. The deletion target is ALWAYS the literal
    canonical path audit_root/.0xsimao-auditor-work — never a glob, never
    a pattern, never a path derived from repository content. target_file
    (the audited file, when the invocation names one) deliberately does
    not participate: it can never redirect the workspace."""
    canonical = work_dir(audit_root)
    if canonical.is_symlink() or canonical.is_file():
        canonical.unlink()
    elif canonical.exists():
        shutil.rmtree(canonical)
    for sub in ("findings", "bundles", "poc"):
        (canonical / sub).mkdir(parents=True, exist_ok=True)
    return canonical


def accept_lens_output(audit_root, lens_id, text, usable=True):
    """Turn 4 persistence: the orchestrator (not the read-only worker)
    persists accepted raw lens output. Stage inside the workspace, then
    atomically rename onto the canonical filename only once the output is
    validated usable — a failed/incomplete attempt never clobbers or
    creates lens-NN.md."""
    final = lens_findings_path(audit_root, lens_id)
    if not usable:
        return False
    staged = final.parent / (final.name + ".tmp")
    staged.write_text(text, encoding="utf-8")
    staged.replace(final)
    return True


def workspace_complete(audit_root):
    """Turn 5 workspace completeness invariant: findings/lens-01.md …
    lens-12.md all exist."""
    return all(lens_findings_path(audit_root, n).is_file() for n in LENS_IDS)


def discover_in_scope(audit_root):
    """Python mirror of the Mode Selection find, including the mandatory
    .0xsimao-auditor-work/ exclusion."""
    root = Path(audit_root)
    found = []
    for path in sorted(root.rglob("*.sol")):
        rel = path.relative_to(root)
        if any(part in EXCLUDED_DIR_NAMES for part in rel.parts):
            continue
        name = rel.name
        if name.endswith(".t.sol") or "Test" in name or "Mock" in name:
            continue
        found.append(path)
    return found


def simulate_full_run(audit_root, flags=()):
    """Deterministic model of one complete audit invocation per SKILL.md:
    Turn 1 init/reset, Turn 2 money map, Turn 3 source + 12 bundles,
    Turn 4 persisted lens outputs, Turn 6 report. A `--file-output` in
    flags is accepted as the documented deprecated no-op and changes
    nothing about the run."""
    initialize_workspace(audit_root)
    work = work_dir(audit_root)
    (work / "money-map.md").write_text("# Money Map\n", encoding="utf-8")
    (work / "source.md").write_text("# Source\n", encoding="utf-8")
    for n in LENS_IDS:
        lens_bundle_path(audit_root, n).write_text(f"bundle {n}\n", encoding="utf-8")
        accept_lens_output(audit_root, n, f"FINDING lens {n}\n")
    report_path(audit_root).write_text("# Security Review\n", encoding="utf-8")
    return work


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

_CHECKS = []


def check(fn):
    _CHECKS.append(fn)
    return fn


@check
def defaults_validate_against_schema():
    schema = load_json(SCHEMA_PATH)
    validate_schema(load_json(DEFAULTS_PATH), schema, defs=schema.get("$defs", {}))


@check
def defaults_structure():
    cfg = load_json(DEFAULTS_PATH)
    validate_merged_config(cfg)
    deep = cfg["modelClasses"]["deep"]
    assert deep["preferredActive"] <= deep["maxActive"]


@check
def every_concurrency_1_to_12_is_valid():
    defaults = load_json(DEFAULTS_PATH)
    for n in range(1, 13):
        cfg = apply_config_chain(defaults, flag=n)
        assert cfg["maxConcurrency"] == n
        # deep maxActive 2 > 1 must NOT invalidate --concurrency 1
        validate_merged_config(cfg)


@check
def class_limits_clamp_to_effective_concurrency():
    defaults = load_json(DEFAULTS_PATH)
    expectations = {1: (1, 1), 2: (1, 2), 3: (1, 2), 5: (1, 2), 12: (1, 2)}
    for n, (preferred, maximum) in expectations.items():
        limits = effective_limits(defaults, n)
        assert limits["deep"] == {"preferred": preferred, "max": maximum}, (n, limits)
        assert limits["fast"] == {"preferred": n, "max": n}, (n, limits)


@check
def concurrency_one_runs_the_full_audit():
    defaults = load_json(DEFAULTS_PATH)
    cfg = apply_config_chain(defaults, flag=1)
    configured, effective = resolve_concurrency(cfg, flag=1)
    assert (configured, effective) == (1, 1)
    assert effective_limits(cfg, effective)["deep"] == {"preferred": 1, "max": 1}
    sched = Scheduler(cfg, effective)
    run_to_completion(sched)
    assert sched.max_active_total <= 1
    assert sched.turn6_eligible()


@check
def runtime_limit_below_requested():
    defaults = load_json(DEFAULTS_PATH)
    cfg = apply_config_chain(defaults)
    configured, effective = resolve_concurrency(cfg, flag=5, runtime_limit=3)
    assert (configured, effective) == (5, 3)
    sched = Scheduler(cfg, effective)
    run_to_completion(sched)
    assert sched.max_active_total <= 3, "Turn 4 must never try to keep five active"
    assert sched.turn6_eligible()


@check
def partial_lens_override_preserves_defaults():
    defaults = load_json(DEFAULTS_PATH)
    overlay = {"lensAssignments": {"5": {"modelClass": "deep"}}}
    cfg = apply_config_chain(defaults, [overlay])
    assert cfg["lensAssignments"]["5"] == {"modelClass": "deep", "priority": 50}
    assert sorted(int(key) for key in cfg["lensAssignments"]) == LENS_IDS


@check
def flag_precedence_beats_config():
    defaults = load_json(DEFAULTS_PATH)
    cfg = apply_config_chain(defaults, [{"maxConcurrency": 5}], flag=2)
    assert cfg["maxConcurrency"] == 2
    configured, effective = resolve_concurrency(cfg, flag=2)
    assert (configured, effective) == (2, 2)


@check
def unknown_keys_fail_early():
    schema = load_json(SCHEMA_PATH)
    defaults = load_json(DEFAULTS_PATH)
    bad_configs = [
        {"maxConcurency": 3},                                # top-level typo
        {"lensAssignments": {"5": {"modelClas": "deep"}}},   # nested typo
        {"modelClasses": {"deep": {"modle": "GLM-5.3"}}},    # class-field typo
        {"maxConcurrency": None},                            # null rejected
        {"lensAssignments": {"13": {"modelClass": "deep"}}}, # lens id out of range
        {"lensAssignments": {"0": {"modelClass": "deep"}}},  # lens id out of range
        {"maxConcurrency": 0},                               # below minimum
        {"maxConcurrency": 13},                              # above maximum
        {"modelClasses": {"deep": {"preferredActive": 3, "maxActive": 2}}},
        {"workDir": "/tmp/evil"},                            # workspace redirect (trust boundary)
        {"outputDirectory": "/tmp/evil"},                    # workspace redirect (trust boundary)
    ]
    for bad in bad_configs:
        try:
            merged = deep_merge(defaults, bad)
            validate_merged_config(merged)
        except ValidationError:
            continue
        raise AssertionError(f"config {bad!r} should have failed validation")


@check
def direct_model_override_counts_as_deep():
    defaults = load_json(DEFAULTS_PATH)
    overlay = {"lensAssignments": {"6": {"modelClass": "fast", "model": "GLM-5.3"}}}
    cfg = apply_config_chain(defaults, [overlay])
    lens6 = resolve_lenses(cfg)[6]
    assert lens6["configuredModelClass"] == "fast"
    assert lens6["resolvedModel"] == "GLM-5.3"
    assert lens6["concurrencyClass"] == "deep"
    sched = Scheduler(cfg, cfg["maxConcurrency"])
    run_to_completion(sched)
    assert sched.max_active_by_class["deep"] <= 2, "three GLM-5.3 workers must be impossible"
    assert sched.turn6_eligible()


@check
def model_unavailable_falls_back_without_consuming_attempt():
    defaults = load_json(DEFAULTS_PATH)
    cfg = apply_config_chain(defaults)
    sched = Scheduler(cfg, cfg["maxConcurrency"])
    lens4 = sched.lenses[4]
    assert lens4["resolvedModel"] == "GLM-5.3" and lens4["concurrencyClass"] == "deep"
    sched.on_model_unavailable(4)
    assert lens4["state"] == "PENDING" and lens4["attempt"] == 1
    assert lens4["resolvedModel"] == "GLM-5.3-Flash"
    assert lens4["concurrencyClass"] == "fast"
    run_to_completion(sched)
    assert sched.turn6_eligible()
    # lens 4 must run on Flash capacity after the fallback
    assert sched.active_classes_seen[4] == {"fast"}


@check
def rolling_completion_with_scripted_order():
    defaults = load_json(DEFAULTS_PATH)
    cfg = apply_config_chain(defaults)
    sched = Scheduler(cfg, cfg["maxConcurrency"], mode="ROLLING")
    sched.fill()
    # default initial spawn (concurrency 4, deep preferred 1):
    # lens 1 (deep) + the three highest-priority fast lenses
    assert sched.active_ids() == [1, 6, 9, 11]
    assert sched.lenses[1]["concurrencyClass"] == "deep"
    for lens_id in (6, 9, 11):
        assert sched.lenses[lens_id]["concurrencyClass"] == "fast"
    # scripted order: repeatedly complete the lowest-numbered ACTIVE lens —
    # a different order than fill preference, so rolling refill is exercised
    while sched.by_state("ACTIVE"):
        lens_id = min(sched.by_state("ACTIVE"))
        sched.on_complete(lens_id)
        sched.fill()  # rolling: refill the freed slot immediately
    assert sched.by_state("COMPLETED") == LENS_IDS
    assert not sched.by_state("PENDING")
    assert not sched.by_state("ACTIVE")
    assert not sched.by_state("FAILED")
    assert sched.turn6_eligible()
    assert sched.max_active_total <= 4
    assert sched.max_active_by_class["deep"] <= 2


@check
def admission_refusal_clamps_without_consuming_attempt():
    defaults = load_json(DEFAULTS_PATH)
    cfg = apply_config_chain(defaults)
    sched = Scheduler(cfg, 3)
    sched.fill()
    assert sched.active_ids() == [1, 6, 9]
    sched.on_complete(9)  # two active; the fill rules would offer a third spawn
    spawns_before = len(sched.spawn_log)
    sched.on_admission_refusal()  # runtime rejects the third spawn
    assert sched.effective == 2
    lens11 = sched.lenses[11]
    assert lens11["state"] == "PENDING" and lens11["attempt"] == 1
    sched.fill()  # must NOT re-attempt the identical rejected spawn
    assert len(sched.spawn_log) == spawns_before, "identical rejected spawn was retried"
    run_to_completion(sched)
    assert sched.turn6_eligible()
    assert sched.max_active_total <= 3


@check
def execution_failures_bounded_to_two_attempts():
    defaults = load_json(DEFAULTS_PATH)
    cfg = apply_config_chain(defaults)
    sched = Scheduler(cfg, 3)
    sched.fill()
    for lens_id in (9, 6, 1, 11):  # drive lens 8 to ACTIVE
        assert sched.lenses[lens_id]["state"] == "ACTIVE"
        sched.on_complete(lens_id)
        sched.fill()
    assert sched.lenses[8]["state"] == "ACTIVE"
    sched.on_execution_failure(8)  # attempt 1 fails
    lens8 = sched.lenses[8]
    assert lens8["state"] == "PENDING" and lens8["attempt"] == 2
    sched.fill()
    while sched.lenses[8]["state"] != "ACTIVE":
        active = [i for i in sched.active_ids() if i != 8]
        assert active, "stalled waiting for lens 8 to be rescheduled"
        sched.on_complete(active[0])
        sched.fill()
    sched.on_execution_failure(8)  # attempt 2 fails -> terminal
    assert lens8["state"] == "FAILED"
    run_to_completion(sched)
    assert sched.by_state("FAILED") == [8]
    assert not sched.turn6_eligible(), "Turn 6 must not run with a FAILED lens"


@check
def flash_drain_allows_second_deep_never_third():
    defaults = load_json(DEFAULTS_PATH)
    cfg = apply_config_chain(defaults)
    sched = Scheduler(cfg, 3)
    sched.fill()
    while any(
        sched.lenses[i]["concurrencyClass"] == "fast"
        and sched.lenses[i]["state"] in ("PENDING", "ACTIVE")
        for i in LENS_IDS
    ):
        fast_active = [
            i for i in sched.active_ids()
            if sched.lenses[i]["concurrencyClass"] == "fast"
        ]
        target = fast_active[0] if fast_active else sched.active_ids()[0]
        sched.on_complete(target)
        sched.fill()
        assert sched.active_of_class("deep") <= 2
    seen_two = False
    while sched.by_state("PENDING") or sched.by_state("ACTIVE"):
        active = sched.active_ids()
        assert active, "stalled with deep-only pending work"
        seen_two = seen_two or sched.active_of_class("deep") == 2
        sched.on_complete(active[0])
        sched.fill()
        assert sched.active_of_class("deep") <= 2, "three GLM-5.3 workers must be impossible"
    assert seen_two, "deep should reach its hard max once Flash drains"
    assert sched.turn6_eligible()


@check
def bounded_batch_mode_respects_caps():
    defaults = load_json(DEFAULTS_PATH)
    cfg = apply_config_chain(defaults)
    sched = Scheduler(cfg, 3, mode="BOUNDED_BATCH")
    while sched.by_state("PENDING"):
        sched.fill()  # compose one group
        group = sched.active_ids()
        assert group, "bounded batch made no progress"
        assert len(group) <= 3
        deep_in_group = sum(
            1 for i in group if sched.lenses[i]["concurrencyClass"] == "deep"
        )
        assert deep_in_group <= 2
        for lens_id in group:
            sched.on_complete(lens_id)  # no refill inside the group
    assert sched.turn6_eligible()
    assert "bounded-batch" in describe_mode("BOUNDED_BATCH", 3)


@check
def sequential_mode_runs_isolated_passes():
    defaults = load_json(DEFAULTS_PATH)
    cfg = apply_config_chain(defaults)
    sched = Scheduler(cfg, 1, mode="SEQUENTIAL")
    while sched.by_state("PENDING"):
        sched.fill()
        active = sched.active_ids()
        assert len(active) == 1, "sequential must run exactly one lens at a time"
        sched.on_complete(active[0])
    assert sched.turn6_eligible()
    assert "sequential" in describe_mode("SEQUENTIAL", 1)


@check
def turn6_barrier_requires_twelve_completions():
    defaults = load_json(DEFAULTS_PATH)
    cfg = apply_config_chain(defaults)
    sched = Scheduler(cfg, 3)
    sched.fill()
    completed = 0
    while completed < 11:
        active = sched.active_ids()
        assert active
        sched.on_complete(active[0])
        sched.fill()
        completed += 1
    assert len(sched.by_state("COMPLETED")) == 11
    assert not sched.turn6_eligible(), "never proceed with 11/12"


@check
def repo_local_config_is_never_implicit():
    # The documented chain is defaults -> runtime profile -> explicit
    # --config -> flags. A repo-local .0xsimao-ai.json only ever enters
    # through the explicit --config slot (SKILL.md Turn 1b).
    assert SOURCE_ORDER == [
        "defaults",
        "runtime profile",
        "explicit --config",
        "invocation flags",
    ]
    assert not any(
        "repo" in source or ".0xsimao-ai" in source for source in SOURCE_ORDER
    ), "repo-local config must not be an implicit source"


# -- audit workspace: layout, persistence, discovery exclusion --------------

@check
def workspace_case_a_full_repo_run_anchors_at_cwd():
    # Case A: cwd = /tmp/example-protocol -> workspace beneath that directory.
    audit_root = Path("/tmp/example-protocol")
    assert work_dir(audit_root) == Path("/tmp/example-protocol/.0xsimao-auditor-work")
    assert work_dir(audit_root).parent == audit_root


@check
def workspace_case_b_nested_file_still_anchors_at_cwd():
    # Case B: auditing src/core/Vault.sol still writes to <cwd>/.0xsimao-auditor-work,
    # never to src/core/.
    with tempfile.TemporaryDirectory() as tmp:
        audit_root = Path(tmp).resolve()
        nested = audit_root / "src" / "core"
        nested.mkdir(parents=True)
        (nested / "Vault.sol").write_text("contract Vault {}\n", encoding="utf-8")
        work = initialize_workspace(audit_root, target_file=nested / "Vault.sol")
        assert work == audit_root / WORK_DIR_NAME
        assert not (nested / WORK_DIR_NAME).exists()
        assert not (audit_root / "src" / WORK_DIR_NAME).exists()


@check
def workspace_case_c_only_hidden_dir_added_to_project_root():
    # Case C: after a run, the only new root-level path is the workspace.
    with tempfile.TemporaryDirectory() as tmp:
        audit_root = Path(tmp).resolve()
        (audit_root / "Vault.sol").write_text("contract Vault {}\n", encoding="utf-8")
        (audit_root / "README.md").write_text("protocol\n", encoding="utf-8")
        before = {p.name for p in audit_root.iterdir()}
        simulate_full_run(audit_root)
        after = {p.name for p in audit_root.iterdir()}
        assert after - before == {WORK_DIR_NAME}
        for stray in ("money-map.md", "source.md", "report.md", ".audit-simao-zz9"):
            assert not (audit_root / stray).exists()


@check
def workspace_case_d_excluded_from_discovery():
    # Case D: a .sol file inside the workspace (future PoC) is never
    # discovered as audit scope.
    with tempfile.TemporaryDirectory() as tmp:
        audit_root = Path(tmp).resolve()
        (audit_root / "src").mkdir()
        (audit_root / "src" / "Vault.sol").write_text("contract Vault {}\n", encoding="utf-8")
        simulate_full_run(audit_root)
        (work_dir(audit_root) / "poc" / "Exploit.sol").write_text(
            "contract Exploit {}\n", encoding="utf-8"
        )
        discovered = discover_in_scope(audit_root)
        assert (audit_root / "src" / "Vault.sol") in discovered
        assert not any(WORK_DIR_NAME in p.parts for p in discovered), (
            "workspace content must never be discovered as audit scope"
        )


@check
def workspace_case_e_stale_contents_reset_at_start():
    # Case E: previous-run artifacts (including a stale report.md) are gone
    # after initialization; the canonical structure is recreated.
    with tempfile.TemporaryDirectory() as tmp:
        audit_root = Path(tmp).resolve()
        work = audit_root / WORK_DIR_NAME
        (work / "findings").mkdir(parents=True)
        (work / "findings" / "old.md").write_text("stale\n", encoding="utf-8")
        (work / "report.md").write_text("# stale report\n", encoding="utf-8")
        initialize_workspace(audit_root)
        assert not (work / "findings" / "old.md").exists()
        assert not (work / "report.md").exists(), (
            "a stale report.md must be removed at start so it cannot be "
            "mistaken for the outcome of a run that fails midway"
        )
        for sub in ("findings", "bundles", "poc"):
            assert (work / sub).is_dir()


@check
def workspace_case_f_final_artifacts_persist():
    # Case F: after a successful run the full artifact set remains on disk.
    with tempfile.TemporaryDirectory() as tmp:
        audit_root = Path(tmp).resolve()
        work = simulate_full_run(audit_root)
        for name in ("report.md", "money-map.md", "source.md"):
            assert (work / name).is_file()
        for n in LENS_IDS:
            assert (work / "findings" / f"lens-{n:02d}.md").is_file()
            assert (work / "bundles" / f"lens-{n:02d}-bundle.md").is_file()
        assert (work / "poc").is_dir()
        # zero-padded naming: lens-01.md exists, lens-1.md never does
        assert not (work / "findings" / "lens-1.md").exists()
        assert not (work / "bundles" / "lens-1-bundle.md").exists()


@check
def workspace_case_g_deprecated_file_output_is_noop():
    # Case G: --file-output must not fail the run and must not create a
    # second report outside the workspace.
    with tempfile.TemporaryDirectory() as tmp:
        audit_root = Path(tmp).resolve()
        (audit_root / "Vault.sol").write_text("contract Vault {}\n", encoding="utf-8")
        simulate_full_run(audit_root, flags=["--file-output"])
        reports = sorted(audit_root.rglob("report.md"))
        assert reports == [work_dir(audit_root) / "report.md"], (
            "exactly one report.md, inside the workspace"
        )
        assert not (audit_root / "report.md").exists()


@check
def workspace_reset_never_targets_repo_controlled_paths():
    # The reset deletes ONLY the literal canonical directory. Legacy
    # temp-dir names, prefix-adjacent decoys, real project content, and a
    # repo-local config attempting to redirect the workspace all survive.
    with tempfile.TemporaryDirectory() as tmp:
        audit_root = Path(tmp).resolve()
        decoys = [
            ".audit-simao-zz1234",           # legacy temp-dir name
            ".0xsimao-auditor-work-backup",  # prefix-adjacent decoy
            ".0xsimao-auditor-work-malicious",
            "src",                           # real project content
        ]
        for name in decoys:
            (audit_root / name).mkdir()
            (audit_root / name / "keep.txt").write_text("x", encoding="utf-8")
        (audit_root / ".0xsimao-ai.json").write_text(
            json.dumps({"workDir": str(audit_root / "elsewhere")}),
            encoding="utf-8",
        )
        initialize_workspace(audit_root)
        for name in decoys:
            assert (audit_root / name / "keep.txt").is_file(), (
                f"reset must not touch {name}"
            )


@check
def failed_lens_attempt_never_fakes_a_completed_artifact():
    # A rejected retry leaves the previously accepted output intact; an
    # accepted retry atomically overwrites the same lens file.
    with tempfile.TemporaryDirectory() as tmp:
        audit_root = Path(tmp).resolve()
        initialize_workspace(audit_root)
        accept_lens_output(audit_root, 3, "attempt-1 output\n")
        accept_lens_output(audit_root, 3, "garbage", usable=False)
        assert (
            lens_findings_path(audit_root, 3).read_text(encoding="utf-8")
            == "attempt-1 output\n"
        )
        accept_lens_output(audit_root, 3, "attempt-2 output\n")
        assert (
            lens_findings_path(audit_root, 3).read_text(encoding="utf-8")
            == "attempt-2 output\n"
        )
        assert not (work_dir(audit_root) / "findings" / "lens-03.md.tmp").exists()


@check
def turn5_blocks_without_twelve_persisted_findings():
    # Turn 5 completeness: 11/12 persisted outputs block Turn 6; the
    # report must never be composed from fewer than 12.
    with tempfile.TemporaryDirectory() as tmp:
        audit_root = Path(tmp).resolve()
        initialize_workspace(audit_root)
        for n in LENS_IDS[:-1]:
            accept_lens_output(audit_root, n, f"FINDING {n}\n")
        assert not workspace_complete(audit_root), (
            "11/12 persisted outputs must block Turn 6"
        )
        accept_lens_output(audit_root, 12, "FINDING 12\n")
        assert workspace_complete(audit_root)


# --------------------------------------------------------------------------
# Context budget, scope rules, findings index (SKILL.md + references)
# --------------------------------------------------------------------------

SCOPE_RULES_PATH = ROOT / "references" / "scope-rules.json"
FINDINGS_TEMPLATE_PATH = ROOT / "references" / "findings-index.yaml"
SKILL_PATH = ROOT / "SKILL.md"
INDEX_SCHEMA = "whitehexlabs.audit-findings-index/v1"
INDEX_TOP_LEVEL = ("schema", "run_id", "step", "coverage", "findings")
INDEX_COVERAGE_KEYS = ("summary", "limitations")
INDEX_FINDING_KEYS = (
    "id",
    "original_finding_id",
    "title",
    "root_cause",
    "mechanism",
    "locations",
    "preconditions",
    "impact",
    "requires_privileged_actor",
    "severity_hint",
    "evidence",
)
INDEX_EVIDENCE_KEYS = ("artifact_id", "locator")
RETURN_NOTE_LIMIT = 1600
# Eager SKILL.md was 33384 chars before the split. Stay well under that.
SKILL_CHAR_BUDGET = 16000


def load_scope_rules():
    return load_json(SCOPE_RULES_PATH)


def primary_paths(files, lens_id, rules=None):
    """Which in-scope files a lens must read. Mirrors references/scope-rules.md.

    files: iterable of {path, tags, uncertain}. A full-source lens lists
    every file. An uncertain file is listed for every lens. A narrow lens
    lists files whose tags intersect narrowTags for that lens.
    """
    rules = load_scope_rules() if rules is None else rules
    full = {int(n) for n in rules["fullSourceLenses"]}
    narrow = {int(key): set(value) for key, value in rules["narrowTags"].items()}
    selected = []
    for item in files:
        tags = set(item.get("tags") or [])
        uncertain = bool(item.get("uncertain"))
        if lens_id in full or uncertain or (tags & narrow.get(lens_id, set())):
            selected.append(item["path"])
    return selected


def _require_exact_keys(value, allowed, label):
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be a mapping")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ValidationError(f"unknown key {unknown[0]}")
    missing = [key for key in allowed if key not in value]
    if missing:
        raise ValidationError(missing[0])


def validate_findings_index(doc, expected_run_id=None, expected_step=None):
    """whitehexlabs.audit-findings-index/v1, matching the orchestrator rules.

    Top-level keys are exactly schema, run_id, step, coverage, findings.
    requires_privileged_actor is boolean true, boolean false, or the
    string 'unknown'. The strings 'true' and 'false' are rejected.
    Evidence is a non-empty list of {artifact_id: primary-report, locator}.
    """
    _require_exact_keys(doc, INDEX_TOP_LEVEL, "findings index")
    if doc["schema"] != INDEX_SCHEMA:
        raise ValidationError("findings-index schema")
    if not isinstance(doc["run_id"], str) or not doc["run_id"].strip():
        raise ValidationError("run_id")
    if expected_run_id is not None and doc["run_id"] != expected_run_id:
        raise ValidationError("run_id")
    if isinstance(doc["step"], bool) or not isinstance(doc["step"], int):
        raise ValidationError("step")
    if expected_step is not None and doc["step"] != expected_step:
        raise ValidationError("step")
    coverage = doc["coverage"]
    if not isinstance(coverage, dict):
        raise ValidationError("coverage.summary")
    coverage_unknown = sorted(set(coverage) - set(INDEX_COVERAGE_KEYS))
    if coverage_unknown:
        raise ValidationError(f"unknown key {coverage_unknown[0]}")
    summary = coverage.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValidationError("coverage.summary")
    if "limitations" not in coverage:
        raise ValidationError("coverage.limitations")
    limitations = coverage["limitations"]
    if not isinstance(limitations, list) or not all(isinstance(item, str) for item in limitations):
        raise ValidationError("coverage.limitations")
    findings = doc["findings"]
    if not isinstance(findings, list):
        raise ValidationError("findings")
    seen = set()
    for index, entry in enumerate(findings):
        _require_exact_keys(entry, INDEX_FINDING_KEYS, f"findings[{index}]")
        finding_id = entry["id"]
        if not isinstance(finding_id, str) or not finding_id.strip():
            raise ValidationError("id")
        if finding_id in seen:
            raise ValidationError("duplicate finding id")
        seen.add(finding_id)
        original = entry["original_finding_id"]
        if original is not None and (not isinstance(original, str) or not original.strip()):
            raise ValidationError("original_finding_id")
        for key in ("title", "root_cause", "mechanism", "impact"):
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise ValidationError(key)
        locations = entry["locations"]
        if not isinstance(locations, list) or not locations:
            raise ValidationError("locations")
        for loc in locations:
            if not isinstance(loc, str) or ":" not in loc or not loc.rsplit(":", 1)[-1].isdigit():
                raise ValidationError("locations")
        preconditions = entry["preconditions"]
        if (
            not isinstance(preconditions, list)
            or not preconditions
            or not all(isinstance(item, str) and item.strip() for item in preconditions)
        ):
            raise ValidationError("preconditions")
        privileged = entry["requires_privileged_actor"]
        if privileged not in (True, False, "unknown"):
            raise ValidationError("requires_privileged_actor")
        hint = entry["severity_hint"]
        if hint is not None and (not isinstance(hint, str) or not hint.strip()):
            raise ValidationError("severity_hint")
        evidence = entry["evidence"]
        if not isinstance(evidence, list) or not evidence:
            raise ValidationError("evidence")
        for item in evidence:
            _require_exact_keys(item, INDEX_EVIDENCE_KEYS, "evidence")
            if item["artifact_id"] != "primary-report":
                raise ValidationError("primary-report")
            if not isinstance(item["locator"], str) or not item["locator"].strip():
                raise ValidationError("locator")
    return True


def sample_finding(**overrides):
    entry = {
        "id": "local-001",
        "original_finding_id": "H-1",
        "title": "`Vault::redeem` does not decrease `totalAssets` on the early-exit branch",
        "root_cause": (
            "src/Vault.sol:120 the early-return branch transfers assets and "
            "never decrements totalAssets"
        ),
        "mechanism": (
            "1. Alice deposits 100e18. 2. Alice redeems on the early-exit branch. "
            "3. Bob withdraws last and is short 100e18."
        ),
        "locations": ["src/Vault.sol:120"],
        "preconditions": ["None"],
        "impact": "The last withdrawer is short by the amount early redeemers took.",
        "requires_privileged_actor": False,
        "severity_hint": "HIGH",
        "evidence": [{
            "artifact_id": "primary-report",
            "locator": "section 'H-1' of the primary report",
        }],
    }
    entry.update(overrides)
    return entry


def sample_findings_index(findings=None):
    return {
        "schema": INDEX_SCHEMA,
        "run_id": "run-20260101-000000-000000-ab12",
        "step": 1,
        "coverage": {
            "summary": (
                "Accounting-first review of src/Vault.sol across 12 lenses. "
                "Lenses 1, 6, 9, 11, and 12 read source.md."
            ),
            "limitations": ["lens 5 NO_SURFACE: no cross-chain code"],
        },
        "findings": findings if findings is not None else [sample_finding()],
    }


def build_return_note(
    *,
    done=True,
    counts,
    report_path,
    index_path,
    log_path,
    mode,
    effective,
    warnings=(),
    titles=(),
):
    """The parent-facing note. Required lines always stay. Titles drop
    first, then warnings, until the note is within 1600 characters.
    Finding bodies are not a field here."""
    status = "done" if done else "incomplete"
    required = [
        f"0xsimao-ai {status}",
        (
            "counts: critical={critical} high={high} medium={medium} "
            "low={low} info={info} leads={leads}"
        ).format(**counts),
        f"scheduler: {mode} effective={effective}",
        f"report: {report_path}",
        f"index: {index_path}",
        f"log: {log_path}",
    ]
    optional = [f"warning: {item}" for item in warnings] + [f"title: {item}" for item in titles]
    kept = list(optional)
    while True:
        note = "\n".join(required + kept)
        if len(note) <= RETURN_NOTE_LIMIT:
            return note
        if not kept:
            raise ValidationError("return note required lines exceed 1600 characters")
        kept.pop()


def _agent_body(path):
    text = Path(path).read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValidationError(f"{path} missing frontmatter")
    end = text.find("\n---", 3)
    if end < 0:
        raise ValidationError(f"{path} unclosed frontmatter")
    return text[end + 4 :].strip()


@check
def scope_rules_cover_every_lens_once():
    rules = load_scope_rules()
    full = [int(n) for n in rules["fullSourceLenses"]]
    narrow = sorted(int(key) for key in rules["narrowTags"])
    assert full == [1, 6, 9, 11, 12], full
    assert narrow == [2, 3, 4, 5, 7, 8, 10], narrow
    assert set(full).isdisjoint(narrow)
    assert sorted(set(full) | set(narrow)) == LENS_IDS
    vocabulary = set(rules["tagVocabulary"])
    for tags in rules["narrowTags"].values():
        unknown = set(tags) - vocabulary
        assert not unknown, unknown


@check
def primary_sets_keep_full_lenses_and_skip_irrelevant_files():
    vault = {"path": "src/Vault.sol", "tags": ["accounting", "lifecycle", "shares"], "uncertain": False}
    bridge = {"path": "src/Bridge.sol", "tags": ["cross-chain", "async"], "uncertain": False}
    mystery = {"path": "src/Mystery.sol", "tags": [], "uncertain": True}
    files = [vault, bridge, mystery]
    for lens_id in (1, 6, 9, 11, 12):
        assert primary_paths(files, lens_id) == [
            "src/Vault.sol",
            "src/Bridge.sol",
            "src/Mystery.sol",
        ], lens_id
    # Lens 5 reads the bridge and the uncertain file, not the plain vault.
    assert primary_paths(files, 5) == ["src/Bridge.sol", "src/Mystery.sol"]
    # Lens 4 has no liquidation tag here.
    assert primary_paths(files, 4) == ["src/Mystery.sol"]
    # Accounting tag pulls the vault into the share lens; the bridge does not.
    assert primary_paths(files, 2) == ["src/Vault.sol", "src/Mystery.sol"]
    # A fully read file with no matching tag is not primary for a narrow lens.
    plain = [{"path": "src/Registry.sol", "tags": ["stateful"], "uncertain": False}]
    assert primary_paths(plain, 5) == []
    assert primary_paths(plain, 8) == []
    assert primary_paths(plain, 1) == ["src/Registry.sol"]


def _rejects(doc, needle, **kwargs):
    try:
        validate_findings_index(doc, **kwargs)
    except ValidationError as exc:
        assert needle in str(exc), (needle, exc)
        return
    raise AssertionError(f"expected rejection containing {needle!r}")


@check
def findings_index_matches_orchestrator_contract():
    run_id = "run-20260101-000000-000000-ab12"
    validate_findings_index(
        sample_findings_index(), expected_run_id=run_id, expected_step=1
    )
    validate_findings_index(sample_findings_index(findings=[]))
    # Quoted booleans are not the YAML booleans true/false. unknown is a string.
    _rejects(
        sample_findings_index(findings=[sample_finding(requires_privileged_actor="false")]),
        "requires_privileged_actor",
    )
    _rejects(
        sample_findings_index(findings=[sample_finding(requires_privileged_actor="true")]),
        "requires_privileged_actor",
    )
    omitted = sample_finding()
    del omitted["requires_privileged_actor"]
    _rejects(sample_findings_index(findings=[omitted]), "requires_privileged_actor")
    unknown_ok = sample_finding(id="local-002", requires_privileged_actor="unknown", severity_hint=None)
    validate_findings_index(sample_findings_index(findings=[sample_finding(), unknown_ok]))
    _rejects(
        sample_findings_index(findings=[sample_finding(), sample_finding(id="local-001")]),
        "duplicate finding id",
    )
    missing_cause = sample_finding()
    del missing_cause["root_cause"]
    _rejects(sample_findings_index(findings=[missing_cause]), "root_cause")
    _rejects(
        sample_findings_index(findings=[sample_finding(evidence=[])]),
        "evidence",
    )
    _rejects(
        sample_findings_index(findings=[sample_finding(evidence=[{
            "artifact_id": "findings-native-json",
            "locator": "x",
        }])]),
        "primary-report",
    )
    bad_schema = sample_findings_index()
    bad_schema["schema"] = "whitehexlabs.audit-findings-index/v2"
    _rejects(bad_schema, "findings-index schema")
    extra = sample_findings_index()
    extra["dedup"] = True
    _rejects(extra, "unknown key")
    extra_finding_key = sample_finding(kind="finding")
    _rejects(sample_findings_index(findings=[extra_finding_key]), "unknown key")
    no_summary = sample_findings_index()
    no_summary["coverage"] = {"limitations": []}
    _rejects(no_summary, "coverage.summary")
    _rejects(sample_findings_index(), "run_id", expected_run_id="run-19990101-000000-000000-dead")
    _rejects(sample_findings_index(), "step", expected_step=2)
    template = FINDINGS_TEMPLATE_PATH.read_text(encoding="utf-8")
    for phrase in (
        "schema: whitehexlabs.audit-findings-index/v1",
        "requires_privileged_actor: unknown",
        "artifact_id: primary-report",
        "findings-index.yaml",
    ):
        assert phrase in template, phrase
    assert "findings-index.json" not in template


@check
def return_note_stays_within_1600_characters():
    counts = {"critical": 0, "high": 2, "medium": 1, "low": 0, "info": 3, "leads": 4}
    note = build_return_note(
        counts=counts,
        report_path=".0xsimao-auditor-work/report.md",
        index_path=".0xsimao-auditor-work/findings-index.yaml",
        log_path=".0xsimao-auditor-work/run.log",
        mode="rolling",
        effective=4,
        warnings=("repo-local config ignored",),
        titles=("H-1 redeem leaves totalAssets unchanged", "H-2 index advanced for free"),
    )
    assert len(note) <= RETURN_NOTE_LIMIT
    assert "report: .0xsimao-auditor-work/report.md" in note
    assert "findings-index.yaml" in note
    assert "high=2" in note
    # A flood of titles must not push the paths out.
    flooded = build_return_note(
        counts=counts,
        report_path=".0xsimao-auditor-work/report.md",
        index_path=".0xsimao-auditor-work/findings-index.yaml",
        log_path=".0xsimao-auditor-work/run.log",
        mode="bounded-batch",
        effective=4,
        titles=tuple(f"{'H-1 ' * 80}title {n}" for n in range(30)),
    )
    assert len(flooded) <= RETURN_NOTE_LIMIT
    assert "index: .0xsimao-auditor-work/findings-index.yaml" in flooded
    assert flooded.count("\ntitle:") < 30


@check
def skill_brief_stays_under_budget_and_names_the_index():
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert len(text) <= SKILL_CHAR_BUDGET, len(text)
    for phrase in (
        "whitehexlabs.audit-findings-index/v1",
        "1600",
        "findings-index.yaml",
        "run.log",
        "Do not concatenate `source.md` into the bundles.",
        "NO_SURFACE:",
    ):
        assert phrase in text, phrase
    assert "source.md` + `money-map.md`" not in text
    assert "findings-index.json" not in text
    method = (ROOT / "references" / "simao-method.md").read_text(encoding="utf-8")
    card = (ROOT / "references" / "method-card.md").read_text(encoding="utf-8")
    assert len(card) < len(method)
    prompt = (ROOT / "references" / "lens-worker-prompt.md").read_text(encoding="utf-8")
    assert len(prompt) < 4000, len(prompt)


@check
def zcode_workers_share_one_body():
    deep = ROOT / "integrations" / "zcode" / "agents" / "0xsimao-deep.md"
    fast = ROOT / "integrations" / "zcode" / "agents" / "0xsimao-fast.md"
    assert _agent_body(deep) == _agent_body(fast)
    body = _agent_body(deep)
    assert "500 characters" in body
    assert "Write" in deep.read_text(encoding="utf-8")
    # The standing prompt must not tell the worker to swallow the whole tree.
    assert "all in-scope source" not in body


def main():
    failures = 0
    for fn in _CHECKS:
        try:
            fn()
        except (AssertionError, ValidationError) as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
        else:
            print(f"PASS {fn.__name__}")
    total = len(_CHECKS)
    print(f"\n{total - failures}/{total} checks passed")
    if failures:
        print("orchestration validation FAILED")
        return 1
    print("orchestration config valid; scheduler invariants hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
