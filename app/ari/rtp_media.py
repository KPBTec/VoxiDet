"""
app/ari/rtp_media.py — Receptor/emisor RTP para el modo ARI cuando
`chan_audiosocket` NO está disponible en el Asterisk (caso real encontrado
en producción: nodo con Asterisk 16.30.0-vici, sin ese módulo instalado ni
compilable ahí en el momento).

Usa el mecanismo NATIVO de ARI para esto — `externalMedia`
(`POST /channels/externalMedia`) — que le pide a Asterisk que mande el
audio de una llamada como RTP crudo (UDP) a un puerto nuestro, sin depender
de ningún canal/módulo de terceros: solo necesita ARI mismo, que ya es
parte del núcleo de Asterisk.

Un socket UDP DEDICADO por llamada (no uno compartido) — bindeado a puerto
0 (el SO elige uno libre) — así no hace falta correlacionar tráfico
entrante de varias llamadas concurrentes por IP:puerto de origen; cada
llamada tiene su propio socket desde el principio hasta el final.

Formato acordado con Asterisk vía `format=slin` al pedir el externalMedia
(ver app/ari/controller.py): PCM 16-bit signed, 8kHz, mono, SIN compresión
— el MISMO formato que ya usa app/core/audiosocket_server.py, así que la
detección (make_detector/detector.feed) es idéntica; lo único que cambia es
cómo llegan los bytes de audio a este proceso (RTP+UDP en vez del framing
de AudioSocket sobre TCP).

NUNCA VALIDADO CONTRA ASTERISK REAL TODAVÍA. Puntos concretos sin
confirmar (ver también la cabecera de controller.py):
- Que Asterisk realmente empiece a mandar RTP a este puerto apenas se crea
  el channel externalMedia (podría hacer falta esperar a que el channel
  esté "Up" primero, como con el channel de AudioSocket).
- El campo Payload Type (PT) que Asterisk espera ver en los frames que le
  mandamos de vuelta (acá se manda 0, PCMU, como placeholder — con
  format=slin puede que Asterisk lo ignore y decodifique según el formato
  ya negociado en el channel, o puede que rechace el paquete; falta ver).
- Si additionally hace falta manejar reordenamiento por número de secuencia
  — no implementado en esta primera versión (ver comentario en
  `datagram_received`), la ventana de análisis es corta (segundos) así que
  se asume tolerable, a confirmar con tráfico real.
"""
import asyncio
import logging
import struct

log = logging.getLogger("voxidet.ari.rtp")

_RTP_HEADER_LEN  = 12
_RTP_VERSION_BYTE = 0x80   # V=2, P=0, X=0, CC=0
_RTP_PT_PCMU      = 0      # placeholder — ver nota en la cabecera del módulo
_RTP_SSRC         = 0x564F_5849  # "VOXI" en hex, arbitrario pero fijo y reconocible en una captura

SILENCE_FRAME = b"\x00" * 320  # 20ms @ 8kHz 16-bit mono — mismo tamaño que audiosocket_server.py


class _RtpProtocol(asyncio.DatagramProtocol):
    def __init__(self, feed_callback):
        self._feed = feed_callback
        self.transport: asyncio.DatagramTransport | None = None
        self.remote_addr: tuple[str, int] | None = None
        self._seq = 0
        self._ts  = 0

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.remote_addr is None:
            self.remote_addr = addr
            log.debug("RTP: primer paquete recibido desde %s", addr)
        if len(data) <= _RTP_HEADER_LEN:
            return
        self._feed(data[_RTP_HEADER_LEN:])

    def error_received(self, exc: Exception) -> None:
        log.warning("RTP: error de socket: %s", exc)

    def send_frame(self, payload: bytes) -> None:
        """Manda un frame de audio de confort de vuelta — requiere haber
        visto al menos un paquete entrante (remote_addr) para saber a dónde
        contestar; a diferencia de AudioSocket (TCP, con conexión
        establecida), RTP/UDP no tiene un destino implícito hasta que llega
        el primer paquete real."""
        if not self.transport or not self.remote_addr:
            return
        self._seq = (self._seq + 1) & 0xFFFF
        self._ts  = (self._ts + 160) & 0xFFFFFFFF  # 160 samples = 20ms @ 8kHz
        header = struct.pack(">BBHII", _RTP_VERSION_BYTE, _RTP_PT_PCMU, self._seq, self._ts, _RTP_SSRC)
        try:
            self.transport.sendto(header + payload, self.remote_addr)
        except Exception as e:
            log.warning("RTP: error mandando frame de confort: %s", e)

    def close(self) -> None:
        if self.transport:
            self.transport.close()


class RtpSession:
    """Un socket UDP dedicado a una sola llamada — bindeado en un puerto
    libre elegido por el SO. Usar `port` para pedirle a Asterisk el
    externalMedia hacia acá; `send_silence()` para el audio de confort;
    `close()` al terminar."""

    def __init__(self, protocol: _RtpProtocol, transport: asyncio.DatagramTransport, port: int):
        self._protocol = protocol
        self._transport = transport
        self.port = port

    def send_silence(self) -> None:
        self._protocol.send_frame(SILENCE_FRAME)

    def close(self) -> None:
        self._protocol.close()


async def create_rtp_session(feed_callback) -> RtpSession:
    """feed_callback(chunk: bytes) se llama de forma síncrona por cada
    frame de audio recibido (ya sin la cabecera RTP de 12 bytes)."""
    loop = asyncio.get_event_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: _RtpProtocol(feed_callback),
        local_addr=("0.0.0.0", 0),
    )
    port = transport.get_extra_info("sockname")[1]
    log.debug("RTP: sesión nueva bindeada en puerto %d", port)
    return RtpSession(protocol, transport, port)
