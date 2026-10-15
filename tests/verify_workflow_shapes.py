"""Workflow SHAPE taxonomy verification (SPEC-workflows-redesign Phase 3, Tier 2).

Pure unit tests over app.services.workflow_shapes.detect_shapes + shape_headline. No DB,
no provider — the classifier is a pure function of a step list. Proves the SPEC detection
table EXACTLY, one case per shape + combinations + the defensive edge cases (dangling /
renamed branch key, empty steps, self-loop), the display-priority primary, and that both
input shapes (WorkflowStep ORM rows AND plain draft dicts) classify identically.

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys, ASCII-only prints.
"""

import ast
import os
import sys
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_wfshapes_")

from app.models import StepType, WorkflowStep
from app.services import workflow_shapes as ws


def _step(
    step_type=StepType.LLM_AGENT,
    name="",
    key=None,
    branch=None,
    run_condition=None,
    qe_gate=False,
    qe_threshold=70,
    max_visits=None,
):
    """A draft-style step dict (the Design-review shape)."""
    return {
        "step_type": step_type,
        "name": name,
        "key": key,
        "branch": branch,
        "run_condition": run_condition,
        "qe_gate": qe_gate,
        "qe_threshold": qe_threshold,
        "max_visits": max_visits,
    }


def test_empty_steps_is_straight_through():
    r = ws.detect_shapes([])
    assert r.tags == frozenset({ws.STRAIGHT_THROUGH}), r.tags
    assert r.primary == ws.STRAIGHT_THROUGH
    assert r.additive == []
    assert r.steps_len == 0
    # None input is also valid (never raises).
    assert ws.detect_shapes(None).primary == ws.STRAIGHT_THROUGH
    print("  (1) empty/None steps -> straight-through")


def test_straight_through():
    steps = [_step(name="Translate"), _step(step_type=StepType.LQA, name="Check")]
    r = ws.detect_shapes(steps)
    assert r.tags == frozenset({ws.STRAIGHT_THROUGH}), r.tags
    assert r.primary == ws.STRAIGHT_THROUGH
    # An empty-dict branch counts as NO branch (still straight-through).
    r2 = ws.detect_shapes([_step(name="T", branch={})])
    assert r2.primary == ws.STRAIGHT_THROUGH, r2.tags
    print("  (2) plain pipeline (and empty-dict branch) -> straight-through")


def test_gated_via_run_condition():
    steps = [
        _step(name="Translate"),
        _step(step_type=StepType.LQA, name="Check"),
        _step(name="Fix", run_condition={"ref": "prev_lqa", "op": "failed"}),
    ]
    r = ws.detect_shapes(steps)
    assert r.tags == frozenset({ws.GATED}), r.tags
    assert r.primary == ws.GATED
    print("  (3) run_condition guard, no loop-back -> gated")


def test_gated_via_forward_branch_end():
    # An LQA gate that ENDS the run on pass (on_pass=end, on_fail unset -> falls through).
    steps = [
        _step(name="Translate"),
        _step(step_type=StepType.LQA, name="Check", key="chk", branch={"on_pass": "end"}),
    ]
    r = ws.detect_shapes(steps)
    assert r.tags == frozenset({ws.GATED}), r.tags
    assert r.primary == ws.GATED
    print("  (4) forward-only branch (on_pass=end) -> gated (not triage, not loop)")


def test_correction_loop():
    # Translate -> Fix(key) -> LQA gate; on_fail loops back to Fix (earlier).
    steps = [
        _step(name="Translate"),
        _step(name="Fix", key="fix"),
        _step(
            step_type=StepType.LQA,
            name="LQA",
            key="lqa",
            branch={"on_pass": "end", "on_fail": {"step": "fix"}},
            max_visits=3,
        ),
    ]
    r = ws.detect_shapes(steps)
    assert ws.CORRECTION_LOOP in r.tags, r.tags
    assert ws.GATED not in r.tags, "loop-back must suppress the gated tag"
    assert r.primary == ws.CORRECTION_LOOP
    print("  (5) branch loops back to an earlier key -> correction-loop")


def test_self_loop_is_correction_loop():
    # A gate that loops back to ITSELF (equal position = equal-or-earlier order_index).
    steps = [
        _step(name="Translate"),
        _step(step_type=StepType.LQA, name="Check", key="c", branch={"on_fail": {"step": "c"}}),
    ]
    r = ws.detect_shapes(steps)
    assert r.primary == ws.CORRECTION_LOOP, r.tags
    print("  (6) self-loop (target == source position) -> correction-loop")


def test_triage_binary_fork():
    # A gate whose two slots go to DIFFERENT forward targets.
    steps = [
        _step(name="Translate"),
        _step(
            step_type=StepType.LQA,
            name="Route",
            key="r",
            branch={"on_pass": "end", "on_fail": "next"},
        ),
        _step(step_type=StepType.HUMAN_REVIEW, name="Human fix"),
    ]
    r = ws.detect_shapes(steps)
    assert ws.TRIAGE in r.tags, r.tags
    assert ws.HUMAN_CHECKPOINT in r.tags, r.tags
    assert ws.GATED not in r.tags, "triage must suppress the plain gated tag"
    # Priority: triage beats gated for the primary; human-checkpoint is additive.
    assert r.primary == ws.TRIAGE
    assert r.additive == [ws.HUMAN_CHECKPOINT]
    print("  (7) both slots forward to different targets -> triage (+ human additive)")


def test_same_targets_not_triage():
    # Both slots forward to the SAME target -> not a fork -> gated, not triage.
    steps = [
        _step(name="Translate"),
        _step(
            step_type=StepType.LQA,
            name="Check",
            key="c",
            branch={"on_pass": "next", "on_fail": "next"},
        ),
    ]
    r = ws.detect_shapes(steps)
    assert ws.TRIAGE not in r.tags, r.tags
    assert r.primary == ws.GATED
    print("  (8) both slots -> same target -> not triage (gated)")


def test_human_checkpoint_additive_only():
    # A plain pipeline that ends in a human review: straight-through primary, human chip.
    steps = [_step(name="Translate"), _step(step_type=StepType.HUMAN_REVIEW, name="Review")]
    r = ws.detect_shapes(steps)
    assert r.tags == frozenset({ws.STRAIGHT_THROUGH, ws.HUMAN_CHECKPOINT}), r.tags
    assert r.primary == ws.STRAIGHT_THROUGH  # human-checkpoint is never primary
    assert r.ordered_tags == [ws.STRAIGHT_THROUGH, ws.HUMAN_CHECKPOINT]
    print("  (9) human_review present -> additive human-checkpoint, never primary")


def test_dangling_branch_key_never_raises():
    # A branch naming a key NO step carries (renamed/deleted target): treat as no edge.
    steps = [
        _step(name="Translate"),
        _step(step_type=StepType.LQA, name="Check", key="c", branch={"on_fail": {"step": "ghost"}}),
    ]
    r = ws.detect_shapes(steps)  # must not raise
    assert ws.CORRECTION_LOOP not in r.tags, "dangling key must NOT create a loop"
    assert r.primary == ws.GATED, r.tags  # branch present, but no resolvable edge
    # Junk shapes anywhere must also be swallowed.
    junk = [_step(branch={"on_pass": 12345, "on_fail": ["bad"]}), _step(branch="not-a-dict")]
    assert ws.detect_shapes(junk).primary in (ws.GATED, ws.STRAIGHT_THROUGH)
    print("  (10) dangling/renamed key + junk branch -> no edge, never raises")


def test_priority_loop_beats_triage():
    # A workflow that is BOTH a triage fork AND a correction loop -> primary loop.
    steps = [
        _step(name="Translate"),
        _step(name="Fix", key="fix"),
        _step(
            step_type=StepType.LQA,
            name="Route",
            key="r",
            branch={"on_pass": "next", "on_fail": {"step": "fix"}},
        ),
        # a second gate that forks forward (triage)
        _step(
            step_type=StepType.LQA,
            name="Route2",
            key="r2",
            branch={"on_pass": "end", "on_fail": "next"},
        ),
        _step(name="Tail"),
    ]
    r = ws.detect_shapes(steps)
    assert ws.CORRECTION_LOOP in r.tags and ws.TRIAGE in r.tags, r.tags
    assert r.primary == ws.CORRECTION_LOOP, "loop outranks triage in the priority"
    assert r.sorted_tags() == sorted([ws.CORRECTION_LOOP, ws.TRIAGE])
    print("  (11) correction-loop + triage -> primary is correction-loop (priority)")


def test_orm_rows_classify_identically():
    # The SAME topology as the correction-loop dict case, but built from ORM rows.
    steps = [
        WorkflowStep(order_index=0, step_type=StepType.LLM_AGENT, name="Translate"),
        WorkflowStep(order_index=1, step_type=StepType.LLM_AGENT, name="Fix", key="fix"),
        WorkflowStep(
            order_index=2,
            step_type=StepType.LQA,
            name="LQA",
            key="lqa",
            branch={"on_pass": "end", "on_fail": {"step": "fix"}},
            max_visits=3,
        ),
    ]
    r = ws.detect_shapes(steps)
    assert r.primary == ws.CORRECTION_LOOP, r.tags
    # qe_gate on an ORM llm_agent -> gated, matching a dict with the same flag.
    q = [
        WorkflowStep(
            order_index=0,
            step_type=StepType.LLM_AGENT,
            name="MT",
            qe_gate=True,
            qe_threshold=85,
            key="mt",
            branch={"on_pass": "end"},
        )
    ]
    assert ws.detect_shapes(q).primary == ws.GATED
    print("  (12) ORM WorkflowStep rows classify identically to draft dicts")


def test_headline_generation():
    # Correction loop headline names the loop-back target and re-check; no "agent".
    steps = [
        _step(name="Translate"),
        _step(name="Fix", key="fix"),
        _step(
            step_type=StepType.LQA,
            name="LQA",
            key="lqa",
            branch={"on_pass": "end", "on_fail": {"step": "fix"}},
        ),
    ]
    h = ws.shape_headline(steps, arrow=" -> ")
    assert h.startswith("This is a correction loop:"), h
    assert "loops back to Fix and re-checks" in h, h
    assert "agent" not in h.lower(), ("positioning guard: never 'agent'", h)

    # QE gate threshold surfaces in the chain.
    g = [_step(name="MT", qe_gate=True, qe_threshold=90, key="mt", branch={"on_pass": "end"})]
    hg = ws.shape_headline(g, arrow=" -> ")
    assert "gate at 90%" in hg, hg
    assert hg.startswith("This is a gated pipeline:"), hg

    # Straight-through + human checkpoint + fan-out extras.
    st = [_step(name="Translate"), _step(step_type=StepType.HUMAN_REVIEW, name="Review")]
    hs = ws.shape_headline(st, arrow=" -> ", n_locales=4)
    assert hs.startswith("This is a straight-through pipeline:"), hs
    # QA-10 (#40): 4 target locales == a base + 3 VARIANTS; the headline counts variants
    # (n_locales - 1), the SAME number the review screen's fan-out rows + confirm button
    # show (ml_variant_count), never the raw locale count.
    assert "a human checkpoint" in hs and "3 language variants" in hs, hs
    assert "4 language variants" not in hs, (
        "headline must count variants (base + n-1), not raw locales",
        hs,
    )

    # Empty steps degrade gracefully.
    assert "no steps yet" in ws.shape_headline([])

    # Unnamed steps fall back to role verbs (never "agent").
    verbs = ws.shape_headline(
        [
            _step(step_type=StepType.LLM_AGENT),
            _step(step_type=StepType.LQA),
            _step(step_type=StepType.HUMAN_REVIEW),
        ],
        arrow=" -> ",
    )
    assert "Translate" in verbs and "Check" in verbs and "Review" in verbs, verbs
    assert "agent" not in verbs.lower(), verbs
    print("  (13) headlines: loop/gate/straight-through, extras, ASCII arrow, no 'agent'")


def test_labels_cover_every_tag():
    for slug in (
        ws.STRAIGHT_THROUGH,
        ws.GATED,
        ws.CORRECTION_LOOP,
        ws.TRIAGE,
        ws.HUMAN_CHECKPOINT,
        ws.FAN_OUT,
    ):
        assert ws.SHAPE_LABELS.get(slug)
        assert ws.SHAPE_HINTS.get(slug)
    # detect_shapes NEVER emits fan-out (not a step property).
    assert ws.FAN_OUT not in ws.detect_shapes([_step(step_type=StepType.HUMAN_REVIEW)]).tags
    print("  (14) every tag has a label + hint; detect_shapes never emits fan-out")


_AST_CACHE = {}  # (repo-relative posix path, source bytes) -> ast.Module, once per run


def _cached_tree(rel, source):
    # Issue #330: parse each source file at most once per suite run. Keyed by
    # content as well as path so a mutated file (the self-test swaps one file's
    # bytes per case) reparses alone and unchanged files reuse their AST.
    key = (rel, source)
    tree = _AST_CACHE.get(key)
    if tree is None:
        tree = ast.parse(source)
        _AST_CACHE[key] = tree
    return tree


def _created_via_stamps_written(root=_ROOT, overrides=None):
    """Scan writes/defaults, loading constant definitions as ASTs, never imports.

    Unresolved stamps fail with their own source location. The only exceptions
    are the exact documented passthroughs below, not arbitrary parameters/getattr.
    `overrides` maps repo-relative posix paths to source bytes that replace (or
    add) files in memory, so the mutation self-test never copies a source tree.
    """
    root = Path(root)
    overrides = overrides or {}
    modules = {}

    def file_source(rel):
        source = overrides.get(rel)
        if source is not None:
            return source
        path = root / rel
        return path.read_bytes() if path.is_file() else None

    def module_bindings(module):
        if module in modules:
            return modules[module]
        bindings = {}
        modules[module] = bindings
        stem = module.replace(".", "/")
        rel = f"{stem}.py"
        source = file_source(rel)
        if source is None:
            rel = f"{stem}/__init__.py"
            source = file_source(rel)
            if source is None:
                return bindings
        package = module.split(".") if rel.endswith("__init__.py") else module.split(".")[:-1]
        for node in _cached_tree(rel, source).body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        bindings[target.id] = ("expr", node.value)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    bindings[alias.asname or alias.name.split(".")[0]] = (
                        "import",
                        alias.name if alias.asname else alias.name.split(".")[0],
                    )
            elif isinstance(node, ast.ImportFrom):
                prefix = package[: len(package) - node.level + 1] if node.level else []
                origin = ".".join(prefix + ([node.module] if node.module else []))
                for alias in node.names:
                    bindings[alias.asname or alias.name] = ("import", f"{origin}.{alias.name}")
        return bindings

    def resolve(expr, module, seen=frozenset()):
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return expr.value
        parts = []
        while isinstance(expr, ast.Attribute):
            parts.insert(0, expr.attr)
            expr = expr.value
        if not isinstance(expr, ast.Name):
            return None
        key = (module, expr.id, tuple(parts))
        if key in seen:
            return None
        binding = module_bindings(module).get(expr.id)
        if binding is None:
            return None
        kind, value = binding
        if kind == "expr":
            return resolve(value, module, seen | {key}) if not parts else None
        dotted = ".".join([value, *parts])
        origin, _, name = dotted.rpartition(".")
        return resolve(ast.Name(id=name), origin, seen | {key})

    passthroughs = {
        # Clone copies source provenance, including legacy NULL; no new stamp.
        ("app/services/workflows_common.py", "clone_workflow"): "getattr(src, 'created_via', None)",
        # Locale variants copy this local; its source assignment is checked below.
        ("app/services/workflows_part5.py", "copy_to_pairs"): "src_created_via",
        # Forwarded parameter; its default and callers' explicit values are scanned.
        ("app/services/workflows_part4.py", "create_workflow"): "created_via",
    }
    written = {}
    unresolved = []
    for root_rel in ("app", "scripts", "alembic", "integrations"):
        rels = {p.relative_to(root).as_posix() for p in (root / root_rel).rglob("*.py")}
        rels.update(rel for rel in overrides if rel.partition("/")[0] == root_rel)
        for rel in sorted(rels):
            source = file_source(rel)
            if source is None or b"created_via" not in source:
                # Every recorded write spells created_via in its own file. Known limit (#330
                # gate N1): a name split by implicit concatenation ('created_' 'via') or
                # written with escapes skips this byte prefilter; the full AST walk would
                # catch it but costs ~30 s per run. Treat such spellings as review findings.
                continue
            module = rel.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
            tree = _cached_tree(rel, source)

            def record(expr, scope, *, default=False, module=module, rel=rel):
                stamp = resolve(expr, module)
                if stamp is not None:
                    written.setdefault(stamp, f"{rel}:{expr.lineno}")
                    return
                expected = passthroughs.get((rel, getattr(scope, "name", "")))
                if not default and expected and ast.unparse(expr) == expected:
                    if expected == "src_created_via":
                        assignments = [
                            n
                            for n in ast.walk(scope)
                            if isinstance(n, ast.Assign)
                            and any(isinstance(t, ast.Name) and t.id == expected for t in n.targets)
                        ]
                        if (
                            len(assignments) != 1
                            or ast.unparse(assignments[0].value)
                            != "getattr(src, 'created_via', None)"
                        ):
                            # Name the assignment line (the tampered line), not
                            # the use line the passthrough expression sits on.
                            line = assignments[0].lineno if assignments else expr.lineno
                            unresolved.append(f"{rel}:{line}: changed source stamp passthrough")
                    return
                unresolved.append(f"{rel}:{expr.lineno}: {ast.unparse(expr)}")

            def visit(node, scope=None):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    scope = node
                    args = node.args
                    positional = args.posonlyargs + args.args
                    defaults = (
                        list(zip(positional[-len(args.defaults) :], args.defaults, strict=True))
                        if args.defaults
                        else []
                    )
                    defaults += list(zip(args.kwonlyargs, args.kw_defaults, strict=True))
                    for arg, value in defaults:
                        if arg.arg == "created_via" and value is not None:
                            record(value, scope, default=True)
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg == "created_via":
                            record(kw.value, scope)
                        elif kw.arg is None and isinstance(kw.value, ast.Dict):
                            for key, value in zip(kw.value.keys, kw.value.values, strict=True):
                                if isinstance(key, ast.Constant) and key.value == "created_via":
                                    record(value, scope)
                    if (
                        isinstance(node.func, ast.Name)
                        and node.func.id == "setattr"
                        and len(node.args) == 3
                    ):
                        key = node.args[1]
                        if isinstance(key, ast.Constant) and key.value == "created_via":
                            record(node.args[2], scope)
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and target.attr == "created_via"
                            and node.value is not None
                        ):
                            record(node.value, scope)
                for child in ast.iter_child_nodes(node):
                    visit(child, scope)

            visit(tree)
    assert not unresolved, "Unresolved created_via stamps: " + "; ".join(unresolved)
    return written


def test_origin_labels_cover_every_written_stamp(root=_ROOT, overrides=None):
    # Issue #183: a created_via stamp written anywhere but missing from ORIGIN_LABELS
    # renders origin=unknown on the CLI and no chip in the web UI. The written set is
    # scanned from writes/defaults in app/, scripts/, alembic/ and integrations/,
    # resolving imported constants such as SHOWCASE_MARKER, so a new stamp
    # without a label goes THIS red instead of shipping an invisible provenance.
    written = _created_via_stamps_written(root, overrides)
    assert written, "the scan found no created_via writes -- the scanner broke"
    # Sanity: the scan really sees the known stamps through their constants (a scan
    # that silently matched nothing would pass the loop below vacuously).
    assert "showcase-seed" in written, sorted(written)
    missing = sorted(
        f"{stamp} (written at {written[stamp]})"
        for stamp in written
        if stamp not in ws.ORIGIN_LABELS
    )
    assert not missing, f"created_via stamps with no ORIGIN_LABELS entry: {', '.join(missing)}"
    stamps = ", ".join(sorted(written))
    print(f"  (15) every written created_via stamp ({stamps}) has an ORIGIN_LABELS entry")


def test_origin_label_showcase_seed_reads_the_label():
    # Defect 4 (unit half): the showcase stamp maps to its human label, not None.
    assert ws.origin_label("showcase-seed") == "seeded for the showcase demo"
    print("  (16) origin_label('showcase-seed') -> 'seeded for the showcase demo'")


def test_stamp_scan_mutations_in_memory():
    # Mutate the scan in memory (source overrides), never the checkout or
    # executable modules; with the AST cache each case reparses only its probe.
    constants_rel = "app/services/_origin_probe_constants.py"
    probe_rel = "app/services/_origin_probe.py"
    cases = {
        "imported": ("from app.services._origin_probe_constants import STAMP\n", "STAMP"),
        "module": (
            "import app.services._origin_probe_constants\n",
            "app.services._origin_probe_constants.STAMP",
        ),
        "alias": ("import app.services._origin_probe_constants as stamps\n", "stamps.STAMP"),
        "relative": ("from ._origin_probe_constants import STAMP as VIA\n", "VIA"),
        "annotated": ("STAMP: str = 'probe-stamp'\n", "STAMP"),
        "dict-splat": ("", "**{'created_via': 'probe-stamp'}"),
        "conditional": ("", "'manual' if flag else 'draft'"),
        "unknown": ("", "unknown_stamp"),
    }
    for case, (prefix, expression) in cases.items():
        argument = expression if case == "dict-splat" else f"created_via={expression}"
        overrides = {
            constants_rel: b"STAMP: str = 'probe-stamp'\n",
            probe_rel: f"{prefix}Workflow(\n    {argument},\n)\n".encode(),
        }
        location = f"app/services/_origin_probe.py:{prefix.count(chr(10)) + 2}"
        try:
            test_origin_labels_cover_every_written_stamp(overrides=overrides)
        except AssertionError as exc:
            message = str(exc)
            assert location in message, message
            if case in ("conditional", "unknown"):
                assert "Unresolved created_via stamps:" in message, message
            else:
                assert "probe-stamp (written at " in message, message
            print(f"collected ids: origin mutation {case}: {message}")
        else:
            raise AssertionError(f"{case} mutation passed silently")
    # Mutate the real helper's default, which must be scanned independently
    # of the allowlisted parameter forwarding expression in its body.
    helper_rel = "app/services/workflows_part4.py"
    original = (Path(_ROOT) / helper_rel).read_bytes()
    assert b'created_via: str = "manual"' in original
    overrides = {
        helper_rel: original.replace(
            b'created_via: str = "manual"', b'created_via: str = "hand-built"'
        )
    }
    try:
        test_origin_labels_cover_every_written_stamp(overrides=overrides)
    except AssertionError as exc:
        message = str(exc)
        assert "hand-built (written at app/services/workflows_part4.py:" in message, message
        print(f"collected ids: origin mutation default: {message}")
    else:
        raise AssertionError("default mutation passed silently")
    print("  (17) in-memory mutations: constants/splat/default detected; unknown expressions fail")


def main():
    test_empty_steps_is_straight_through()
    test_straight_through()
    test_gated_via_run_condition()
    test_gated_via_forward_branch_end()
    test_correction_loop()
    test_self_loop_is_correction_loop()
    test_triage_binary_fork()
    test_same_targets_not_triage()
    test_human_checkpoint_additive_only()
    test_dangling_branch_key_never_raises()
    test_priority_loop_beats_triage()
    test_orm_rows_classify_identically()
    test_headline_generation()
    test_labels_cover_every_tag()
    test_origin_labels_cover_every_written_stamp()
    test_origin_label_showcase_seed_reads_the_label()
    test_stamp_scan_mutations_in_memory()
    print("WORKFLOW SHAPES OK")


if __name__ == "__main__":
    main()
