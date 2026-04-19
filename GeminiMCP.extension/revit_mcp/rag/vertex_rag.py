# google-cloud-discoveryengine and its dependencies are bundled in lib/

from revit_mcp.config import VERTEX_SERVING_CONFIG

# Pre-import grpc once at module load time in a background thread with a timeout
# so the first query_vertex_rag() call is not blocked by grpc C-extension init.
_discoveryengine = None
_MessageToDict = None
_import_error = None


def _do_import():
    global _discoveryengine, _MessageToDict, _import_error
    try:
        # Import only the REST transport submodule — avoids triggering grpc C-extension init
        # which hangs indefinitely inside Revit's embedded Python / COM threading environment.
        import os as _os
        _os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "false")
        # Stub out grpc before discoveryengine loads it so the C extension never initialises
        import sys as _sys
        if "grpc" not in _sys.modules:
            import types as _types
            _grpc_stub = _types.ModuleType("grpc")
            _grpc_stub.insecure_channel = lambda *a, **k: None
            _grpc_stub.secure_channel = lambda *a, **k: None
            _grpc_stub.ssl_channel_credentials = lambda *a, **k: None
            _sys.modules["grpc"] = _grpc_stub
            _sys.modules["grpc._channel"] = _types.ModuleType("grpc._channel")
            _sys.modules["grpc.experimental"] = _types.ModuleType("grpc.experimental")
        from google.cloud import discoveryengine_v1beta as de
        from google.protobuf.json_format import MessageToDict as mtd
        _discoveryengine = de
        _MessageToDict = mtd
    except Exception as e:
        _import_error = e


def _ensure_imported(timeout=8.0):
    """Import discoveryengine in a thread so grpc init can't block the caller."""
    import threading
    import time
    global _discoveryengine, _import_error
    if _discoveryengine is not None:
        return True
    if _import_error is not None:
        return False
    t = threading.Thread(target=_do_import, daemon=True)
    t0 = time.time()
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        _log(f"IMPORT TIMEOUT after {timeout}s — grpc init blocked, skipping RAG")
        return False
    if _import_error:
        _log(f"IMPORT ERROR: {_import_error}")
        return False
    return _discoveryengine is not None


def _log(msg):
    try:
        from revit_mcp.gemini_client import client
        client.log(f"[RAG:vertex] {msg}")
    except Exception:
        pass


def query_vertex_rag(query: str, topic_filter: str = None, top_k: int = 5) -> list:
    import time
    _log(f"query_vertex_rag START — query='{query[:80]}' filter={topic_filter}")
    t0 = time.time()

    _log("ensuring discoveryengine is imported (thread, timeout=8s)...")
    if not _ensure_imported(timeout=8.0):
        _log("discoveryengine not available — returning []")
        return []
    _log(f"import ready in {time.time()-t0:.2f}s")

    discoveryengine = _discoveryengine
    MessageToDict = _MessageToDict

    try:
        _log("creating SearchServiceClient (REST transport)...")
        # transport='rest' uses plain HTTP instead of grpc — avoids grpc C-extension
        # init hang inside Revit's embedded Python environment
        search_client = discoveryengine.SearchServiceClient(transport="rest")
        _log(f"SearchServiceClient created in {time.time()-t0:.2f}s")
    except Exception as e:
        _log(f"SearchServiceClient creation FAILED: {e}")
        return []

    filter_expr = f'metadata.topic: "{topic_filter}"' if topic_filter else None
    request = discoveryengine.SearchRequest(
        serving_config=VERTEX_SERVING_CONFIG,
        query=query,
        page_size=top_k,
        filter=filter_expr,
        query_expansion_spec=discoveryengine.SearchRequest.QueryExpansionSpec(
            condition=discoveryengine.SearchRequest.QueryExpansionSpec.Condition.AUTO
        ),
        content_search_spec=discoveryengine.SearchRequest.ContentSearchSpec(
            extractive_content_spec=discoveryengine.SearchRequest.ContentSearchSpec.ExtractiveContentSpec(
                max_extractive_answer_count=1,
            )
        ),
    )

    try:
        _log(f"calling search() timeout=6s")
        t_search = time.time()
        response = search_client.search(request, timeout=6.0)
        _log(f"search() returned in {time.time()-t_search:.2f}s")
    except Exception as e:
        _log(f"search() FAILED after {time.time()-t0:.2f}s: {e}")
        return []

    results = []
    for result in response.results:
        doc = result.document
        derived = doc.derived_struct_data
        title = derived.get("title", "")
        content = ""
        page = ""

        answers_field = derived.get("extractive_answers") or derived.get("extractiveAnswers")
        if answers_field:
            try:
                first = list(answers_field)[0]
                content = first.get("content", "")
                page = first.get("pageNumber", "") or first.get("page_number", "")
            except Exception:
                pass

        if not content:
            struct = MessageToDict(doc.struct_data) if doc.struct_data else {}
            content = struct.get("content", "")

        results.append({
            "content": content,
            "metadata": {
                "title": title,
                "page": page,
                "clause": f"{title} p.{page}" if page else title,
            },
        })

    _log(f"query_vertex_rag DONE — {len(results)} results in {time.time()-t0:.2f}s total")
    return results
