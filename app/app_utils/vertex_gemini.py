import os
import asyncio
import google.auth
import logging
from typing import AsyncGenerator
from google.adk.models.google_llm import Gemini, _build_response_log
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.utils.context_utils import Aclosing
from google.adk.utils.streaming_utils import StreamingResponseAggregator
from google.genai import Client
from google.genai import types

logger = logging.getLogger("google_adk")

class VertexGemini(Gemini):
    @property
    def api_client(self) -> Client:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
            
        if not hasattr(self, "_clients_by_loop"):
            self._clients_by_loop = {}
            
        # Clean up closed loops to avoid memory leak
        for lp in list(self._clients_by_loop.keys()):
            if lp is not None and lp.is_closed():
                try:
                    self._clients_by_loop[lp].close()
                except Exception:
                    pass
                del self._clients_by_loop[lp]
                
        if loop not in self._clients_by_loop:
            from app.app_utils.env_resolver import get_project_id, get_region
            project = get_project_id()
            location = get_region()
            os.environ.pop("GEMINI_API_KEY", None)
            os.environ.pop("GOOGLE_API_KEY", None)
            
            import google.auth
            credentials, _ = google.auth.default()
            
            self._clients_by_loop[loop] = Client(
                vertexai=True,
                project=project,
                location=location,
                credentials=credentials
            )
            
        return self._clients_by_loop[loop]

    @property
    def _live_api_client(self) -> Client:
        """Avoid closed event loop for live/voice connections by resolving dynamically."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
            
        if not hasattr(self, "_live_clients_by_loop"):
            self._live_clients_by_loop = {}
            
        # Clean up closed loops
        for lp in list(self._live_clients_by_loop.keys()):
            if lp is not None and lp.is_closed():
                try:
                    self._live_clients_by_loop[lp].close()
                except Exception:
                    pass
                del self._live_clients_by_loop[lp]
                
        if loop not in self._live_clients_by_loop:
            from app.app_utils.env_resolver import get_project_id, get_region
            project = get_project_id()
            location = get_region()
            base_url, _ = self._base_url_and_api_version
            
            os.environ.pop("GEMINI_API_KEY", None)
            os.environ.pop("GOOGLE_API_KEY", None)
            
            import google.auth
            credentials, _ = google.auth.default()
            
            self._live_clients_by_loop[loop] = Client(
                vertexai=True,
                project=project,
                location=location,
                credentials=credentials,
                http_options=types.HttpOptions(
                    headers=self._tracking_headers(),
                    api_version=self._live_api_version,
                    base_url=base_url,
                )
            )
            
        return self._live_clients_by_loop[loop]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        """Override to delegate calls to the synchronous client via asyncio.to_thread.
        
        This forces use of the synchronous 'requests'-based Client, which correctly
        leverages Mutual TLS (mTLS) in Vertex AI Reasoning Engine / Workload Identity.
        """
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("GOOGLE_API_KEY", None)
        await self._preprocess_request(llm_request)
        self._maybe_append_user_content(llm_request)

        # Handle context caching if configured
        cache_metadata = None
        cache_manager = None
        if llm_request.cache_config:
            from google.adk.telemetry.tracing import tracer
            from google.adk.models.gemini_context_cache_manager import GeminiContextCacheManager

            with tracer.start_as_current_span('handle_context_caching') as span:
                cache_manager = GeminiContextCacheManager(self.api_client)
                cache_metadata = await cache_manager.handle_context_caching(llm_request)
                if cache_metadata:
                    if cache_metadata.cache_name:
                        span.set_attribute('cache_action', 'active_cache')
                        span.set_attribute('cache_name', cache_metadata.cache_name)
                    else:
                        span.set_attribute('cache_action', 'fingerprint_only')

        logger.info(
            'Sending out request, model: %s, backend: %s, stream: %s (thread-delegated mTLS)',
            llm_request.model,
            self._api_backend,
            stream,
        )

        if llm_request.config:
            if not llm_request.config.http_options:
                llm_request.config.http_options = types.HttpOptions()
            llm_request.config.http_options.headers = self._merge_tracking_headers(
                llm_request.config.http_options.headers
            )
            _, api_version = self._base_url_and_api_version
            if api_version:
                llm_request.config.http_options.api_version = api_version

        client = self.api_client

        if stream:
            # Delegate streaming generation to a thread
            responses = await asyncio.to_thread(
                client.models.generate_content_stream,
                model=llm_request.model,
                contents=llm_request.contents,
                config=llm_request.config,
            )

            aggregator = StreamingResponseAggregator()
            
            def get_next_chunk(it):
                try:
                    return next(it)
                except StopIteration:
                    return None

            while True:
                response = await asyncio.to_thread(get_next_chunk, responses)
                if response is None:
                    break
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(_build_response_log(response))
                async with Aclosing(
                    aggregator.process_response(response)
                ) as aggregator_gen:
                    async for llm_response in aggregator_gen:
                        yield llm_response

            if (close_result := aggregator.close()) is not None:
                if cache_metadata:
                    cache_manager.populate_cache_metadata_in_response(
                        close_result, cache_metadata
                    )
                yield close_result

        else:
            # Delegate non-streaming call to a thread
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=llm_request.model,
                contents=llm_request.contents,
                config=llm_request.config,
            )
            logger.info('Response received from the model.')
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(_build_response_log(response))

            llm_response = LlmResponse.create(response)
            if cache_metadata:
                cache_manager.populate_cache_metadata_in_response(
                    llm_response, cache_metadata
                )
            yield llm_response

def get_model(model_name: str = "gemini-2.5-flash") -> VertexGemini:
    return VertexGemini(model=model_name)


