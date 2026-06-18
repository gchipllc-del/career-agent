"""
Autonomous Career Agent — hardened LangGraph engine.

Builds on the corrected reference (grounded tailoring/validation + retry
breaker + working human-in-the-loop) and adds:

  SANITIZE node  prompt-injection defense for scraped postings (Module 1 from
                 the spec, which the original never implemented): heuristic scan
                 + an unguessable fence + a fence-aware tailoring prompt.
  MOCK fallback  runs with no OPENAI_API_KEY / no FIRECRAWL_API_KEY so the whole
                 graph is demoable offline; the security + validation logic stays
                 fully live (it's deterministic, in core.py).
  SERVER hooks   run_to_gate() / resume() so a web backend can drive the graph.

Security-critical logic lives in core.py and is shared with the dependency-free
demo pipeline, so both engines enforce identical rules.

Run (CLI):  OPENAI_API_KEY=... FIRECRAWL_API_KEY=... python career_agent.py
"""

import os
import uuid
from typing import Dict, Any, List, TypedDict

import localenv
localenv.load()  # apply a personal .env before provider settings are read

import core

# Heavy, optional deps — guarded so the module imports even when they're absent
# (the server then falls back to core.run_pipeline).
try:
    from langchain_openai import ChatOpenAI
except Exception:  # pragma: no cover
    ChatOpenAI = None

try:
    # Native Anthropic client for Claude (wraps the official `anthropic` SDK —
    # NOT an OpenAI-compatible shim).
    from langchain_anthropic import ChatAnthropic
except Exception:  # pragma: no cover
    ChatAnthropic = None

try:
    from firecrawl import Firecrawl
except Exception:  # pragma: no cover
    Firecrawl = None

from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import InMemorySaver

# --- provider selection -----------------------------------------------------
# Pick a backend with LLM_PROVIDER. Three notable modes:
#   • anthropic  -> Claude Opus (personal, highest quality, PAID). Uses the
#                   native Anthropic client; needs ANTHROPIC_API_KEY.
#   • ollama     -> a local model on THIS machine, NO API key. Install Ollama
#                   and `ollama pull llama3.2`. Good for "anyone can run it."
#   • (no key)   -> zero-dependency MOCK mode that anyone can run with nothing.
# Hosted OpenAI-compatible free tiers (groq/openrouter/cerebras) also supported.
#   LLM_PROVIDER = anthropic | ollama | groq | openrouter | cerebras | openai
#   LLM_MODEL    = override the model id for the chosen provider
#   ANTHROPIC_API_KEY (anthropic)  /  OPENAI_API_KEY|LLM_API_KEY (the rest)
OPENAI_COMPAT = {
    "groq":       ("https://api.groq.com/openai/v1",  "llama-3.3-70b-versatile"),
    "openrouter": ("https://openrouter.ai/api/v1",    "deepseek/deepseek-chat-v3:free"),
    "cerebras":   ("https://api.cerebras.ai/v1",      "llama-3.1-8b"),
    "openai":     ("https://api.openai.com/v1",       "gpt-4o"),
    "ollama":     ("http://localhost:11434/v1",       "llama3.2"),  # local, no API key
}
_provider = os.getenv("LLM_PROVIDER", "groq").lower()
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")

# NOTE: validate_payload_node uses the DEFAULT with_structured_output()
# (tool-calling), which is the reliable structured-output seam on every backend
# here (Claude tool-use, Groq 70B, Ollama). Do NOT switch it to
# method="json_schema"/strict on Groq gpt-oss — that path is broken (LangChain #34155).


def _build_llm():
    """Return (llm_or_None, provider_label, model_id). None => MOCK mode."""
    # Claude Opus via the native Anthropic client (not an OpenAI shim).
    if _provider == "anthropic":
        model = os.getenv("LLM_MODEL", "claude-opus-4-8")
        key = os.getenv("ANTHROPIC_API_KEY")
        if ChatAnthropic is None or not key:
            return None, "anthropic", model
        # Opus 4.8/4.7 reject temperature/top_p/top_k (HTTP 400) — never pass them.
        # max_tokens is required by the Anthropic API; size it for a full resume.
        return (ChatAnthropic(model=model, max_tokens=8192, timeout=120, api_key=key),
                "anthropic", model)
    # Local Ollama — OpenAI-compatible endpoint, needs NO API key.
    if _provider == "ollama":
        base, dm = OPENAI_COMPAT["ollama"]
        model = os.getenv("LLM_MODEL", dm)
        if ChatOpenAI is None:
            return None, "ollama", model
        return (ChatOpenAI(model=model, base_url=base, api_key="ollama", temperature=0),
                "ollama", model)
    # Hosted OpenAI-compatible providers — need a key.
    base, dm = OPENAI_COMPAT.get(_provider, OPENAI_COMPAT["groq"])
    model = os.getenv("LLM_MODEL", dm)
    if ChatOpenAI is None or not LLM_API_KEY:
        return None, _provider, model
    return (ChatOpenAI(model=model, base_url=base, api_key=LLM_API_KEY, temperature=0),
            _provider, model)


LLM, ACTIVE_PROVIDER, ACTIVE_MODEL = _build_llm()
MOCK_MODE = LLM is None


class ApplicationState(TypedDict):
    job_url: str
    master_resume: str
    scraped_job_spec: str
    safe_job_spec: str          # fenced, injection-neutralized
    job_fence: str              # per-run sentinel (canary leak tripwire)
    injection_flags: List[str]  # heuristic hits (advisory)
    tailored_resume: str
    validation_status: str
    audit_notes: str
    coverage: Dict[str, Any]    # JD keyword gap analysis (advisory)
    retry_count: int


# --- nodes ------------------------------------------------------------------

def scrape_job_node(state: ApplicationState) -> Dict[str, Any]:
    # Job text pasted directly? Use it as-is (no scrape, no key needed).
    if state.get("scraped_job_spec"):
        return {}
    url = state.get("job_url", "").strip()
    if not url or Firecrawl is None or not os.getenv("FIRECRAWL_API_KEY"):
        return {"scraped_job_spec": "(no job description provided)"}
    result = Firecrawl(api_key=os.getenv("FIRECRAWL_API_KEY")).scrape(url)
    # Firecrawl has returned both dict-like and Document objects across versions.
    markdown = (
        result.get("markdown", "")
        if isinstance(result, dict)
        else getattr(result, "markdown", "") or ""
    )
    return {"scraped_job_spec": markdown}


def sanitize_input_node(state: ApplicationState) -> Dict[str, Any]:
    """Module 1: neutralize prompt injection before the posting reaches the LLM."""
    raw = state.get("scraped_job_spec", "") or ""
    safe_spec, sentinel = core.build_safe_spec(raw)
    return {
        "safe_job_spec": safe_spec,
        "job_fence": sentinel,
        "injection_flags": core.scan_injection(raw),
    }


def tailor_materials_node(state: ApplicationState) -> Dict[str, Any]:
    attempt = state.get("retry_count", 0)

    # On a retry, feed the prior failure back so the model repairs the specific
    # problem instead of re-rolling blind.
    feedback = ""
    if attempt > 0 and state.get("audit_notes"):
        feedback = (
            "\n\nYour previous attempt FAILED validation. Correct exactly these "
            "issues and change nothing else:\n" + state["audit_notes"]
        )

    if MOCK_MODE:
        tailored = core.mock_tailor(state["master_resume"], feedback)
    else:
        prompt = core.tailoring_prompt(
            state["safe_job_spec"], state["master_resume"], feedback
        )
        tailored = LLM.invoke(prompt).content

    return {"tailored_resume": tailored, "retry_count": attempt + 1}


def validate_payload_node(state: ApplicationState) -> Dict[str, Any]:
    master, tailored = state["master_resume"], state["tailored_resume"]
    determ_flags = core.fabrication_flags(master, tailored)

    # Deterministic hard-fail: a leaked fence sentinel means the model copied the
    # fence / was steered by injected text. No LLM needed — fail closed.
    if core.canary_leaked(tailored, state.get("job_fence", "")):
        return {
            "validation_status": "FAILED",
            "audit_notes": "Prompt-leak tripwire: tailored output echoed the fence sentinel.",
        }

    if MOCK_MODE:
        passed = not determ_flags
        reasoning = (
            "No fabricated facts detected (deterministic check)."
            if passed else "Tailored resume introduced numbers absent from the master."
        )
    else:
        class VerificationSchema(BaseModel):
            passed: bool = Field(
                description="True ONLY if the tailored resume invents no fact absent from the master."
            )
            reasoning: str = Field(
                description="Name any fabricated employer, title, date, degree, or metric — or confirm none."
            )

        # include_raw=True so a malformed/empty tool call surfaces as
        # parsing_error / parsed=None instead of crashing on result.passed.
        structured_llm = LLM.with_structured_output(VerificationSchema, include_raw=True)
        audit_prompt = (
            "Compare the TAILORED resume against the MASTER resume. FAIL if the "
            "tailored version introduces ANY employer, title, date, degree, "
            "certification, or quantified metric not present or directly "
            "supported in the master. Rewording is fine; new facts are not.\n\n"
            f"=== MASTER RESUME ===\n{master}\n\n"
            f"=== TAILORED RESUME ===\n{tailored}\n\n"
            f"=== PRE-SCAN: numbers in tailored but not master ===\n"
            f"{determ_flags or 'none'}"
        )
        result = structured_llm.invoke(audit_prompt)
        parsed = result.get("parsed") if isinstance(result, dict) else result
        parse_err = result.get("parsing_error") if isinstance(result, dict) else None
        if parsed is None or parse_err is not None:
            # Fail CLOSED — never auto-pass when the validator's own response is
            # unparseable. The deterministic flags still inform the human.
            note = "Validator response could not be parsed; failing closed for safety."
            if determ_flags:
                note = f"[guard] new numbers not in master: {determ_flags}. {note}"
            return {"validation_status": "FAILED", "audit_notes": note}
        passed, reasoning = parsed.passed, parsed.reasoning

    # Deterministic guard is AUTHORITATIVE on both the LLM and MOCK paths: a
    # tailored resume that introduces numbers absent from the master FAILS even
    # if the LLM judge was lenient. Fails safe — the retry loop and human gate
    # handle borderline cases, and the tailoring prompt forbids inventing numbers
    # in the first place, so a legitimate rewrite should never trip this.
    notes = reasoning
    if determ_flags:
        passed = False
        notes = f"[guard] new numbers not in master: {determ_flags}. {reasoning}"
    return {
        "validation_status": "PASSED" if passed else "FAILED",
        "audit_notes": notes,
        "coverage": core.jd_coverage(state.get("scraped_job_spec", ""), tailored),
    }


def manual_review_node(state: ApplicationState) -> Dict[str, Any]:
    # Terminal landing pad when tailoring can't pass validation. Never submits.
    return {"validation_status": "NEEDS_MANUAL_REVIEW"}


# --- routing ----------------------------------------------------------------

def route_after_validation(state: ApplicationState) -> str:
    if state["validation_status"] == "PASSED":
        return "human_approval_gate"
    if state.get("retry_count", 0) >= core.MAX_TAILORING_ATTEMPTS:
        return "manual_review"
    return "tailor_materials"


# --- graph ------------------------------------------------------------------

workflow = StateGraph(ApplicationState)
workflow.add_node("scrape_job", scrape_job_node)
workflow.add_node("sanitize_input", sanitize_input_node)
workflow.add_node("tailor_materials", tailor_materials_node)
workflow.add_node("validate_payload", validate_payload_node)
workflow.add_node("manual_review", manual_review_node)
workflow.add_node("human_approval_gate", lambda s: {})

workflow.set_entry_point("scrape_job")
workflow.add_edge("scrape_job", "sanitize_input")
workflow.add_edge("sanitize_input", "tailor_materials")
workflow.add_edge("tailor_materials", "validate_payload")
workflow.add_conditional_edges(
    "validate_payload",
    route_after_validation,
    {
        "human_approval_gate": "human_approval_gate",
        "tailor_materials": "tailor_materials",
        "manual_review": "manual_review",
    },
)
workflow.add_edge("manual_review", END)
workflow.add_edge("human_approval_gate", END)

def _make_checkpointer():
    """Durable SqliteSaver when langgraph-checkpoint-sqlite is installed, so a
    pending human approval survives a process restart; else in-memory (keeps the
    zero-extra-dependency path working). ThreadingHTTPServer handles each request
    on a different thread, so the connection MUST use check_same_thread=False —
    the saver serializes access with its own lock."""
    try:
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver
        conn = sqlite3.connect(os.getenv("CHECKPOINT_DB", "checkpoints.sqlite"),
                               check_same_thread=False)
        saver = SqliteSaver(conn)
        try:
            saver.setup()  # idempotent table creation on older versions
        except Exception:
            pass
        return saver
    except Exception:
        return InMemorySaver()


checkpointer = _make_checkpointer()
app = workflow.compile(
    checkpointer=checkpointer,
    interrupt_before=["human_approval_gate"],
)


# --- server entry points (used by server.py when LangGraph is installed) ----

# Graph node -> dashboard stage name, for streaming progress.
_NODE_STAGE = {
    "scrape_job": "scrape",
    "sanitize_input": "sanitize",
    "tailor_materials": "tailor",
    "validate_payload": "validate",
}


def run_to_gate_events(run_id: str, master_resume: str, job_text: str = "", job_url: str = ""):
    """Streaming form: drives the graph and yields per-node progress events as
    each node completes (stream_mode='updates'), then a final
    {'event':'done','state':...}. run_id is the thread_id, so resume(run_id)
    works afterward. Stops at the human-approval interrupt."""
    config = {"configurable": {"thread_id": run_id}}
    initial: ApplicationState = {
        "job_url": job_url,
        "master_resume": master_resume,
        "scraped_job_spec": job_text.strip(),
        "safe_job_spec": "",
        "job_fence": "",
        "injection_flags": [],
        "tailored_resume": "",
        "validation_status": "",
        "audit_notes": "",
        "coverage": {},
        "retry_count": 0,
    }
    for chunk in app.stream(initial, config, stream_mode="updates"):
        for node in chunk:  # chunk: {node_name: state_delta}
            stage = _NODE_STAGE.get(node)
            if stage:
                yield {"event": "stage", "stage": stage, "status": "done"}
    state = dict(app.get_state(config).values)
    state["engine"] = "langgraph-mock" if MOCK_MODE else "langgraph"
    yield {"event": "done", "state": state}


def run_to_gate(master_resume: str, job_text: str = "", job_url: str = ""):
    """Non-streaming form — drains run_to_gate_events. Returns (run_id, state)."""
    run_id = uuid.uuid4().hex[:12]
    state = None
    for ev in run_to_gate_events(run_id, master_resume, job_text, job_url):
        if ev.get("event") == "done":
            state = ev["state"]
    return run_id, state


def resume(run_id: str):
    """Resume an approved run past the gate to completion (where real submission
    would fire). Returns the final state."""
    config = {"configurable": {"thread_id": run_id}}
    for _ in app.stream(None, config):
        pass
    return dict(app.get_state(config).values)


# --- CLI driver -------------------------------------------------------------

if __name__ == "__main__":
    sample_master = (
        "Jane Doe — Software Engineer\n"
        "Acme Corp (2019-2024): Built payment services in Python; cut p99 latency 40%.\n"
        "B.S. Computer Science, State University, 2019."
    )
    run_id, state = run_to_gate(
        sample_master,
        job_text="Senior Python Engineer. Payments experience required.",
    )
    print(f"ENGINE   : {state.get('engine')}")
    print(f"INJECTION: {state.get('injection_flags') or 'clean'}")
    print(f"VALIDATION: {state.get('validation_status')}  (attempts: {state.get('retry_count')})")
    print(f"AUDIT    : {state.get('audit_notes')}")
    print("\n--- TAILORED RESUME (review before submit) ---\n")
    print(state.get("tailored_resume"))

    if state.get("validation_status") == "PASSED":
        if input("\nSubmit this application? [y/N] ").strip().lower() == "y":
            resume(run_id)
            print("Submitted.")
        else:
            print("Aborted by human.")
    else:
        print("Not submitted — escalated for manual review.")
