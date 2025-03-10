import asyncio
import json
import logging
from copy import deepcopy
from datetime import datetime
from typing import Any, AsyncGenerator, Literal, Optional
from uuid import UUID

from fastapi import HTTPException

from core import (
    R2RRAGAgent,
    R2RStreamingRAGAgent,
    R2RXMLToolsRAGAgent,
    R2RXMLToolsStreamingRAGAgent,
)
from core.agent.research import R2RResearchAgent
from core.base import (
    AggregateSearchResult,
    ChunkSearchResult,
    DocumentResponse,
    GenerationConfig,
    GraphCommunityResult,
    GraphEntityResult,
    GraphRelationshipResult,
    GraphSearchResult,
    GraphSearchResultType,
    IngestionStatus,
    Message,
    R2RException,
    SearchSettings,
    format_search_results_for_llm,
)
from core.base.api.models import RAGResponse, User
from core.telemetry.telemetry_decorator import telemetry_event
from core.utils import (
    SearchResultsCollector,
    SSEFormatter,
    dump_collector,
    extract_citations,
    num_tokens_from_messages,
)
from shared.api.models.management.responses import MessageResponse

from ..abstractions import R2RProviders
from ..config import R2RConfig
from .base import Service

logger = logging.getLogger()


class RetrievalService(Service):
    def __init__(
        self,
        config: R2RConfig,
        providers: R2RProviders,
    ):
        super().__init__(
            config,
            providers,
        )

    @telemetry_event("Search")
    async def search(
        self,
        query: str,
        search_settings: SearchSettings = SearchSettings(),
        *args,
        **kwargs,
    ) -> AggregateSearchResult:
        """
        Depending on search_settings.search_strategy, fan out
        to basic, hyde, or rag_fusion method. Each returns
        an AggregateSearchResult that includes chunk + graph results.
        """
        strategy = search_settings.search_strategy.lower()

        if strategy == "hyde":
            return await self._hyde_search(query, search_settings)
        elif strategy == "rag_fusion":
            return await self._rag_fusion_search(query, search_settings)
        else:
            # 'vanilla', 'basic', or anything else...
            return await self._basic_search(query, search_settings)

    async def _basic_search(
        self, query: str, search_settings: SearchSettings
    ) -> AggregateSearchResult:
        """
        1) Possibly embed the query (if semantic or hybrid).
        2) Chunk search.
        3) Graph search.
        4) Combine into an AggregateSearchResult.
        """
        # -- 1) Possibly embed the query
        query_vector = None
        if (
            search_settings.use_semantic_search
            or search_settings.use_hybrid_search
        ):
            query_vector = (
                await self.providers.completion_embedding.async_get_embedding(
                    query  # , EmbeddingPurpose.QUERY
                )
            )

        # -- 2) Chunk search
        chunk_results = []
        if search_settings.chunk_settings.enabled:
            chunk_results = await self._vector_search_logic(
                query_text=query,
                search_settings=search_settings,
                precomputed_vector=query_vector,  # Pass in the vector we just computed (if any)
            )

        # -- 3) Graph search
        graph_results = []
        if search_settings.graph_settings.enabled:
            graph_results = await self._graph_search_logic(
                query_text=query,
                search_settings=search_settings,
                precomputed_vector=query_vector,  # same idea
            )

        # -- 4) Combine
        return AggregateSearchResult(
            chunk_search_results=chunk_results,
            graph_search_results=graph_results,
        )

    async def _rag_fusion_search(
        self, query: str, search_settings: SearchSettings
    ) -> AggregateSearchResult:
        """
        Implements 'RAG Fusion':
        1) Generate N sub-queries from the user query
        2) For each sub-query => do chunk & graph search
        3) Combine / fuse all retrieved results using Reciprocal Rank Fusion
        4) Return an AggregateSearchResult
        """

        # 1) Generate sub-queries from the user’s original query
        #    Typically you want the original query to remain in the set as well,
        #    so that we do not lose the exact user intent.
        sub_queries = [query]
        if search_settings.num_sub_queries > 1:
            # Generate (num_sub_queries - 1) rephrasings
            # (Or just generate exactly search_settings.num_sub_queries,
            #  and remove the first if you prefer.)
            extra = await self._generate_similar_queries(
                query=query,
                num_sub_queries=search_settings.num_sub_queries - 1,
            )
            sub_queries.extend(extra)

        # 2) For each sub-query => do chunk + graph search
        #    We’ll store them in a structure so we can fuse them.
        #    chunk_results_list is a list of lists of ChunkSearchResult
        #    graph_results_list is a list of lists of GraphSearchResult
        chunk_results_list = []
        graph_results_list = []

        for sq in sub_queries:
            # Recompute or reuse the embedding if desired
            # (You could do so, but not mandatory if you have a local approach)
            # chunk + graph search
            aggr = await self._basic_search(sq, search_settings)
            chunk_results_list.append(aggr.chunk_search_results)
            graph_results_list.append(aggr.graph_search_results)

        # 3) Fuse the chunk results and fuse the graph results.
        #    We'll use a simple RRF approach: each sub-query's result list
        #    is a ranking from best to worst.
        fused_chunk_results = self._reciprocal_rank_fusion_chunks(
            chunk_results_list
        )
        fused_graph_results = self._reciprocal_rank_fusion_graphs(
            graph_results_list
        )

        # Optionally, after the RRF, you may want to do a final semantic re-rank
        # of the fused results by the user’s original query.
        # E.g.:
        if fused_chunk_results:
            fused_chunk_results = (
                await self.providers.completion_embedding.arerank(
                    query=query,
                    results=fused_chunk_results,
                    limit=search_settings.limit,
                )
            )

        # Sort or slice the graph results if needed:
        if fused_graph_results and search_settings.include_scores:
            fused_graph_results.sort(
                key=lambda g: g.score if g.score is not None else 0.0,
                reverse=True,
            )
            fused_graph_results = fused_graph_results[: search_settings.limit]

        # 4) Return final AggregateSearchResult
        return AggregateSearchResult(
            chunk_search_results=fused_chunk_results,
            graph_search_results=fused_graph_results,
        )

    async def _generate_similar_queries(
        self, query: str, num_sub_queries: int = 2
    ) -> list[str]:
        """
        Use your LLM to produce 'similar' queries or rephrasings
        that might retrieve different but relevant documents.

        You can prompt your model with something like:
        "Given the user query, produce N alternative short queries that
        capture possible interpretations or expansions.
        Keep them relevant to the user's intent."
        """
        if num_sub_queries < 1:
            return []

        # In production, you'd fetch a prompt from your prompts DB:
        # Something like:
        prompt = f"""
    You are a helpful assistant. The user query is: "{query}"
    Generate {num_sub_queries} alternative search queries that capture
    slightly different phrasings or expansions while preserving the core meaning.
    Return each alternative on its own line.
        """

        # For a short generation, we can set minimal tokens
        gen_config = GenerationConfig(
            model=self.config.app.fast_llm,
            max_tokens=128,
            temperature=0.8,
            stream=False,
        )
        response = await self.providers.llm.aget_completion(
            messages=[{"role": "system", "content": prompt}],
            generation_config=gen_config,
        )
        raw_text = response.choices[0].message.content.strip()

        # Suppose each line is a sub-query
        lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
        return lines[:num_sub_queries]

    def _reciprocal_rank_fusion_chunks(
        self, list_of_rankings: list[list[ChunkSearchResult]], k: float = 60.0
    ) -> list[ChunkSearchResult]:
        """
        Simple RRF for chunk results.
        list_of_rankings is something like:
        [
            [chunkA, chunkB, chunkC],  # sub-query #1, in order
            [chunkC, chunkD],         # sub-query #2, in order
            ...
        ]

        We'll produce a dictionary mapping chunk.id -> aggregated_score,
        then sort descending.
        """
        if not list_of_rankings:
            return []

        # Build a map of chunk_id => final_rff_score
        score_map = {}

        # We also need to store a reference to the chunk object
        # (the "first" or "best" instance), so we can reconstruct them later
        chunk_map = {}

        for ranking_list in list_of_rankings:
            for rank, chunk_result in enumerate(ranking_list, start=1):
                if not chunk_result.id:
                    # fallback if no chunk_id is present
                    continue

                c_id = chunk_result.id
                # RRF scoring
                # score = sum(1 / (k + rank)) for each sub-query ranking
                # We'll accumulate it.
                existing_score = score_map.get(c_id, 0.0)
                new_score = existing_score + 1.0 / (k + rank)
                score_map[c_id] = new_score

                # Keep a reference to chunk
                if c_id not in chunk_map:
                    chunk_map[c_id] = chunk_result

        # Now sort by final score
        fused_items = sorted(
            score_map.items(), key=lambda x: x[1], reverse=True
        )

        # Rebuild the final list of chunk results with new 'score'
        fused_chunks = []
        for c_id, agg_score in fused_items:
            # copy the chunk
            c = chunk_map[c_id]
            # Optionally store the RRF score if you want
            c.score = agg_score
            fused_chunks.append(c)

        return fused_chunks

    def _reciprocal_rank_fusion_graphs(
        self, list_of_rankings: list[list[GraphSearchResult]], k: float = 60.0
    ) -> list[GraphSearchResult]:
        """
        Similar RRF logic but for graph results.
        """
        if not list_of_rankings:
            return []

        score_map = {}
        graph_map = {}

        for ranking_list in list_of_rankings:
            for rank, g_result in enumerate(ranking_list, start=1):
                # We'll do a naive ID approach:
                # If your GraphSearchResult has a unique ID in g_result.content.id or so
                # we can use that as a key.
                # If not, you might have to build a key from the content.
                g_id = None
                if hasattr(g_result.content, "id"):
                    g_id = str(g_result.content.id)
                else:
                    # fallback
                    g_id = f"graph_{hash(g_result.content.json())}"

                existing_score = score_map.get(g_id, 0.0)
                new_score = existing_score + 1.0 / (k + rank)
                score_map[g_id] = new_score

                if g_id not in graph_map:
                    graph_map[g_id] = g_result

        # Sort descending by aggregated RRF score
        fused_items = sorted(
            score_map.items(), key=lambda x: x[1], reverse=True
        )

        fused_graphs = []
        for g_id, agg_score in fused_items:
            g = graph_map[g_id]
            g.score = agg_score
            fused_graphs.append(g)

        return fused_graphs

    async def _hyde_search(
        self, query: str, search_settings: SearchSettings
    ) -> AggregateSearchResult:
        """
        1) Generate N hypothetical docs via LLM
        2) For each doc => embed => parallel chunk search & graph search
        3) Merge chunk results => optional re-rank => top K
        4) Merge graph results => (optionally re-rank or keep them distinct)
        """
        # 1) Generate hypothetical docs
        hyde_docs = await self._run_hyde_generation(
            query=query, num_sub_queries=search_settings.num_sub_queries
        )

        chunk_all = []
        graph_all = []

        # We'll gather the per-doc searches in parallel
        tasks = []
        for hypothetical_text in hyde_docs:
            tasks.append(
                asyncio.create_task(
                    self._fanout_chunk_and_graph_search(
                        user_text=query,  # The user’s original query
                        alt_text=hypothetical_text,  # The hypothetical doc
                        search_settings=search_settings,
                    )
                )
            )

        # 2) Wait for them all
        results_list = await asyncio.gather(*tasks)
        # each item in results_list is a tuple: (chunks, graphs)

        # Flatten chunk+graph results
        for c_results, g_results in results_list:
            chunk_all.extend(c_results)
            graph_all.extend(g_results)

        # 3) Re-rank chunk results with the original query
        if chunk_all:
            chunk_all = await self.providers.completion_embedding.arerank(
                query=query,  # final user query
                results=chunk_all,
                limit=int(
                    search_settings.limit * search_settings.num_sub_queries
                ),
                # no limit on results - limit=search_settings.limit,
            )

        # 4) If needed, re-rank graph results or just slice top-K by score
        if search_settings.include_scores and graph_all:
            graph_all.sort(key=lambda g: g.score or 0.0, reverse=True)
            graph_all = (
                graph_all  # no limit on results - [: search_settings.limit]
            )

        return AggregateSearchResult(
            chunk_search_results=chunk_all,
            graph_search_results=graph_all,
        )

    async def _fanout_chunk_and_graph_search(
        self,
        user_text: str,
        alt_text: str,
        search_settings: SearchSettings,
    ) -> tuple[list[ChunkSearchResult], list[GraphSearchResult]]:
        """
        1) embed alt_text (HyDE doc or sub-query, etc.)
        2) chunk search + graph search with that embedding
        """
        # Precompute the embedding of alt_text
        vec = await self.providers.completion_embedding.async_get_embedding(
            alt_text  # , EmbeddingPurpose.QUERY
        )

        # chunk search
        chunk_results = []
        if search_settings.chunk_settings.enabled:
            chunk_results = await self._vector_search_logic(
                query_text=user_text,  # used for text-based stuff & re-ranking
                search_settings=search_settings,
                precomputed_vector=vec,  # use the alt_text vector for semantic/hybrid
            )

        # graph search
        graph_results = []
        if search_settings.graph_settings.enabled:
            graph_results = await self._graph_search_logic(
                query_text=user_text,  # or alt_text if you prefer
                search_settings=search_settings,
                precomputed_vector=vec,
            )

        return (chunk_results, graph_results)

    async def _vector_search_logic(
        self,
        query_text: str,
        search_settings: SearchSettings,
        precomputed_vector: Optional[list[float]] = None,
    ) -> list[ChunkSearchResult]:
        """
        • If precomputed_vector is given, use it for semantic/hybrid search.
        Otherwise embed query_text ourselves.
        • Then do fulltext, semantic, or hybrid search.
        • Optionally re-rank and return results.
        """
        if not search_settings.chunk_settings.enabled:
            return []

        # 1) Possibly embed
        query_vector = precomputed_vector
        if query_vector is None and (
            search_settings.use_semantic_search
            or search_settings.use_hybrid_search
        ):
            query_vector = (
                await self.providers.completion_embedding.async_get_embedding(
                    query_text  # , EmbeddingPurpose.QUERY
                )
            )

        # 2) Choose which search to run
        if (
            search_settings.use_fulltext_search
            and search_settings.use_semantic_search
        ) or search_settings.use_hybrid_search:
            raw_results = (
                await self.providers.database.chunks_handler.hybrid_search(
                    query_vector=query_vector,
                    query_text=query_text,
                    search_settings=search_settings,
                )
            )
        elif search_settings.use_fulltext_search:
            raw_results = (
                await self.providers.database.chunks_handler.full_text_search(
                    query_text=query_text,
                    search_settings=search_settings,
                )
            )
        elif search_settings.use_semantic_search:
            raw_results = (
                await self.providers.database.chunks_handler.semantic_search(
                    query_vector=query_vector,
                    search_settings=search_settings,
                )
            )
        else:
            raise ValueError(
                "At least one of use_fulltext_search or use_semantic_search must be True"
            )

        # 3) Re-rank
        reranked = await self.providers.completion_embedding.arerank(
            query=query_text, results=raw_results, limit=search_settings.limit
        )

        # 4) Possibly augment text or metadata
        final_results = []
        for r in reranked:
            if "title" in r.metadata and search_settings.include_metadatas:
                title = r.metadata["title"]
                r.text = f"Document Title: {title}\n\nText: {r.text}"
            r.metadata["associated_query"] = query_text
            final_results.append(r)

        return final_results

    async def _graph_search_logic(
        self,
        query_text: str,
        search_settings: SearchSettings,
        precomputed_vector: Optional[list[float]] = None,
    ) -> list[GraphSearchResult]:
        """
        Mirrors your previous GraphSearch approach:
        • if precomputed_vector is supplied, use that
        • otherwise embed query_text
        • search entities, relationships, communities
        • return results
        """
        results = []
        if not search_settings.graph_settings.enabled:
            return results

        # 1) Possibly embed
        query_embedding = precomputed_vector
        if query_embedding is None:
            query_embedding = (
                await self.providers.completion_embedding.async_get_embedding(
                    query_text
                )
            )

        base_limit = search_settings.limit
        graph_limits = search_settings.graph_settings.limits or {}

        # Entity search
        entity_limit = graph_limits.get("entities", base_limit)
        entity_cursor = self.providers.database.graphs_handler.graph_search(
            query_text,
            search_type="entities",
            limit=entity_limit,
            query_embedding=query_embedding,
            property_names=["name", "description", "id"],
            filters=search_settings.filters,
        )
        async for ent in entity_cursor:
            score = ent.get("similarity_score")
            metadata = ent.get("metadata", {})
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except:
                    pass

            results.append(
                GraphSearchResult(
                    id=ent.get("id", None),
                    content=GraphEntityResult(
                        name=ent.get("name", ""),
                        description=ent.get("description", ""),
                        id=ent.get("id", None),
                    ),
                    result_type=GraphSearchResultType.ENTITY,
                    score=score if search_settings.include_scores else None,
                    metadata=(
                        {
                            **(metadata or {}),
                            "associated_query": query_text,
                        }
                        if search_settings.include_metadatas
                        else None
                    ),
                )
            )

        # Relationship search
        rel_limit = graph_limits.get("relationships", base_limit)
        rel_cursor = self.providers.database.graphs_handler.graph_search(
            query_text,
            search_type="relationships",
            limit=rel_limit,
            query_embedding=query_embedding,
            property_names=[
                "id",
                "subject",
                "predicate",
                "object",
                "description",
                "subject_id",
                "object_id",
            ],
            filters=search_settings.filters,
        )
        async for rel in rel_cursor:
            score = rel.get("similarity_score")
            metadata = rel.get("metadata", {})
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except:
                    pass

            results.append(
                GraphSearchResult(
                    id=ent.get("id", None),
                    content=GraphRelationshipResult(
                        id=rel.get("id", None),
                        subject=rel.get("subject", ""),
                        predicate=rel.get("predicate", ""),
                        object=rel.get("object", ""),
                        subject_id=rel.get("subject_id", None),
                        object_id=rel.get("object_id", None),
                        description=rel.get("description", ""),
                    ),
                    result_type=GraphSearchResultType.RELATIONSHIP,
                    score=score if search_settings.include_scores else None,
                    metadata=(
                        {
                            **(metadata or {}),
                            "associated_query": query_text,
                        }
                        if search_settings.include_metadatas
                        else None
                    ),
                )
            )

        # Community search
        comm_limit = graph_limits.get("communities", base_limit)
        comm_cursor = self.providers.database.graphs_handler.graph_search(
            query_text,
            search_type="communities",
            limit=comm_limit,
            query_embedding=query_embedding,
            property_names=[
                "id",
                "name",
                "summary",
            ],
            filters=search_settings.filters,
        )
        async for comm in comm_cursor:
            score = comm.get("similarity_score")
            metadata = comm.get("metadata", {})
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except:
                    pass

            results.append(
                GraphSearchResult(
                    id=ent.get("id", None),
                    content=GraphCommunityResult(
                        id=comm.get("id", None),
                        name=comm.get("name", ""),
                        summary=comm.get("summary", ""),
                    ),
                    result_type=GraphSearchResultType.COMMUNITY,
                    score=score if search_settings.include_scores else None,
                    metadata=(
                        {
                            **(metadata or {}),
                            "associated_query": query_text,
                        }
                        if search_settings.include_metadatas
                        else None
                    ),
                )
            )

        return results

    async def _run_hyde_generation(
        self,
        query: str,
        num_sub_queries: int = 2,
    ) -> list[str]:
        """
        Calls the LLM with a 'HyDE' style prompt to produce multiple
        hypothetical documents/answers, one per line or separated by blank lines.
        """
        # Retrieve the prompt template from your database or config:
        # e.g. your "hyde" prompt has placeholders: {message}, {num_outputs}
        hyde_template = (
            await self.providers.database.prompts_handler.get_cached_prompt(
                prompt_name="hyde",
                inputs={"message": query, "num_outputs": num_sub_queries},
            )
        )

        # Now call the LLM with that as the system or user prompt:
        completion_config = GenerationConfig(
            model=self.config.app.fast_llm,  # or whichever short/cheap model
            max_tokens=512,
            temperature=0.7,
            stream=False,
        )

        response = await self.providers.llm.aget_completion(
            messages=[{"role": "system", "content": hyde_template}],
            generation_config=completion_config,
        )

        # Suppose the LLM returns something like:
        #
        # "Doc1. Some made up text.\n\nDoc2. Another made up text.\n\n"
        #
        # So we split by double-newline or some pattern:
        raw_text = response.choices[0].message.content
        hypothetical_docs = [
            chunk.strip() for chunk in raw_text.split("\n\n") if chunk.strip()
        ]

        return hypothetical_docs

    @telemetry_event("SearchDocuments")
    async def search_documents(
        self,
        query: str,
        settings: SearchSettings,
        query_embedding: Optional[list[float]] = None,
    ) -> list[DocumentResponse]:
        return (
            await self.providers.database.documents_handler.search_documents(
                query_text=query,
                settings=settings,
                query_embedding=query_embedding,
            )
        )

    @telemetry_event("Completion")
    async def completion(
        self,
        messages: list[dict],
        generation_config: GenerationConfig,
        *args,
        **kwargs,
    ):
        return await self.providers.llm.aget_completion(
            [message.to_dict() for message in messages],
            generation_config,
            *args,
            **kwargs,
        )

    @telemetry_event("Embedding")
    async def embedding(
        self,
        text: str,
    ):
        return await self.providers.completion_embedding.async_get_embedding(
            text=text
        )

    @telemetry_event("RAG")
    async def rag(
        self,
        query: str,
        rag_generation_config: GenerationConfig,
        search_settings: SearchSettings = SearchSettings(),
        system_prompt_name: str | None = None,
        task_prompt_name: str | None = None,
        **kwargs,
    ) -> Any:
        """
        A single RAG method that can do EITHER a one-shot synchronous RAG or
        streaming SSE-based RAG, depending on rag_generation_config.stream.

        1) Perform aggregator search => context
        2) Build system+task prompts => messages
        3) If not streaming => normal LLM call => return RAGResponse
        4) If streaming => return an async generator of SSE lines
        """
        # 1) Possibly fix up any UUID filters in search_settings
        for f, val in list(search_settings.filters.items()):
            if isinstance(val, UUID):
                search_settings.filters[f] = str(val)

        try:
            # 2) Perform search => aggregated_results
            aggregated_results = await self.search(query, search_settings)

            # 3) Build context from aggregator
            collector = SearchResultsCollector()
            collector.add_aggregate_result(aggregated_results)
            context_str = format_search_results_for_llm(
                aggregated_results, collector
            )

            # 4) Prepare system+task messages
            system_prompt_name = system_prompt_name or "system"
            task_prompt_name = task_prompt_name or "rag"
            task_prompt_override = kwargs.get("task_prompt_override")

            messages = await self.providers.database.prompts_handler.get_message_payload(
                system_prompt_name=system_prompt_name,
                task_prompt_name=task_prompt_name,
                task_inputs={"query": query, "context": context_str},
                task_prompt_override=task_prompt_override,
            )

            # 5) Check streaming vs. non-streaming
            if not rag_generation_config.stream:
                # ========== Non-Streaming Logic ==========
                response = await self.providers.llm.aget_completion(
                    messages=messages,
                    generation_config=rag_generation_config,
                )
                llm_text = response.choices[0].message.content

                # (a) Extract short-ID references from final text
                raw_sids = extract_citations(llm_text)

                # (b) Possibly prune large content out of metadata
                metadata = response.dict()
                if "choices" in metadata and len(metadata["choices"]) > 0:
                    metadata["choices"][0]["message"].pop("content", None)

                # (c) Build final RAGResponse
                rag_resp = RAGResponse(
                    generated_answer=llm_text,
                    search_results=aggregated_results,
                    citations=[
                        {
                            "id": f"cit_{sid}",
                            "object": "citation",
                            "payload": self._find_item_by_shortid(
                                sid, collector
                            ),
                        }
                        for sid in raw_sids
                    ],
                    metadata=metadata,
                    completion=llm_text,
                )
                return rag_resp

            else:
                # ========== Streaming SSE Logic ==========
                async def sse_generator() -> AsyncGenerator[str, None]:
                    # 1) Emit search results via SSEFormatter
                    async for line in SSEFormatter.yield_search_results_event(
                        aggregated_results
                    ):
                        yield line

                    announced_ids = (
                        set()
                    )  # keep track of which short IDs we’ve announced
                    partial_text_buffer = ""
                    citations = []

                    # Begin streaming from the LLM
                    msg_stream = self.providers.llm.aget_completion_stream(
                        messages=messages,
                        generation_config=rag_generation_config,
                    )

                    try:
                        async for chunk in msg_stream:
                            delta = chunk.choices[0].delta
                            finish_reason = chunk.choices[0].finish_reason
                            # if delta.thinking:
                            # check if delta has `thinking` attribute

                            if hasattr(delta, "thinking") and delta.thinking:
                                # Emit SSE "thinking" event
                                async for (
                                    line
                                ) in SSEFormatter.yield_thinking_event(
                                    delta.thinking
                                ):
                                    yield line

                            if delta.content:
                                # (b) Emit SSE "message" event for this chunk of text
                                async for (
                                    line
                                ) in SSEFormatter.yield_message_event(
                                    delta.content
                                ):
                                    yield line

                                # Accumulate new text
                                partial_text_buffer += delta.content

                                # (a) Extract citations from updated buffer
                                #     For each *new* short ID, emit an SSE "citation" event
                                found_ids = extract_citations(
                                    partial_text_buffer
                                )
                                for sid in found_ids:
                                    if sid not in announced_ids:
                                        announced_ids.add(sid)
                                        citation_payload = {
                                            "id": f"cit_{sid}",
                                            "short_id": sid,
                                            "payload": self._find_item_by_shortid(
                                                sid, collector
                                            ),
                                        }
                                        citations.append(citation_payload)
                                        # Emit citation
                                        async for (
                                            line
                                        ) in SSEFormatter.yield_citation_event(
                                            citation_payload
                                        ):
                                            yield line

                            # If the LLM signals it’s done
                            if finish_reason == "stop":
                                # (c) Emit final answer + all collected citations
                                final_answer_evt = {
                                    "id": "msg_final",
                                    "object": "rag.final_answer",
                                    "generated_answer": partial_text_buffer,
                                    "citations": citations,
                                }
                                async for (
                                    line
                                ) in SSEFormatter.yield_final_answer_event(
                                    final_answer_evt
                                ):
                                    yield line

                                # (d) Signal the end of the SSE stream
                                yield SSEFormatter.yield_done_event()
                                break

                    except Exception as e:
                        logger.error(f"Error streaming LLM in rag: {e}")
                        # Optionally yield an SSE "error" event or handle differently
                        raise

                return sse_generator()

        except Exception as e:
            logger.exception(f"Error in RAG pipeline: {e}")
            if "NoneType" in str(e):
                raise HTTPException(
                    status_code=502,
                    detail="Server not reachable or returned an invalid response",
                )
            raise HTTPException(
                status_code=500,
                detail=f"Internal RAG Error - {str(e)}",
            )

        except Exception as e:
            logger.exception(f"Error in RAG pipeline: {e}")
            if "NoneType" in str(e):
                raise HTTPException(
                    status_code=502,
                    detail="Server not reachable or returned an invalid response",
                )
            raise HTTPException(
                status_code=500,
                detail=f"Internal RAG Error - {str(e)}",
            )

    def _find_item_by_shortid(
        self, sid: str, collector: SearchResultsCollector
    ) -> Optional[tuple[str, Any, int]]:
        """
        Example helper that tries to match aggregator items by short ID,
        meaning result_obj.id starts with sid.
        """
        for source_type, result_obj in collector.get_all_results():
            # if the aggregator item has an 'id' attribute
            if getattr(result_obj, "id", None) is not None:
                full_id_str = str(result_obj.id)
                if full_id_str.startswith(sid):
                    if source_type == "chunk":
                        return (
                            result_obj.as_dict()
                        )  # (source_type, result_obj.as_dict())
                    else:
                        return result_obj  # (source_type, result_obj)
        return None

    @telemetry_event("Agent")
    async def agent(
        self,
        rag_generation_config: GenerationConfig,
        search_settings: SearchSettings = SearchSettings(),
        task_prompt_override: Optional[str] = None,
        include_title_if_available: Optional[bool] = False,
        conversation_id: Optional[UUID] = None,
        message: Optional[Message] = None,
        messages: Optional[list[Message]] = None,
        use_system_context: bool = False,
        max_tool_context_length: int = 32_768,
        override_tools: Optional[list[dict[str, Any]]] = None,
        mode: Optional[Literal["rag", "research"]] = "rag",
    ):
        try:
            if message and messages:
                raise R2RException(
                    status_code=400,
                    message="Only one of message or messages should be provided",
                )

            if not message and not messages:
                raise R2RException(
                    status_code=400,
                    message="Either message or messages should be provided",
                )

            # Ensure 'message' is a Message instance
            if message and not isinstance(message, Message):
                if isinstance(message, dict):
                    message = Message.from_dict(message)
                else:
                    raise R2RException(
                        status_code=400,
                        message="""
                            Invalid message format. The expected format contains:
                                role: MessageType | 'system' | 'user' | 'assistant' | 'function'
                                content: Optional[str]
                                name: Optional[str]
                                function_call: Optional[dict[str, Any]]
                                tool_calls: Optional[list[dict[str, Any]]]
                                """,
                    )

            # Ensure 'messages' is a list of Message instances
            if messages:
                processed_messages = []
                for message in messages:
                    if isinstance(message, Message):
                        processed_messages.append(message)
                    elif hasattr(message, "dict"):
                        processed_messages.append(
                            Message.from_dict(message.dict())
                        )
                    elif isinstance(message, dict):
                        processed_messages.append(Message.from_dict(message))
                    else:
                        processed_messages.append(
                            Message.from_dict(str(message))
                        )
                messages = processed_messages
            else:
                messages = []

            # Transform UUID filters to strings
            for filter_key, value in search_settings.filters.items():
                if isinstance(value, UUID):
                    search_settings.filters[filter_key] = str(value)

            ids = []
            needs_conversation_name = False
            if conversation_id:  # Fetch the existing conversation
                try:
                    conversation_messages = await self.providers.database.conversations_handler.get_conversation(
                        conversation_id=conversation_id,
                    )
                    needs_conversation_name = len(conversation_messages) == 0
                except Exception as e:
                    logger.error(f"Error fetching conversation: {str(e)}")

                if conversation_messages is not None:
                    messages_from_conversation: list[Message] = []
                    for message_response in conversation_messages:
                        if isinstance(message_response, MessageResponse):
                            messages_from_conversation.append(
                                message_response.message
                            )
                            ids.append(message_response.id)
                        else:
                            logger.warning(
                                f"Unexpected type in conversation found: {type(message_response)}\n{message_response}"
                            )
                    messages = messages_from_conversation + messages
            else:  # Create new conversation
                conversation_response = (
                    await self.providers.database.conversations_handler.create_conversation()
                )
                conversation_id = conversation_response.id
                needs_conversation_name = True

            if message:
                messages.append(message)

            if not messages:
                raise R2RException(
                    status_code=400,
                    message="No messages to process",
                )

            current_message = messages[-1]
            logger.info(
                f"Running the agent with conversation_id = {conversation_id} and message = {current_message}"
            )
            # Save the new message to the conversation
            parent_id = ids[-1] if ids else None
            message_response = await self.providers.database.conversations_handler.add_message(
                conversation_id=conversation_id,
                content=current_message,
                parent_id=parent_id,
            )

            message_id = (
                message_response.id if message_response is not None else None
            )

            # -- Step 1: parse the filter dict from search_settings
            #    (assuming search_settings.filters is the dict you want to parse)
            filter_user_id, filter_collection_ids = (
                self._parse_user_and_collection_filters(
                    search_settings.filters
                )
            )

            system_instruction = None

            if use_system_context and task_prompt_override:
                raise R2RException(
                    status_code=400,
                    message="Both use_system_context and task_prompt_override cannot be True at the same time",
                )

            # STEP 1: Determine the final system prompt content
            if task_prompt_override:
                system_instruction = task_prompt_override
            else:
                system_instruction = (
                    await self._build_aware_system_instruction(
                        max_tool_context_length=max_tool_context_length,
                        filter_user_id=filter_user_id,
                        filter_collection_ids=filter_collection_ids,
                        model=rag_generation_config.model,
                        use_system_context=use_system_context,
                    )
                )

            agent_config = deepcopy(self.config.agent)
            agent_config.tools = override_tools or agent_config.tools

            if rag_generation_config.stream:

                async def stream_response():
                    try:
                        if (
                            "openai" in rag_generation_config.model.lower()
                            or "anthropic"
                            in rag_generation_config.model.lower()
                        ):
                            agent = R2RStreamingRAGAgent(
                                database_provider=self.providers.database,
                                llm_provider=self.providers.llm,
                                config=agent_config,
                                search_settings=search_settings,
                                rag_generation_config=rag_generation_config,
                                max_tool_context_length=max_tool_context_length,
                                knowledge_search_method=self.search,
                                content_method=self.get_context,
                                file_search_method=self.search_documents,
                            )
                        else:
                            agent_config.include_tools = False
                            agent = R2RXMLToolsStreamingRAGAgent(
                                database_provider=self.providers.database,
                                llm_provider=self.providers.llm,
                                config=agent_config,
                                search_settings=search_settings,
                                rag_generation_config=rag_generation_config,
                                max_tool_context_length=max_tool_context_length,
                                knowledge_search_method=self.search,
                                content_method=self.get_context,
                                file_search_method=self.search_documents,
                            )

                        async for chunk in agent.arun(
                            messages=messages,
                            system_instruction=system_instruction,
                            include_title_if_available=include_title_if_available,
                        ):
                            yield chunk
                    except Exception as e:
                        logger.error(f"Error streaming agent output: {e}")
                        raise e
                    finally:
                        msgs = [
                            msg.to_dict()
                            for msg in agent.conversation.messages
                        ]
                        input_tokens = num_tokens_from_messages(msgs[:-1])
                        output_tokens = num_tokens_from_messages([msgs[-1]])
                        await self.providers.database.conversations_handler.add_message(
                            conversation_id=conversation_id,
                            content=agent.conversation.messages[-1],
                            parent_id=message_id,
                            metadata={
                                "input_tokens": input_tokens,
                                "output_tokens": output_tokens,
                            },
                        )
                        # TODO  - no copy pasta!
                        if needs_conversation_name:
                            try:
                                prompt = f"Generate a succinct name (3-6 words) for this conversation, given the first input mesasge here = {str(message.to_dict())}"
                                conversation_name = (
                                    (
                                        await self.providers.llm.aget_completion(
                                            [
                                                {
                                                    "role": "system",
                                                    "content": prompt,
                                                }
                                            ],
                                            GenerationConfig(
                                                model=self.config.app.fast_llm
                                            ),
                                        )
                                    )
                                    .choices[0]
                                    .message.content
                                )
                                await self.providers.database.conversations_handler.update_conversation(
                                    conversation_id=conversation_id,
                                    name=conversation_name,
                                )
                            except Exception as e:
                                logger.error(
                                    f"Error generating conversation name: {e}"
                                )

                return stream_response()

            # For non-streaming mode, select the appropriate agent type based on model
            if (
                "openai" in rag_generation_config.model.lower()
                or "anthropic" in rag_generation_config.model.lower()
            ):
                if mode == "rag":
                    agent = R2RRAGAgent(
                        database_provider=self.providers.database,
                        llm_provider=self.providers.llm,
                        config=agent_config,
                        search_settings=search_settings,
                        rag_generation_config=rag_generation_config,
                        max_tool_context_length=max_tool_context_length,
                        knowledge_search_method=self.search,
                        content_method=self.get_context,
                        file_search_method=self.search_documents,
                    )
                else:
                    # Use the standard RAG agent for other models
                    agent = R2RResearchAgent(
                        database_provider=self.providers.database,
                        llm_provider=self.providers.llm,
                        config=agent_config,
                        search_settings=search_settings,
                        rag_generation_config=rag_generation_config,
                        max_tool_context_length=max_tool_context_length,
                        knowledge_search_method=self.search,
                        content_method=self.get_context,
                        file_search_method=self.search_documents,
                    )
            else:
                if mode != "rag":
                    raise R2RException(
                        status_code=400,
                        message="Research mode is only supported for OpenAI and Anthropic models",
                    )
                # Use the new R2RXMLToolsRAGAgent for non-streaming XML parsing for deepseek
                agent_config.include_tools = False
                agent = R2RXMLToolsRAGAgent(
                    database_provider=self.providers.database,
                    llm_provider=self.providers.llm,
                    config=agent_config,
                    search_settings=search_settings,
                    rag_generation_config=rag_generation_config,
                    max_tool_context_length=max_tool_context_length,
                    knowledge_search_method=self.search,
                    content_method=self.get_context,
                    file_search_method=self.search_documents,
                )

            results = await agent.arun(
                messages=messages,
                system_instruction=system_instruction,
                include_title_if_available=include_title_if_available,
            )

            # Save the assistant's reply to the conversation
            if isinstance(results[-1], dict):
                assistant_message = Message(**results[-1])
            elif isinstance(results[-1], Message):
                assistant_message = results[-1]
            else:
                assistant_message = Message(
                    role="assistant", content=str(results[-1])
                )

            if hasattr(agent, "search_results_collector"):
                collector = agent.search_results_collector
            else:
                collector = SearchResultsCollector()  # or fallback if needed

            # Suppose your final assistant text is:
            raw_text = assistant_message.content or ""

            short_ids = extract_citations(raw_text)  # e.g. [abc1234]

            final_citations = []
            for sid in short_ids:
                final_citations.append(
                    {
                        "id": f"cit_{sid}",
                        "object": "citation",
                        "short_id": sid,
                        "payload": collector.find_by_short_id(sid),
                    }
                )
            # Persist everything in the conversation DB
            await self.providers.database.conversations_handler.add_message(
                conversation_id=str(conversation_id),
                content=assistant_message,
                parent_id=message_id,
                metadata={
                    "citations": final_citations,
                    # You can also store the entire collector or just dump the underlying results
                    "aggregated_search_result": json.dumps(
                        dump_collector(collector)
                    ),
                },
            )

            if needs_conversation_name:
                conversation_name = None
                try:
                    prompt = f"Generate a succinct name (3-6 words) for this conversation, given the first input mesasge here = {str(message.to_dict())}"
                    conversation_name = (
                        (
                            await self.providers.llm.aget_completion(
                                [{"role": "system", "content": prompt}],
                                GenerationConfig(
                                    model=self.config.app.fast_llm
                                ),
                            )
                        )
                        .choices[0]
                        .message.content
                    )
                except Exception as e:
                    pass
                finally:
                    await self.providers.database.conversations_handler.update_conversation(
                        conversation_id=conversation_id,
                        name=conversation_name or "",
                    )

            return {
                "messages": [
                    Message(
                        role="assistant",
                        content=assistant_message.content,
                        metadata={
                            "citations": final_citations,
                            "tool_calls": agent.tool_calls,
                            # You can also store the entire collector or just dump the underlying results
                            "aggregated_search_result": json.dumps(
                                dump_collector(collector)
                            ),
                        },
                    )
                ],
                "conversation_id": str(
                    conversation_id
                ),  # Ensure it's a string
            }

        except Exception as e:
            logger.error(f"Error in agent response: {str(e)}")
            if "NoneType" in str(e):
                raise HTTPException(
                    status_code=502,
                    detail="Server not reachable or returned an invalid response",
                )
            raise HTTPException(
                status_code=500,
                detail=f"Internal Server Error - {str(e)}",
            )

    async def get_context(
        self,
        filters: dict[str, Any],
        options: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """
        Return an ordered list of documents (with minimal overview fields),
        plus all associated chunks in ascending chunk order.

        Only the filters: owner_id, collection_ids, and document_id
        are supported. If any other filter or operator is passed in,
        we raise an error.

        Args:
            filters: A dictionary describing the allowed filters
                     (owner_id, collection_ids, document_id).
            options: A dictionary with extra options, e.g. include_summary_embedding
                     or any custom flags for additional logic.

        Returns:
            A list of dicts, where each dict has:
              {
                "document": <DocumentResponse>,
                "chunks": [ <chunk0>, <chunk1>, ... ]
              }
        """
        # 2. Fetch matching documents
        matching_docs = await self.providers.database.documents_handler.get_documents_overview(
            offset=0,
            limit=-1,
            filters=filters,
            include_summary_embedding=options.get(
                "include_summary_embedding", False
            ),
        )

        if not matching_docs["results"]:
            return []

        # 3. For each document, fetch associated chunks in ascending chunk order
        results = []
        for doc_response in matching_docs["results"]:
            doc_id = doc_response.id
            chunk_data = await self.providers.database.chunks_handler.list_document_chunks(
                document_id=doc_id,
                offset=0,
                limit=-1,  # get all chunks
                include_vectors=False,
            )
            chunks = chunk_data["results"]  # already sorted by chunk_order

            # 4. Build a returned structure that includes doc + chunks
            results.append(
                {
                    "document": doc_response.model_dump(),
                    # or doc_response.dict() or doc_response.model_dump()
                    "chunks": chunks,
                }
            )

        return results

    def _parse_user_and_collection_filters(
        self,
        filters: dict[str, Any],
    ):
        ### TODO - Come up with smarter way to extract owner / collection ids for non-admin
        filter_starts_with_and = filters.get("$and", None)
        filter_starts_with_or = filters.get("$or", None)
        if filter_starts_with_and:
            try:
                filter_starts_with_and_then_or = filter_starts_with_and[0][
                    "$or"
                ]

                user_id = filter_starts_with_and_then_or[0]["owner_id"]["$eq"]
                collection_ids = filter_starts_with_and_then_or[1][
                    "collection_ids"
                ]["$overlap"]
                return user_id, [str(ele) for ele in collection_ids]
            except Exception as e:
                logger.error(
                    f"Error: {e}.\n\n While"
                    + """ parsing filters: expected format {'$or': [{'owner_id': {'$eq': 'uuid-string-here'}, 'collection_ids': {'$overlap': ['uuid-of-some-collection']}}]}, if you are a superuser then this error can be ignored."""
                )
                return None, []
        elif filter_starts_with_or:
            try:
                user_id = filter_starts_with_or[0]["owner_id"]["$eq"]
                collection_ids = filter_starts_with_or[1]["collection_ids"][
                    "$overlap"
                ]
                return user_id, [str(ele) for ele in collection_ids]
            except Exception as e:
                logger.error(
                    """Error parsing filters: expected format {'$or': [{'owner_id': {'$eq': 'uuid-string-here'}, 'collection_ids': {'$overlap': ['uuid-of-some-collection']}}]}, if you are a superuser then this error can be ignored."""
                )
                return None, []
        else:
            # Admin user
            return None, []

    async def _build_documents_context(
        self,
        filter_user_id: Optional[UUID] = None,
        max_summary_length: int = 128,
        limit: int = 1000,
    ) -> str:
        """
        Fetches documents matching the given filters and returns a formatted string
        enumerating them.
        """
        # We only want up to `limit` documents for brevity
        docs_data = await self.providers.database.documents_handler.get_documents_overview(
            offset=0,
            limit=limit,
            filter_user_ids=[filter_user_id] if filter_user_id else None,
            include_summary_embedding=False,
        )

        docs = docs_data["results"]
        if not docs:
            return "No documents found."

        lines = []
        for i, doc in enumerate(docs, start=1):
            if (
                not doc.summary
                or doc.ingestion_status != IngestionStatus.SUCCESS
            ):
                lines.append(
                    f"[{i}] Title: {doc.title}, Summary: (Summary not available), Status:{doc.ingestion_status} ID: {doc.id}"
                )
                continue

            # Build a line referencing the doc
            title = doc.title or "(Untitled Document)"
            lines.append(
                f"[{i}] Title: {title}, Summary: {doc.summary[0:max_summary_length] + ('...' if len(doc.summary) > max_summary_length else ''),}, Total Tokens: {doc.total_tokens}, ID: {doc.id}"
            )
        return "\n".join(lines)

    async def _build_aware_system_instruction(
        self,
        max_tool_context_length: int = 10_000,
        filter_user_id: Optional[UUID] = None,
        filter_collection_ids: Optional[list[UUID]] = None,
        model: Optional[str] = None,
        use_system_context: bool = False,
    ) -> str:
        """
        High-level method that:
          1) builds the documents context
          2) builds the collections context
          3) loads the new `dynamic_reasoning_rag_agent` prompt
        """
        date_str = str(datetime.now().isoformat()).split("T")[0]

        # "dynamic_rag_agent" // "static_rag_agent"

        prompt_name = (
            self.config.agent.agent_dynamic_prompt
            if use_system_context
            else self.config.agent.agent_static_prompt
        )

        if "deepseek" in model or "gemini" in model:
            prompt_name = prompt_name + "_xml_tooling"

        if use_system_context:
            doc_context_str = await self._build_documents_context(
                filter_user_id=filter_user_id,
            )
            logger.debug(f"Loading prompt {prompt_name}")
            # Now fetch the prompt from the database prompts handler
            # This relies on your "rag_agent_extended" existing with
            # placeholders: date, document_context
            system_prompt = await self.providers.database.prompts_handler.get_cached_prompt(
                # We use custom tooling and a custom agent to handle gemini models
                prompt_name,
                inputs={
                    "date": date_str,
                    "max_tool_context_length": max_tool_context_length,
                    "document_context": doc_context_str,
                },
            )
        else:
            system_prompt = await self.providers.database.prompts_handler.get_cached_prompt(
                prompt_name,
                inputs={
                    "date": date_str,
                },
            )
        logger.info(f"Running agent with system prompt = {system_prompt}")
        return system_prompt


class RetrievalServiceAdapter:
    @staticmethod
    def _parse_user_data(user_data):
        if isinstance(user_data, str):
            try:
                user_data = json.loads(user_data)
            except json.JSONDecodeError:
                raise ValueError(f"Invalid user data format: {user_data}")
        return User.from_dict(user_data)

    @staticmethod
    def prepare_search_input(
        query: str,
        search_settings: SearchSettings,
        user: User,
    ) -> dict:
        return {
            "query": query,
            "search_settings": search_settings.to_dict(),
            "user": user.to_dict(),
        }

    @staticmethod
    def parse_search_input(data: dict):
        return {
            "query": data["query"],
            "search_settings": SearchSettings.from_dict(
                data["search_settings"]
            ),
            "user": RetrievalServiceAdapter._parse_user_data(data["user"]),
        }

    @staticmethod
    def prepare_rag_input(
        query: str,
        search_settings: SearchSettings,
        rag_generation_config: GenerationConfig,
        task_prompt_override: Optional[str],
        user: User,
    ) -> dict:
        return {
            "query": query,
            "search_settings": search_settings.to_dict(),
            "rag_generation_config": rag_generation_config.to_dict(),
            "task_prompt_override": task_prompt_override,
            "user": user.to_dict(),
        }

    @staticmethod
    def parse_rag_input(data: dict):
        return {
            "query": data["query"],
            "search_settings": SearchSettings.from_dict(
                data["search_settings"]
            ),
            "rag_generation_config": GenerationConfig.from_dict(
                data["rag_generation_config"]
            ),
            "task_prompt_override": data["task_prompt_override"],
            "user": RetrievalServiceAdapter._parse_user_data(data["user"]),
        }

    @staticmethod
    def prepare_agent_input(
        message: Message,
        search_settings: SearchSettings,
        rag_generation_config: GenerationConfig,
        task_prompt_override: Optional[str],
        include_title_if_available: bool,
        user: User,
        conversation_id: Optional[str] = None,
    ) -> dict:
        return {
            "message": message.to_dict(),
            "search_settings": search_settings.to_dict(),
            "rag_generation_config": rag_generation_config.to_dict(),
            "task_prompt_override": task_prompt_override,
            "include_title_if_available": include_title_if_available,
            "user": user.to_dict(),
            "conversation_id": conversation_id,
        }

    @staticmethod
    def parse_agent_input(data: dict):
        return {
            "message": Message.from_dict(data["message"]),
            "search_settings": SearchSettings.from_dict(
                data["search_settings"]
            ),
            "rag_generation_config": GenerationConfig.from_dict(
                data["rag_generation_config"]
            ),
            "task_prompt_override": data["task_prompt_override"],
            "include_title_if_available": data["include_title_if_available"],
            "user": RetrievalServiceAdapter._parse_user_data(data["user"]),
            "conversation_id": data.get("conversation_id"),
        }
