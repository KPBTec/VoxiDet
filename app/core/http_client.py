"""
http_client.py — cliente httpx compartido para las APIs de transcripción
(Deepgram, Groq, OpenAI, Together, Fireworks).

Antes cada llamada creaba su propio `httpx.AsyncClient` (`async with
httpx.AsyncClient(...) as client`) — bajo carga alta eso significa un TCP
connect + TLS handshake nuevo por CADA detección AMD que llega a un
transcriptor externo, en vez de reusar conexiones keep-alive. Un solo
cliente por proceso (mismo patrón que el pool de Redis en
app/cache/client_cache.py) resuelve esto.
"""
import logging

import httpx

log = logging.getLogger("voxidet.http_client")

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50, keepalive_expiry=30),
        )
        log.info("Cliente HTTP compartido inicializado (pool de conexiones a APIs de transcripción)")
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
