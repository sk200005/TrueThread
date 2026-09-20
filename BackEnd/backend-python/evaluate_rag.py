import json
import requests
import os
import sys
import asyncio
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()


# ── Groq Judge (OpenAI-compatible API) ────────────────────────────────────────

async def judge(client, prompt: str) -> str:
    """Send a judging prompt to Groq and return the raw text."""
    response = await client.chat.completions.create(
        model="qwen/qwen3.8-27b",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=1024,
    )
    return response.choices[0].message.content.strip()


async def score_faithfulness(client, answer: str, contexts: list[str]) -> float:
    """
    Faithfulness: Break the answer into claims, check each against the context.
    Score = supported_claims / total_claims
    """
    context_str = "\n---\n".join(contexts)
    prompt = f"""You are an evaluation judge. Given an ANSWER and a CONTEXT, do the following:
1. Break the ANSWER into individual factual claims.
2. For each claim, determine if it is supported by the CONTEXT (YES or NO).
3. Return ONLY a JSON object like: {{"supported": 3, "total": 4}}

CONTEXT:
{context_str}

ANSWER:
{answer}

Return ONLY the JSON object, nothing else."""

    try:
        result = await judge(client, prompt)
        # Parse JSON from result
        result = result.replace("```json", "").replace("```", "").strip()
        data = json.loads(result)
        if data["total"] == 0:
            return 1.0
        return round(data["supported"] / data["total"], 2)
    except Exception as e:
        print(f"    [Faithfulness Error] {e}")
        return float("nan")


async def score_answer_relevancy(client, question: str, answer: str) -> float:
    """
    Answer Relevancy: Does the answer address the question?
    Score from 0.0 to 1.0
    """
    prompt = f"""You are an evaluation judge. Rate how relevant the ANSWER is to the QUESTION.
A score of 1.0 means the answer directly and completely addresses the question.
A score of 0.0 means the answer is completely irrelevant or off-topic.

QUESTION: {question}
ANSWER: {answer}

Return ONLY a single decimal number between 0.0 and 1.0. Nothing else."""

    try:
        result = await judge(client, prompt)
        return round(float(result), 2)
    except Exception as e:
        print(f"    [Faithfulness Error] {e}")
        return float("nan")


async def score_context_precision(client, question: str, contexts: list[str], ground_truth: str) -> float:
    """
    Context Precision: Are the relevant chunks ranked at the top?
    Uses Mean Average Precision logic.
    """
    if not contexts:
        return 0.0

    prompt = f"""You are an evaluation judge. Given a QUESTION and GROUND TRUTH answer, evaluate each CONTEXT chunk.
For each chunk, respond YES if it is useful for answering the question, NO if it is not.

QUESTION: {question}
GROUND TRUTH: {ground_truth}

CHUNKS:
"""
    for i, ctx in enumerate(contexts):
        prompt += f"\nChunk {i+1}: {ctx[:300]}...\n"

    prompt += f"\nReturn ONLY a JSON array of {len(contexts)} values, e.g. [\"YES\", \"NO\", \"YES\", \"NO\", \"YES\"]. Nothing else."

    try:
        result = await judge(client, prompt)
        result = result.replace("```json", "").replace("```", "").strip()
        verdicts = json.loads(result)

        # Calculate Mean Average Precision
        relevant_count = 0
        precision_sum = 0.0
        for i, v in enumerate(verdicts):
            if v.upper() == "YES":
                relevant_count += 1
                precision_sum += relevant_count / (i + 1)
        if relevant_count == 0:
            return 0.0
        return round(precision_sum / relevant_count, 2)
    except Exception as e:
        print(f"    [Faithfulness Error] {e}")
        return float("nan")


async def score_context_recall(client, contexts: list[str], ground_truth: str) -> float:
    """
    Context Recall: Does the context cover all the information in the ground truth?
    Score = statements_found_in_context / total_ground_truth_statements
    """
    context_str = "\n---\n".join(contexts)
    prompt = f"""You are an evaluation judge. Given a GROUND TRUTH and a CONTEXT, do the following:
1. Break the GROUND TRUTH into individual factual statements.
2. For each statement, check if it can be found or inferred from the CONTEXT (YES or NO).
3. Return ONLY a JSON object like: {{"found": 3, "total": 4}}

GROUND TRUTH:
{ground_truth}

CONTEXT:
{context_str}

Return ONLY the JSON object, nothing else."""

    try:
        result = await judge(client, prompt)
        result = result.replace("```json", "").replace("```", "").strip()
        data = json.loads(result)
        if data["total"] == 0:
            return 1.0
        return round(data["found"] / data["total"], 2)
    except Exception as e:
        print(f"    [Faithfulness Error] {e}")
        return float("nan")


# ── Main Pipeline ─────────────────────────────────────────────────────────────

async def run_evaluation(job_id):
    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        print("Error: GROQ_API_KEY not found in your .env file.")
        sys.exit(1)

    client = AsyncOpenAI(
        api_key=groq_key,
        base_url="https://api.groq.com/openai/v1"
    )

    # 1. Load test cases
    print("Loading test cases from test_cases.json...")
    try:
        with open("test_cases.json", "r") as f:
            test_cases = json.load(f)
    except FileNotFoundError:
        print("Error: test_cases.json not found.")
        sys.exit(1)

    # 2. Gather data from the Python API
    results = []
    print(f"Gathering data from Python API for Job ID: {job_id}...\n")

    for idx, case in enumerate(test_cases, 1):
        question = case["question"]
        ground_truth = case["ground_truth"]
        print(f"  [{idx}/{len(test_cases)}] Asking: {question[:60]}...")

        url = "http://localhost:8000/api/chat/"
        try:
            response = requests.post(url, json={"query_id": job_id, "message": question})
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            print(f"  Error communicating with API: {e}")
            sys.exit(1)

        answer = data.get("response", "")
        contexts = data.get("contexts", [])

        results.append({
            "question": question,
            "answer": answer,
            "contexts": contexts,
            "ground_truth": ground_truth,
        })

    # 3. Run evaluation using Gemini as the judge
    print("\n" + "=" * 60)
    print("Running LLM-as-a-Judge Evaluation using Gemini...")
    print("=" * 60)

    all_faithfulness = []
    all_relevancy = []
    all_precision = []
    all_recall = []

    for idx, r in enumerate(results, 1):
        print(f"\n  Evaluating Q{idx}: {r['question'][:50]}...")

        f_score = await score_faithfulness(client, r["answer"], r["contexts"])
        ar_score = await score_answer_relevancy(client, r["question"], r["answer"])
        cp_score = await score_context_precision(client, r["question"], r["contexts"], r["ground_truth"])
        cr_score = await score_context_recall(client, r["contexts"], r["ground_truth"])

        all_faithfulness.append(f_score)
        all_relevancy.append(ar_score)
        all_precision.append(cp_score)
        all_recall.append(cr_score)

        print(f"    Faithfulness: {f_score}  |  Answer Relevancy: {ar_score}  |  Context Precision: {cp_score}  |  Context Recall: {cr_score}")

    # 4. Calculate averages (ignoring NaN)
    def safe_avg(lst):
        valid = [x for x in lst if x == x]  # NaN != NaN
        return round(sum(valid) / len(valid), 2) if valid else float("nan")

    print("\n" + "=" * 60)
    print("FINAL EVALUATION SCORES")
    print("=" * 60)
    print(f"  Faithfulness      : {safe_avg(all_faithfulness)}")
    print(f"  Answer Relevancy  : {safe_avg(all_relevancy)}")
    print(f"  Context Precision : {safe_avg(all_precision)}")
    print(f"  Context Recall    : {safe_avg(all_recall)}")
    print("=" * 60)

    # 5. Save detailed CSV report
    import csv
    with open("ragas_report.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["question", "answer", "ground_truth", "faithfulness", "answer_relevancy", "context_precision", "context_recall"])
        for i, r in enumerate(results):
            writer.writerow([
                r["question"], r["answer"][:200], r["ground_truth"],
                all_faithfulness[i], all_relevancy[i], all_precision[i], all_recall[i]
            ])
    print("\nDetailed report saved to ragas_report.csv!")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python evaluate_rag.py <job_id>")
        sys.exit(1)

    asyncio.run(run_evaluation(sys.argv[1]))
