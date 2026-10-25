"""
evaluate_rag.py — Ragas-based RAG evaluation script.

Evaluates the existing RAG pipeline (via the /api/chat/ endpoint) using the
Ragas 0.2.x framework with four standard metrics:
    • Context Precision  — Are relevant chunks ranked at the top?
    • Context Recall     — Does the retrieved context cover the ground truth?
    • Faithfulness       — Is the answer grounded in the retrieved context?
    • Answer Relevancy   — Does the answer address the question?

The evaluator LLM is Google Gemini (via langchain-google-genai), reusing the
same GEMINI_API_KEY already configured in .env.

Usage:
    python evaluate_rag.py <job_id>
    python evaluate_rag.py <job_id> --test-file custom_tests.json
    python evaluate_rag.py <job_id> --output results.csv
    python evaluate_rag.py <job_id> --api-url http://localhost:8000/api/chat/

Prerequisites:
    pip install ragas langchain-google-genai
    (Both are already installed in the project venv.)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from typing import Any

# Python 3.12+ / 3.14 compatibility: prevent nest_asyncio from monkey-patching
# asyncio.Task with _PyTask, which breaks asyncio.current_task() and causes
# `RuntimeError: Timeout should be used inside a task` in asyncio.timeout.
if sys.version_info >= (3, 12):
    try:
        import nest_asyncio
        nest_asyncio._patch_asyncio = lambda: None
    except ImportError:
        pass

import requests
from dotenv import load_dotenv

load_dotenv()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Gather data from the existing RAG pipeline
# ─────────────────────────────────────────────────────────────────────────────

def load_test_cases(path: str) -> list[dict[str, str]]:
    """Load evaluation test cases from a JSON file.

    Expected format: [{"question": "...", "ground_truth": "..."}, ...]
    """
    with open(path, "r") as f:
        cases = json.load(f)
    if not cases:
        print(f"Error: {path} is empty.")
        sys.exit(1)
    print(f"Loaded {len(cases)} test cases from {path}")
    return cases


def query_rag_pipeline(
    question: str,
    job_id: str,
    api_url: str,
) -> dict[str, Any]:
    """Send a question to the existing /api/chat/ endpoint.

    Returns: {"answer": str, "contexts": list[str]}
    """
    payload = {"query_id": job_id, "message": question}
    resp = requests.post(api_url, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    return {
        "answer": data.get("response", ""),
        "contexts": data.get("contexts", []),
    }


def gather_pipeline_outputs(
    test_cases: list[dict[str, str]],
    job_id: str,
    api_url: str,
) -> list[dict[str, Any]]:
    """Run every test question through the RAG pipeline and collect results."""
    results: list[dict[str, Any]] = []
    total = len(test_cases)

    print(f"\nQuerying RAG pipeline at {api_url} (job_id={job_id})...\n")
    for idx, case in enumerate(test_cases, 1):
        question = case["question"]
        ground_truth = case["ground_truth"]
        short_q = question[:60] + ("..." if len(question) > 60 else "")
        print(f"  [{idx}/{total}] {short_q}")

        try:
            output = query_rag_pipeline(question, job_id, api_url)
        except requests.exceptions.ConnectionError:
            print(f"\n❌ Error: Cannot connect to FastAPI server at {api_url}!")
            print("Please make sure Docker and the backend services are running:")
            print("    1. Start Docker Desktop")
            print("    2. Run: cd /Users/swayam/Desktop/Test && docker compose up -d\n")
            sys.exit(1)
        except Exception as e:
            print(f"    ⚠ API error: {e}")
            output = {"answer": "", "contexts": []}

        results.append({
            "question": question,
            "answer": output["answer"],
            "contexts": output["contexts"],
            "ground_truth": ground_truth,
        })

    valid_outputs = [r for r in results if r["answer"] or r["contexts"]]
    if not valid_outputs:
        print(f"\n❌ Error: No answers or contexts were retrieved for job_id '{job_id}'.")
        print("Please verify that:")
        print("  1. The job_id matches an ingested document in PostgreSQL.")
        print("  2. Docker PostgreSQL (port 5433) is running and document_chunks are populated.\n")
        sys.exit(1)

    print(f"\nCollected {len(results)} pipeline outputs.")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# 2. Build Ragas EvaluationDataset
# ─────────────────────────────────────────────────────────────────────────────

def build_ragas_dataset(results: list[dict[str, Any]]):
    """Convert pipeline outputs into a Ragas EvaluationDataset.

    Maps our fields to Ragas 0.2.x SingleTurnSample fields:
        user_input          ← question
        response            ← answer (LLM-generated)
        retrieved_contexts  ← contexts (chunks from pgvector)
        reference           ← ground_truth
    """
    from ragas import EvaluationDataset
    from ragas.dataset_schema import SingleTurnSample

    samples = []
    for r in results:
        sample = SingleTurnSample(
            user_input=r["question"],
            response=r["answer"],
            retrieved_contexts=r["contexts"] if r["contexts"] else [],
            reference=r["ground_truth"],
        )
        samples.append(sample)

    dataset = EvaluationDataset(samples=samples)
    print(f"Built Ragas EvaluationDataset with {len(samples)} samples.")
    return dataset


# ─────────────────────────────────────────────────────────────────────────────
# 3. Configure the evaluator LLM and run Ragas evaluation
# ─────────────────────────────────────────────────────────────────────────────

def get_evaluator_llm(model_name: str = "gemini-3.1-flash-lite"):
    """Create a LangChain ChatModel for Ragas to use as the evaluator LLM.

    Uses Google Gemini via langchain-google-genai, reusing the GEMINI_API_KEY
    from .env. Defaults to 'gemini-3.1-flash-lite', which has high free-tier quotas
    (1,500 requests/day vs only 20 requests/day on preview models like 3.6-flash).
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if gemini_key:
        from langchain_google_genai import ChatGoogleGenerativeAI
        from ragas.llms import LangchainLLMWrapper

        base_llm = ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=gemini_key,
            request_timeout=120,
        )
        llm = LangchainLLMWrapper(base_llm)
        print(f"Evaluator LLM: Google Gemini ({model_name})")
        return llm

    # Fallback: use Groq via OpenAI-compatible endpoint
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if groq_key:
        from langchain_openai import ChatOpenAI
        from ragas.llms import LangchainLLMWrapper

        base_llm = ChatOpenAI(
            model="llama-3.3-70b-versatile",
            api_key=groq_key,
            base_url="https://api.groq.com/openai/v1",
            temperature=0.0,
        )
        llm = LangchainLLMWrapper(base_llm)
        print("Evaluator LLM: Groq (llama-3.3-70b-versatile)")
        return llm

    print("Error: No GEMINI_API_KEY or GROQ_API_KEY found in environment.")
    sys.exit(1)


def get_evaluator_embeddings():
    """Create an embedding model for Ragas metrics that require embeddings (e.g. AnswerRelevancy).

    Uses Google Gemini embedding model (gemini-embedding-001) via langchain-google-genai,
    reusing the GEMINI_API_KEY from .env so no OpenAI credentials are required.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if gemini_key:
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        from ragas.embeddings import LangchainEmbeddingsWrapper

        base_emb = GoogleGenerativeAIEmbeddings(
            model="gemini-embedding-001",
            google_api_key=gemini_key,
        )
        embeddings = LangchainEmbeddingsWrapper(base_emb)
        print("Evaluator Embeddings: Google Gemini (gemini-embedding-001)")
        return embeddings

    return None


def run_ragas_evaluation(dataset, llm, embeddings=None, max_workers: int = 3):
    """Run Ragas evaluate() with the four standard RAG metrics.

    Metrics:
        • LLMContextPrecisionWithReference — precision of retrieval ranking
        • LLMContextRecall                 — recall of ground truth in context
        • Faithfulness                     — answer grounded in context
        • AnswerRelevancy                  — answer addresses the question (uses embeddings)
    """
    from ragas import evaluate, RunConfig
    from ragas.metrics import (
        LLMContextPrecisionWithReference,
        LLMContextRecall,
        Faithfulness,
        AnswerRelevancy,
    )

    metrics = [
        LLMContextPrecisionWithReference(llm=llm),
        LLMContextRecall(llm=llm),
        Faithfulness(llm=llm),
        AnswerRelevancy(llm=llm, embeddings=embeddings),
    ]

    # RunConfig: throttle concurrency to stay comfortably within Gemini's 15 RPM free limit
    run_config = RunConfig(
        timeout=300,
        max_retries=10,
        max_wait=120,
        max_workers=max_workers,
    )

    print("\n" + "=" * 64)
    print("  Running Ragas Evaluation")
    print("=" * 64)
    start = time.time()

    result = evaluate(
        dataset=dataset,
        metrics=metrics,
        llm=llm,
        embeddings=embeddings,
        run_config=run_config,
        raise_exceptions=False,
        show_progress=True,
    )

    elapsed = time.time() - start
    print(f"\nEvaluation completed in {elapsed:.1f}s")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 4. Save results to CSV
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(value: Any) -> str:
    """Format a metric value for CSV output."""
    if value is None:
        return "NaN"
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    if isinstance(value, (float, int)):
        return f"{float(value):.4f}"
    return str(value)


def _extract_metric(data: Any, *keys: str) -> float:
    """Extract a metric score from a dict, DataFrame row/Series, or EvaluationResult."""
    if hasattr(data, "_repr_dict") and isinstance(data._repr_dict, dict):
        for k in keys:
            if k in data._repr_dict:
                return data._repr_dict[k]
    if hasattr(data, "get"):
        for k in keys:
            val = data.get(k)
            if val is not None and not (isinstance(val, float) and math.isnan(val)):
                return val
    try:
        for k in keys:
            if k in data:
                val = data[k]
                if isinstance(val, list) and val:
                    valid = [v for v in val if v is not None and not (isinstance(v, float) and math.isnan(v))]
                    return sum(valid) / len(valid) if valid else float("nan")
                if val is not None and not (isinstance(val, float) and math.isnan(val)):
                    return val
    except Exception:
        pass
    return float("nan")


def save_results_csv(
    ragas_result,
    test_cases: list[dict[str, str]],
    pipeline_outputs: list[dict[str, Any]],
    output_path: str,
) -> None:
    """Save per-question scores and aggregate metrics to a CSV file."""

    # Convert Ragas result to a pandas DataFrame for per-row scores if possible
    try:
        df = ragas_result.to_pandas()
    except Exception as e:
        print(f"Warning: could not convert to pandas: {e}")
        df = None

    scores_list = getattr(ragas_result, "scores", [])

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        # ── Header ───────────────────────────────────────────────────────
        writer.writerow([
            "question",
            "answer",
            "ground_truth",
            "num_contexts",
            "context_precision",
            "context_recall",
            "faithfulness",
            "answer_relevancy",
        ])

        # ── Per-question rows ────────────────────────────────────────────
        total_rows = len(test_cases)
        for i in range(total_rows):
            answer = pipeline_outputs[i]["answer"] if i < len(pipeline_outputs) else ""
            # Truncate long answers for readability
            answer_short = answer[:200] + ("..." if len(answer) > 200 else "")
            num_ctx = len(pipeline_outputs[i].get("contexts", [])) if i < len(pipeline_outputs) else 0

            row_data: dict[str, Any] = {}
            if df is not None and i < len(df):
                row_data = df.iloc[i].to_dict()
            elif i < len(scores_list) and isinstance(scores_list[i], dict):
                row_data = scores_list[i]

            cp = _extract_metric(row_data, "context_precision", "llm_context_precision_with_reference")
            cr = _extract_metric(row_data, "context_recall", "llm_context_recall")
            faith = _extract_metric(row_data, "faithfulness")
            ar = _extract_metric(row_data, "answer_relevancy", "response_relevancy")

            question_text = row_data.get("user_input", test_cases[i]["question"])

            writer.writerow([
                question_text,
                answer_short,
                test_cases[i]["ground_truth"],
                num_ctx,
                _fmt(cp),
                _fmt(cr),
                _fmt(faith),
                _fmt(ar),
            ])

        # ── Aggregate row ────────────────────────────────────────────────
        writer.writerow([])  # blank separator
        agg_cp = _extract_metric(ragas_result, "context_precision", "llm_context_precision_with_reference")
        agg_cr = _extract_metric(ragas_result, "context_recall", "llm_context_recall")
        agg_faith = _extract_metric(ragas_result, "faithfulness")
        agg_ar = _extract_metric(ragas_result, "answer_relevancy", "response_relevancy")

        writer.writerow(["AGGREGATE", "", "", ""] + [
            _fmt(agg_cp), _fmt(agg_cr), _fmt(agg_faith), _fmt(agg_ar)
        ])

    print(f"\nResults saved to {output_path}")


def print_aggregate_scores(ragas_result) -> None:
    """Print a summary of aggregate metric scores to stdout."""
    metric_keys = [
        ("Context Precision", ["context_precision", "llm_context_precision_with_reference"]),
        ("Context Recall", ["context_recall", "llm_context_recall"]),
        ("Faithfulness", ["faithfulness"]),
        ("Answer Relevancy", ["answer_relevancy", "response_relevancy"]),
    ]
    print("\n" + "=" * 64)
    print("  AGGREGATE EVALUATION SCORES")
    print("=" * 64)
    for label, keys in metric_keys:
        val = _extract_metric(ragas_result, *keys)
        print(f"  {label:<22}: {_fmt(val)}")
    print("=" * 64)


# ─────────────────────────────────────────────────────────────────────────────
# 5. CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Evaluate the RAG pipeline using Ragas metrics.",
        epilog="Example: python evaluate_rag.py abc-123-def",
    )
    parser.add_argument(
        "job_id",
        help="The job/query ID to scope retrieval (UUID from the ingestion run).",
    )
    parser.add_argument(
        "--test-file",
        default="test_cases.json",
        help="Path to the JSON test cases file (default: test_cases.json).",
    )
    parser.add_argument(
        "--output",
        default="ragas_report.csv",
        help="Output CSV path (default: ragas_report.csv).",
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:8000/api/chat/",
        help="URL of the /api/chat/ endpoint (default: http://localhost:8000/api/chat/).",
    )
    parser.add_argument(
        "--model",
        default="gemini-3.1-flash-lite",
        help="Evaluator LLM model (default: gemini-3.1-flash-lite, with high 1,500 req/day free limit).",
    )
    parser.add_argument(
        "--max-cases",
        type=int,
        default=None,
        help="Limit number of test cases to evaluate (e.g. --max-cases 5).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Max concurrent evaluation workers (default: 3, to respect Gemini free-tier 15 RPM).",
    )
    args = parser.parse_args()

    # ── Step 1: Load test cases ──────────────────────────────────────────
    test_cases = load_test_cases(args.test_file)
    if args.max_cases and args.max_cases > 0:
        test_cases = test_cases[:args.max_cases]
        print(f"Subsetting to first {len(test_cases)} test cases (--max-cases={args.max_cases})")

    # ── Step 2: Query the pipeline ───────────────────────────────────────
    pipeline_outputs = gather_pipeline_outputs(test_cases, args.job_id, args.api_url)

    # ── Step 3: Build Ragas dataset ──────────────────────────────────────
    dataset = build_ragas_dataset(pipeline_outputs)

    # ── Step 4: Configure evaluator LLM and Embeddings ──────────────
    llm = get_evaluator_llm(args.model)
    embeddings = get_evaluator_embeddings()

    # ── Step 5: Run Ragas evaluation ─────────────────────────────────────
    ragas_result = run_ragas_evaluation(dataset, llm, embeddings, max_workers=args.workers)

    # ── Step 6: Print and save results ───────────────────────────────────
    try:
        print_aggregate_scores(ragas_result)
    except Exception as exc:
        print(f"Warning: could not display aggregate summary: {exc}")

    try:
        save_results_csv(ragas_result, test_cases, pipeline_outputs, args.output)
    except Exception as exc:
        print(f"Error saving CSV: {exc}")


if __name__ == "__main__":
    main()
