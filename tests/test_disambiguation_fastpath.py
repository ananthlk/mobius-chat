"""Disambiguation fast-path (2026-08-17, Ananth directly), per
docs/DISAMBIGUATION_FASTPATH_CONTRACT.md.

Three pieces, three test classes:
1. TestValidateDisambiguationBlock -- the new "disambiguation" case in
   assistant_envelope.py's _validate_ui_block (§1/§2 of the contract).
2. TestReactNeedsDisambiguationFlag -- react_loop.py's _finalize_response
   code-detecting an unresolved multi-candidate ambiguity from
   ctx.react_document_download_data (fetch_document's own existing
   signal), NOT a model-emitted field.
3. TestIntegrateDisambiguationBypass -- integrate.py's run_integrate
   skipping Call A/B/C and emitting the lean envelope + disambiguation
   UI block when the flag is set.
"""
from __future__ import annotations

import json
import os
from unittest.mock import patch

from app.communication.assistant_envelope import _validate_ui_block
from app.pipeline.context import PipelineContext
from app.planner.schemas import Plan, SubQuestion
from app.stages.integrate import run_integrate


class TestValidateDisambiguationBlock:
    def test_valid_block_passes_through(self):
        block = {
            "type": "disambiguation",
            "select_kind": "document",
            "query": "Molina provider manual",
            "candidates": [
                {
                    "id": "doc_abc123",
                    "title": "Molina Healthcare Provider Manual 2024",
                    "meta": {"payer": "Molina", "state": "FL", "download_url": "https://x/y"},
                },
                {"title": "missing id — dropped"},
                {"id": "doc_def456", "title": "missing everything else — still valid, id+title is the floor"},
            ],
        }
        out = _validate_ui_block(block, max_source_index=0)
        assert out is not None
        assert out["type"] == "disambiguation"
        assert out["select_kind"] == "document"
        assert out["query"] == "Molina provider manual"
        assert len(out["candidates"]) == 2  # the id-less one dropped
        c0 = out["candidates"][0]
        assert c0["id"] == "doc_abc123"
        assert c0["title"] == "Molina Healthcare Provider Manual 2024"
        assert c0["meta"]["payer"] == "Molina"
        assert c0["meta"]["download_url"] == "https://x/y"

    def test_unknown_select_kind_still_renders_generic(self):
        """Contract §1: 'Unknown kinds render fine (generic treatment).'
        No enum restriction -- select_kind is just required to be a
        non-empty string."""
        block = {
            "type": "disambiguation",
            "select_kind": "some_future_kind_nobody_registered_yet",
            "candidates": [{"id": "x1", "title": "Option one"}],
        }
        out = _validate_ui_block(block, max_source_index=0)
        assert out is not None
        assert out["select_kind"] == "some_future_kind_nobody_registered_yet"

    def test_rejects_empty_select_kind(self):
        assert _validate_ui_block(
            {"type": "disambiguation", "select_kind": "", "candidates": [{"id": "x", "title": "y"}]},
            max_source_index=0,
        ) is None
        assert _validate_ui_block(
            {"type": "disambiguation", "candidates": [{"id": "x", "title": "y"}]},
            max_source_index=0,
        ) is None

    def test_rejects_no_valid_candidates(self):
        assert _validate_ui_block(
            {"type": "disambiguation", "select_kind": "document", "candidates": []},
            max_source_index=0,
        ) is None
        assert _validate_ui_block(
            {"type": "disambiguation", "select_kind": "document", "candidates": [{"title": "no id"}]},
            max_source_index=0,
        ) is None
        assert _validate_ui_block(
            {"type": "disambiguation", "select_kind": "document"},
            max_source_index=0,
        ) is None

    def test_candidates_capped_at_six(self):
        block = {
            "type": "disambiguation",
            "select_kind": "document",
            "candidates": [{"id": f"id{i}", "title": f"Option {i}"} for i in range(10)],
        }
        out = _validate_ui_block(block, max_source_index=0)
        assert len(out["candidates"]) == 6

    def test_action_override_passed_through(self):
        block = {
            "type": "disambiguation",
            "select_kind": "document",
            "candidates": [
                {"id": "x1", "title": "y", "action": {"kind": "link", "url": "https://example.com"}},
            ],
        }
        out = _validate_ui_block(block, max_source_index=0)
        assert out["candidates"][0]["action"] == {"kind": "link", "url": "https://example.com"}

    def test_meta_filters_non_scalar_and_empty_values(self):
        block = {
            "type": "disambiguation",
            "select_kind": "document",
            "candidates": [
                {"id": "x1", "title": "y", "meta": {"payer": "Molina", "empty": "", "nested": {"a": 1}}},
            ],
        }
        out = _validate_ui_block(block, max_source_index=0)
        meta = out["candidates"][0].get("meta") or {}
        assert meta.get("payer") == "Molina"
        assert "empty" not in meta
        assert "nested" not in meta


class TestReactNeedsDisambiguationFlag:
    """react_loop.py's _finalize_response: ctx.react_needs_disambiguation
    is code-computed from ctx.react_document_download_data (fetch_document's
    own existing signal), checked AT FINALIZE TIME so a later resolve-by-id
    call (the model successfully disambiguating itself) correctly clears it."""

    def _make_ctx(self, **kwargs):
        import types
        defaults = dict(
            chat_mode=None, system_context=None, usages=[], seed_tool_results=None,
            react_tool_results=None, effective_message="test query", message="test query",
            correlation_id="cid", thread_id="t1", extra_out=None, merged_state=None,
            user_profile=None, react_trace_rounds=[],
        )
        defaults.update(kwargs)
        return types.SimpleNamespace(**defaults)

    def test_flag_set_when_multiple_documents_unresolved(self):
        from app.pipeline.react_loop import _finalize_response

        ctx = self._make_ctx(react_document_download_data={
            "documents": [
                {"document_id": "d1", "title": "Doc A"},
                {"document_id": "d2", "title": "Doc B"},
                {"document_id": "d3", "title": "Doc C"},
            ],
            "query": "Molina provider manual",
        })
        _finalize_response(ctx, "Which document did you mean?", [], "no_sources", None, None)
        assert ctx.react_needs_disambiguation is True

    def test_flag_not_set_when_single_document(self):
        """A confident single match (or a model-resolved-by-id follow-up
        call, which overwrites this to 1 entry) must NOT trigger the
        disambiguation fast path -- that's a real content answer."""
        from app.pipeline.react_loop import _finalize_response

        ctx = self._make_ctx(react_document_download_data={
            "documents": [{"document_id": "d1", "title": "Doc A"}],
            "query": "Molina provider manual",
        })
        _finalize_response(ctx, "Here's what page 23 says: ...", [], "corpus_only", None, None)
        assert getattr(ctx, "react_needs_disambiguation", False) is False

    def test_flag_not_set_when_no_download_data(self):
        from app.pipeline.react_loop import _finalize_response

        ctx = self._make_ctx()
        _finalize_response(ctx, "some normal answer", [], "corpus_only", None, None)
        assert getattr(ctx, "react_needs_disambiguation", False) is False

    def test_flag_not_set_when_answer_empty(self):
        from app.pipeline.react_loop import _finalize_response

        ctx = self._make_ctx(react_document_download_data={
            "documents": [{"document_id": "d1"}, {"document_id": "d2"}],
            "query": "x",
        })
        _finalize_response(ctx, "", [], "no_sources", None, None)
        assert getattr(ctx, "react_needs_disambiguation", False) is False

    def test_flag_not_set_when_documents_not_a_list(self):
        from app.pipeline.react_loop import _finalize_response

        ctx = self._make_ctx(react_document_download_data={"documents": "not a list", "query": "x"})
        _finalize_response(ctx, "some answer", [], "corpus_only", None, None)
        assert getattr(ctx, "react_needs_disambiguation", False) is False


def _make_plan() -> Plan:
    return Plan(subquestions=[SubQuestion(id="sq1", text="Which document?", kind="non_patient")])


def _make_integrate_ctx(**extra) -> PipelineContext:
    ctx = PipelineContext(
        correlation_id="disamb-cid", thread_id="disamb-thread", message="Molina provider manual pages 20-24",
        plan=_make_plan(), answers=["I found 3 documents — which did you mean?"], sources=[], usages=[],
        retrieval_signals=[],
    )
    for k, v in extra.items():
        setattr(ctx, k, v)
    return ctx


_CANDIDATES = {
    "documents": [
        {"document_id": "d1", "title": "Molina Provider Manual 2024", "payer": "Molina", "state": "FL",
         "download_url": "https://x/d1"},
        {"document_id": "d2", "title": "Molina Provider Manual 2023", "payer": "Molina", "state": "FL",
         "download_url": "https://x/d2"},
    ],
    "query": "Molina provider manual",
}


class TestIntegrateDisambiguationBypass:
    """run_integrate's early branch (before the existing dyn-enrichment
    Task #76 branch): when ctx.react_needs_disambiguation is set, skip
    Call A/B/C entirely and emit the lean §2 envelope + disambiguation
    UI block. Modeled on test_dynamic_enrichment_dispatch.py's proven
    pattern for testing this dispatch chain in isolation."""

    def test_flag_set_skips_call_a_b_c_and_emits_lean_envelope(self):
        ctx = _make_integrate_ctx(
            react_needs_disambiguation=True,
            react_document_download_data=_CANDIDATES,
        )
        with (
            patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "parallel"}),
            patch("app.stages.integrate.format_response_parallel") as mock_par,
            patch("app.stages.integrate.format_response") as mock_seq,
            patch("app.stages.integrate.run_bc_background") as mock_bg,
        ):
            run_integrate(ctx)

        mock_par.assert_not_called()
        mock_seq.assert_not_called()
        mock_bg.assert_not_called()

        payload = ctx.response_payload
        assert payload["status"] == "clarification"
        parsed_message = json.loads(payload["message"])
        assert parsed_message["direct_answer"] == "I found 3 documents — which did you mean?"

    def test_flag_set_emits_disambiguation_block_not_document_download(self):
        ctx = _make_integrate_ctx(
            react_needs_disambiguation=True,
            react_document_download_data=_CANDIDATES,
        )
        with (
            patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "parallel"}),
            patch("app.stages.integrate.format_response_parallel"),
            patch("app.stages.integrate.run_bc_background"),
        ):
            run_integrate(ctx)

        blocks = ctx.response_payload["assistant_envelope"]["blocks"]
        block_types = [b.get("type") for b in blocks]
        assert "disambiguation" in block_types
        assert "document_download" not in block_types

        disamb = next(b for b in blocks if b["type"] == "disambiguation")
        assert disamb["select_kind"] == "document"
        assert disamb["query"] == "Molina provider manual"
        ids = {c["id"] for c in disamb["candidates"]}
        assert ids == {"d1", "d2"}
        c1 = next(c for c in disamb["candidates"] if c["id"] == "d1")
        assert c1["title"] == "Molina Provider Manual 2024"
        assert c1["meta"]["payer"] == "Molina"
        assert c1["meta"]["download_url"] == "https://x/d1"
        # subtitle intentionally NOT emitted (Chat FE derives it from meta)
        assert "subtitle" not in c1

    def test_flag_unset_uses_normal_parallel_path(self):
        """Regression guard: turns that never set the flag must be
        completely unaffected -- same as before this feature existed."""
        ctx = _make_integrate_ctx()
        with (
            patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "parallel", "MOBIUS_DYNAMIC_ENRICHMENT_PCT": "0"}),
            patch("app.stages.integrate.format_response_parallel") as mock_par,
            patch("app.stages.integrate.run_bc_background") as mock_bg,
        ):
            mock_par.return_value = (
                json.dumps({"mode": "FACTUAL", "direct_answer": "x", "sections": []}),
                [{"stage": "integrator_a", "model": "m", "input_tokens": 0, "output_tokens": 0}],
            )
            run_integrate(ctx)

        mock_par.assert_called_once()
        mock_bg.assert_not_called()
        assert ctx.response_payload["status"] == "completed"

    def test_flag_set_but_single_document_data_still_bypasses_if_flag_true(self):
        """The flag itself is the authority integrate.py trusts (set by
        react_loop.py's own >1-document check) -- integrate.py doesn't
        re-derive the candidate count, it just renders whatever's there.
        A single-entry download_data with the flag set produces a
        1-candidate disambiguation block rather than silently no-op'ing;
        react_loop.py is what guarantees the flag only fires on genuine
        ambiguity (covered by TestReactNeedsDisambiguationFlag above)."""
        ctx = _make_integrate_ctx(
            react_needs_disambiguation=True,
            react_document_download_data={"documents": [_CANDIDATES["documents"][0]], "query": "x"},
        )
        with (
            patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "parallel"}),
            patch("app.stages.integrate.format_response_parallel") as mock_par,
        ):
            run_integrate(ctx)
        mock_par.assert_not_called()
        blocks = ctx.response_payload["assistant_envelope"]["blocks"]
        disamb = next(b for b in blocks if b["type"] == "disambiguation")
        assert len(disamb["candidates"]) == 1

    def test_document_id_missing_entries_dropped_from_candidates(self):
        ctx = _make_integrate_ctx(
            react_needs_disambiguation=True,
            react_document_download_data={
                "documents": [
                    {"document_id": "d1", "title": "Real doc"},
                    {"title": "no document_id — must be dropped"},
                ],
                "query": "x",
            },
        )
        with (
            patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "parallel"}),
            patch("app.stages.integrate.format_response_parallel"),
        ):
            run_integrate(ctx)
        blocks = ctx.response_payload["assistant_envelope"]["blocks"]
        disamb = next(b for b in blocks if b["type"] == "disambiguation")
        assert len(disamb["candidates"]) == 1
        assert disamb["candidates"][0]["id"] == "d1"


class TestDocumentSelectionRouting:
    """docs/DISAMBIGUATION_FASTPATH_CONTRACT.md §3/§5: a structured
    `selection` resubmit routes straight to fetch_document(document_id=
    selection.id), skipping state_load/classify/react/integrate entirely.
    "Resolution alone is the response" (Ananth, 2026-08-17) -- no
    synthesis round, reuses the existing react_bypass_integrate mechanism.
    """

    def test_document_selection_skips_pipeline_and_publishes_directly(self):
        from app.pipeline.orchestrator import run_pipeline
        from app.skills.registry import SkillEnvelope, SourceRef

        fake_env = SkillEnvelope(
            text="Here's the Molina Healthcare Provider Manual 2024.",
            sources=[SourceRef(document_name="Molina Healthcare Provider Manual 2024", document_id="doc_abc123",
                                source_type="document", page_number=None, index=1, text="manual.pdf",
                                authority="corpus")],
            signal="corpus_only",
        )

        with (
            patch("app.skills.registry.dispatch", return_value=fake_env) as mock_dispatch,
            patch("app.pipeline.orchestrator.get_queue") as mock_q,
            patch("app.pipeline.orchestrator.store_response"),
            patch("app.pipeline.react_loop.run_react") as mock_react,
        ):
            run_pipeline(
                "sel-cid", "→ Molina Healthcare Provider Manual 2024", "sel-thread",
                selection={"kind": "document", "id": "doc_abc123", "in_reply_to": "prior-cid"},
            )

        mock_react.assert_not_called()  # the whole point -- no react re-loop
        mock_dispatch.assert_called_once()
        call_arg = mock_dispatch.call_args[0][0]
        assert call_arg.name == "fetch_document"
        assert call_arg.inputs == {"document_id": "doc_abc123"}

        mock_q.return_value.publish_response.assert_called_once()
        payload = mock_q.return_value.publish_response.call_args[0][1]
        assert payload["status"] == "completed"
        assert payload["raw_text"] == "Here's the Molina Healthcare Provider Manual 2024."

    def test_document_selection_includes_download_block(self):
        from app.pipeline.orchestrator import run_pipeline
        from app.skills.registry import SkillEnvelope

        def fake_dispatch(call):
            # Mirrors what fetch_document's real resolve-by-id path does:
            # _attach_download_payload writes ctx.react_document_download_data.
            call.pipeline_ctx.react_document_download_data = {
                "documents": [{"document_id": "doc_abc123", "title": "Molina Manual", "download_url": "https://x/y"}],
                "query": "doc_abc123",
            }
            return SkillEnvelope(text="Here's the manual.", sources=[], signal="corpus_only")

        with (
            patch("app.skills.registry.dispatch", side_effect=fake_dispatch),
            patch("app.pipeline.orchestrator.get_queue") as mock_q,
            patch("app.pipeline.orchestrator.store_response"),
        ):
            run_pipeline(
                "sel-cid-2", "→ Molina Manual", "sel-thread-2",
                selection={"kind": "document", "id": "doc_abc123", "in_reply_to": "prior-cid"},
            )

        payload = mock_q.return_value.publish_response.call_args[0][1]
        blocks = payload["assistant_envelope"]["blocks"]
        dl_blocks = [b for b in blocks if b.get("type") == "document_download"]
        assert len(dl_blocks) == 1
        assert dl_blocks[0]["documents"][0]["document_id"] == "doc_abc123"

    def test_non_document_kind_does_not_trigger_selection_routing(self):
        """An unrecognized/future select_kind must not trigger the
        selection short-circuit -- it degrades to the normal pipeline.
        Asserts on the branch condition directly (does
        _run_document_selection get invoked?) rather than mocking out
        the entire normal pipeline (DB/Redis/LLM), which is exercised by
        plenty of other orchestrator tests already."""
        from app.pipeline.orchestrator import run_pipeline

        with patch("app.pipeline.orchestrator._run_document_selection") as mock_sel:
            with patch("app.pipeline.orchestrator.get_queue"), patch("app.pipeline.orchestrator.store_response"):
                with patch.dict(os.environ, {"MOBIUS_USE_REACT": "1"}, clear=False):
                    with patch("app.pipeline.react_loop.run_react"):
                        with patch("app.stages.integrate.run_integrate"):
                            run_pipeline(
                                "sel-cid-3", "some message", "sel-thread-3",
                                selection={"kind": "payer", "id": "molina", "in_reply_to": "x"},
                            )
        mock_sel.assert_not_called()

    def test_missing_id_does_not_trigger_selection_routing(self):
        from app.pipeline.orchestrator import run_pipeline

        with patch("app.pipeline.orchestrator._run_document_selection") as mock_sel:
            with patch("app.pipeline.orchestrator.get_queue"), patch("app.pipeline.orchestrator.store_response"):
                with patch.dict(os.environ, {"MOBIUS_USE_REACT": "1"}, clear=False):
                    with patch("app.pipeline.react_loop.run_react"):
                        with patch("app.stages.integrate.run_integrate"):
                            run_pipeline(
                                "sel-cid-4", "some message", "sel-thread-4",
                                selection={"kind": "document", "id": "", "in_reply_to": "x"},
                            )
        mock_sel.assert_not_called()

    def test_fetch_document_exception_publishes_failed_not_stuck(self):
        from app.pipeline.orchestrator import run_pipeline

        with (
            patch("app.skills.registry.dispatch", side_effect=RuntimeError("boom")),
            patch("app.pipeline.orchestrator.get_queue") as mock_q,
            patch("app.pipeline.orchestrator.store_response"),
        ):
            run_pipeline(
                "sel-cid-5", "→ some doc", "sel-thread-5",
                selection={"kind": "document", "id": "doc_x", "in_reply_to": "prior"},
            )

        payload = mock_q.return_value.publish_response.call_args[0][1]
        assert payload["status"] == "failed"
