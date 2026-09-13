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
   and model-fallback behavior.

Exit code 0 = every check passed.
"""

from __future__ import annotations

import json
import re
import sys
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
    # default initial spawn: lens 1 (deep) + lens 9 (fast) + lens 6 (fast)
    assert sched.active_ids() == [1, 6, 9]
    assert sched.lenses[1]["concurrencyClass"] == "deep"
    assert sched.lenses[6]["concurrencyClass"] == "fast"
    assert sched.lenses[9]["concurrencyClass"] == "fast"
    order = [9, 6, 1, 11, 10, 12, 2, 4, 3, 7, 8, 5]
    for lens_id in order:
        assert sched.lenses[lens_id]["state"] == "ACTIVE", (
            f"completion order expected lens {lens_id} ACTIVE, "
            f"got {sched.lenses[lens_id]['state']}"
        )
        sched.on_complete(lens_id)
        sched.fill()  # rolling: refill the freed slot immediately
    assert sched.by_state("COMPLETED") == LENS_IDS
    assert not sched.by_state("PENDING")
    assert not sched.by_state("ACTIVE")
    assert not sched.by_state("FAILED")
    assert sched.turn6_eligible()
    assert sched.max_active_total <= 3
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
