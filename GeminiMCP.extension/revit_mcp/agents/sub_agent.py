import asyncio
import json
import time
import concurrent.futures
from revit_mcp.gemini_client import client
from revit_mcp.rag.vertex_rag import query_vertex_rag
from revit_mcp.rag.query_builder import build_query

SUB_AGENT_PROMPT = """
You are a Singapore SCDF fire code rules specialist.
Extract spatial dimensions and requirements relevant to the building intent below.
If there are conflicting values, use the STRICTER requirement. Normalise units to mm/m2.
Include the clause reference as the source. Return ONLY raw JSON. No markdown blocks.

Building Intent:
{intent_json}

Retrieved Excerpts:
{chunks_text}

Expected JSON format:
{{
  "authority": "SCDF",
  "rules": {{
    "staircase": {{ "min_width_mm": <int>, "min_count": <int>, "pressurisation_required": <bool>, "source": "<clause>" }},
    "fire_lift": {{ "min_car_width_mm": <int>, "source": "<clause>" }},
    "fire_lift_lobby": {{ "min_area_m2": <float>, "source": "<clause>" }}
  }}
}}
"""


def _log(msg):
    client.log(f"[RAG] {msg}")


async def _fetch_topic(topic: str, intent: dict) -> dict:
    t0 = time.time()
    _log(f"_fetch_topic START: {topic}")
    query = build_query(topic, intent)
    _log(f"_fetch_topic query built for {topic}: {query}")
    loop = asyncio.get_running_loop()
    try:
        results = await loop.run_in_executor(
            None, lambda: query_vertex_rag(query=query, topic_filter=topic, top_k=5)
        )
        _log(f"_fetch_topic DONE: {topic} — {len(results)} results in {time.time()-t0:.2f}s")
    except Exception as e:
        _log(f"_fetch_topic ERROR: {topic} — {e} ({time.time()-t0:.2f}s)")
        results = []
    return {"topic": topic, "results": results}


async def retrieve_rules(intent: dict, report=None) -> dict:
    t0 = time.time()
    topics = intent.get("topics", [])
    _log(f"retrieve_rules START — intent={intent}")

    if not topics:
        _log("retrieve_rules: no topics in intent, skipping")
        return {}

    if report:
        report(f"🔍 **Authority Code Sub-Agent** — querying Vertex AI RAG for: {', '.join(topics)}")

    _log(f"retrieve_rules: firing {len(topics)} parallel Vertex queries...")
    t_vertex = time.time()
    raw_results = await asyncio.gather(*[_fetch_topic(t, intent) for t in topics])
    _log(f"retrieve_rules: all Vertex queries done in {time.time()-t_vertex:.2f}s")

    chunks_text = ""
    retrieved_summary = []
    for item in raw_results:
        topic_label = item["topic"].replace("_", " ").title()
        chunks_text += f"\n\n--- {item['topic'].upper()} ---\n"
        top_chunks = []
        for chunk in item["results"]:
            clause = chunk["metadata"].get("clause", "unknown clause")
            content = chunk["content"]
            chunks_text += f"[{clause}] {content}\n"
            if content:
                top_chunks.append(f"_{clause}_: {content[:120]}...")
        if top_chunks:
            retrieved_summary.append(f"**{topic_label}**\n" + "\n".join(f"  • {c}" for c in top_chunks[:2]))

    if report and retrieved_summary:
        report("📋 **Retrieved SCDF excerpts:**\n" + "\n\n".join(retrieved_summary), is_narrative=True)

    _log(f"retrieve_rules: chunks assembled ({len(chunks_text)} chars), calling Gemini for synthesis...")
    if report:
        report("🤖 **Authority Code Sub-Agent** — synthesising rules into structured JSON...")

    prompt = SUB_AGENT_PROMPT.format(
        intent_json=json.dumps(intent, indent=2),
        chunks_text=chunks_text,
    )

    t_gemini = time.time()
    try:
        raw_text = client.generate_content(prompt, thinking_budget=0)
        _log(f"retrieve_rules: Gemini synthesis done in {time.time()-t_gemini:.2f}s, response len={len(raw_text)}")
    except Exception as e:
        _log(f"retrieve_rules: Gemini synthesis FAILED in {time.time()-t_gemini:.2f}s — {e}")
        if report:
            report("⚠️ Authority Code Sub-Agent: Gemini synthesis failed, using static fallback.")
        return {}

    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]

    try:
        rules = json.loads(raw_text.strip())
        _log(f"retrieve_rules: JSON parsed OK — topics={list(rules.get('rules', {}).keys())} total={time.time()-t0:.2f}s")
        if report and rules.get("rules"):
            lines = []
            for topic, vals in rules["rules"].items():
                label = topic.replace("_", " ").title()
                source = vals.get("source", "")
                nums = {k: v for k, v in vals.items() if k != "source"}
                lines.append(f"**{label}**: {json.dumps(nums)} _{source}_")
            report("✅ **Authority codes resolved:**\n" + "\n".join(f"  • {l}" for l in lines), is_narrative=True)
        return rules
    except Exception as e:
        _log(f"retrieve_rules: JSON parse FAILED — {e} | raw={raw_text[:200]}")
        if report:
            report("⚠️ Authority Code Sub-Agent: failed to parse rules, using static fallback.")
        return {}


def _run_in_new_loop(coro):
    _log("_run_in_new_loop: creating fresh event loop")
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(coro)
        _log("_run_in_new_loop: event loop completed OK")
        return result
    except Exception as e:
        _log(f"_run_in_new_loop: event loop raised {e}")
        raise
    finally:
        loop.close()


def run_retrieve_rules(intent: dict, report=None) -> dict:
    """Synchronous entry point for dispatcher.py."""
    t0 = time.time()
    _log(f"run_retrieve_rules START — intent={intent}")
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            _log("run_retrieve_rules: submitting to ThreadPoolExecutor...")
            future = executor.submit(_run_in_new_loop, retrieve_rules(intent, report=report))
            _log("run_retrieve_rules: waiting for result (timeout=15s)...")
            result = future.result(timeout=15)
            _log(f"run_retrieve_rules DONE in {time.time()-t0:.2f}s")
            return result
    except concurrent.futures.TimeoutError:
        _log(f"run_retrieve_rules TIMEOUT after {time.time()-t0:.2f}s — falling back to static rules")
        if report:
            report("⚠️ Authority code retrieval timed out (>15s), using static rules.")
        return {}
    except Exception as e:
        _log(f"run_retrieve_rules ERROR after {time.time()-t0:.2f}s — {e}")
        if report:
            report(f"⚠️ Authority code retrieval error: {e}")
        return {}
