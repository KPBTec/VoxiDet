import os
import pathlib
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_release_version() -> str:
    """Lee PLATFORM_VERSION de release.conf — fuente única de la versión,
    usada por deploy.sh (resumen final) y el panel admin (sidebar)."""
    path = pathlib.Path(__file__).parent.parent / "release.conf"
    try:
        for line in path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line.startswith("PLATFORM_VERSION="):
                return line.split("=", 1)[1].strip().strip('"')
    except FileNotFoundError:
        pass
    return "0.0.0"


VERSION = _read_release_version()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="credentials.conf",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # MySQL
    MYSQL_HOST: str = "mysql"
    MYSQL_PORT: int = 3306
    MYSQL_DB: str   = "voxidet_db"
    MYSQL_USER: str = ""
    MYSQL_PASSWORD: str = ""

    # Redis
    REDIS_HOST: str     = "redis"
    REDIS_PORT: int     = 6379
    REDIS_PASSWORD: str = ""
    REDIS_CACHE_TTL: int = 300

    # Transcripción — keys numeradas (GROQ_API_KEY_1, GROQ_API_KEY_2, ...)
    # tienen prioridad sobre las variables coma-separadas (backward compat)
    GROQ_API_KEY: str       = ""
    GROQ_API_KEYS: str      = ""
    DEEPGRAM_API_KEY: str   = ""
    DEEPGRAM_API_KEYS: str  = ""
    FIREWORKS_API_KEY: str  = ""
    FIREWORKS_API_KEYS: str = ""
    TOGETHER_API_KEY: str   = ""
    TOGETHER_API_KEYS: str  = ""
    OPENAI_API_KEY: str     = ""
    OPENAI_API_KEYS: str    = ""

    # Directorio base de modelos locales — montado desde /opt/voxidet/models-local en el host
    # No necesita cambiarse; los modelos se descubren automáticamente por sus archivos .version
    MODELS_BASE: str = "/srv/models-local"

    # App
    AUDIO_MAX_SECONDS: float = 3.0
    LOG_LEVEL: str  = "info"
    PUBLIC_URL: str = "http://localhost:8000"

    # Modo AudioSocket (v1.27.0) — puerto TCP crudo donde Asterisk conecta
    # directo vía la aplicación de dialplan AudioSocket() (no HTTP, no pasa
    # por Cloudflare). Sin TLS por ahora (decisión explícita del usuario, ver
    # CHANGELOG v1.27.0) — el audio de la llamada viaja sin cifrar entre el
    # Asterisk y este puerto, deuda técnica pendiente antes de usarlo con
    # tráfico real sensible.
    AUDIOSOCKET_PORT: int = 9099

    # Límite de conexiones concurrentes ACEPTADAS por worker en el puerto
    # AudioSocket (son 11 workers -> techo real de la plataforma ~11x esto).
    # A diferencia del puerto HTTP (Cloudflare + verify_client con allow-list
    # de IP + límite diario), este puerto TCP crudo no tiene ninguna capa
    # intermedia. `sock.listen(256)` en create_listening_socket() NO cumple
    # este rol — eso solo acota la cola de accept() del kernel, no las
    # conexiones ya aceptadas y en curso.
    #
    # La defensa PRINCIPAL contra un flood ya es el timeout de _read_packet()
    # (cada conexión se corta sola a los MAX_SECS=8.0 como máximo, sin
    # excepción) — este número es un techo de emergencia para un ataque
    # sostenido, no la barrera contra tráfico real de un marcador. Sizing:
    # una conexión AudioSocket vive como máximo 8s (normalmente <1s, el 70%
    # se resuelve casi al instante con la capa de energía); incluso un
    # marcador agresivo con decenas de originaciones/seg en simultáneo entre
    # todos los clientes queda muy por debajo de este valor. Configurable sin
    # tocar código vía AUDIOSOCKET_MAX_CONNECTIONS en credentials.conf si el
    # tráfico real de algún cliente lo justifica — el log avisa explícito
    # ("límite de N conexiones concurrentes alcanzado") si algún día se
    # llegara a pisar el techo.
    AUDIOSOCKET_MAX_CONNECTIONS: int = 1000

    # ── Modo ARI (experimental, v1.28.0) ─────────────────────────────────────
    # EXPERIMENTAL — solo para la extensión de prueba (8379 por convención,
    # nunca la extensión real de producción de un cliente). Nace de un
    # problema real: en Asterisk 18.26.4 la app de dialplan AudioSocket()
    # SIEMPRE "sale con error" (revisado contra el código fuente real de esa
    # versión) — no importa qué mande el servidor al cerrar, el dialplan
    # nunca continúa a la siguiente prioridad, así que HUMAN nunca llega a
    # transferirse al agente. ARI (la API REST/WebSocket de Asterisk) evita
    # esto: en vez de que el dialplan dependa de que AudioSocket() termine
    # bien, un servicio aparte (app/ari/controller.py, proceso propio, NO
    # dentro de los workers de gunicorn — ver docstring de ese archivo) toma
    # control del canal real vía Stasis(), crea un canal Snoop (copia del
    # audio del que llama, sin tocar el canal real) + un canal AudioSocket
    # independiente que se conecta al MISMO servidor TCP que ya corre
    # (audiosocket_server.py, sin cambios), y cuando termina el análisis le
    # dice al canal real que CONTINÚE el dialplan normal — en vez de esperar
    # que AudioSocket() "no falle", nunca depende de eso.
    #
    # Nunca validado contra Asterisk real todavía (a la espera de la prueba
    # en la extensión 8379) — ver CLAUDE.md § "Modo ARI" para el estado
    # exacto de qué se probó y qué no.
    ARI_URL:      str = ""              # ej. http://10.100.10.x:8088 — el HTTP interno de Asterisk (ari.conf/http.conf)
    ARI_USER:     str = "voxidet"       # debe existir en ari.conf, tipo user, NO read_only (necesita crear canales/bridges)
    ARI_PASSWORD: str = ""
    ARI_APP:      str = "voxidet-ari"   # nombre de la app Stasis — debe matchear Stasis(voxidet-ari) en el dialplan

    # Cómo le llega el audio al controlador ARI — dos caminos posibles según
    # qué tenga instalado el Asterisk de destino (ver app/ari/controller.py
    # y app/ari/rtp_media.py para el detalle de cada uno):
    #   "audiosocket" — reusa chan_audiosocket + el servidor TCP que ya
    #                   corre siempre (audiosocket_server.py, sin cambios).
    #                   Requiere que ese módulo esté cargado en Asterisk.
    #   "rtp"         — usa el mecanismo nativo de ARI (externalMedia, RTP
    #                   crudo por UDP) — no necesita ningún módulo extra en
    #                   Asterisk, pero es código nuevo (RTP, no TCP) sin
    #                   probar todavía contra un Asterisk real.
    # Encontrado en producción: no todos los nodos tienen chan_audiosocket
    # instalado (confirmado en un nodo real con Asterisk 16.30.0-vici) — de
    # ahí que exista la opción "rtp".
    ARI_MEDIA_MODE: str = "rtp"

    # Host:puerto que ASTERISK debe poder alcanzar por la red interna para
    # conectarse de vuelta al servidor AudioSocket (audiosocket_server.py) —
    # mismo servidor y puerto que ya usa el modo Audiosocket clásico
    # (AUDIOSOCKET_PORT arriba). Solo aplica con ARI_MEDIA_MODE="audiosocket"
    # — con "rtp" no hace falta, cada llamada usa su propio puerto UDP
    # elegido dinámicamente (ver rtp_media.py::create_rtp_session).
    AUDIOSOCKET_HOST: str = ""

    # Alertas proactivas (app/core/alerting.py) — opt-in, sin esto configurado
    # notify() es un no-op (mismo patrón que INSTALL_SHERPA_LARGE: no aparece
    # ni se usa por accidente si nadie lo configuró a propósito). Acepta
    # cualquier webhook que reciba POST {"text": "..."} — Slack incoming
    # webhook, n8n, o un endpoint propio.
    ALERT_WEBHOOK_URL: str = ""

    # Timeouts HTTP de transcripción (capa 2, ASR en la nube) — un valor por
    # proveedor, compartido entre modo batch (amd_engine.py) y modo stream
    # (stream.py). Antes cada archivo tenía su propio literal hardcodeado y
    # quedaron desincronizados (p.ej. together/fireworks: 8.0 en batch vs 5.0
    # en stream para el mismo proveedor) — acá se unifican al valor más chico
    # de los dos existentes (salvo que ya coincidieran), porque en modo stream
    # el presupuesto total por llamada es de solo ~8s (ver MAX_SECS en
    # stream.py) y un timeout de proveedor demasiado largo ahí no deja margen
    # para intentar el fallback a otro proveedor dentro de esa ventana.
    ASR_TIMEOUT_DEEPGRAM:  float = 5.0
    ASR_TIMEOUT_GROQ:      float = 5.0
    ASR_TIMEOUT_OPENAI:    float = 8.0
    ASR_TIMEOUT_TOGETHER:  float = 5.0
    ASR_TIMEOUT_FIREWORKS: float = 5.0

    # Detección de tono de beep de buzón (experimental, solo logging por ahora
    # — ver app/core/tone_detector.py). La frecuencia real depende del
    # operador/central telefónica y NO está calibrada con datos de producción
    # todavía; ajustar aquí sin tocar código una vez que se observen valores reales.
    AMD_BEEP_FREQ_HZ: float = 1000.0

    # Admin API
    ADMIN_KEY: str = ""

    # Admin CMS
    ADMIN_PREFIX: str    = "/admin"
    ADMIN_USER: str      = "admin"
    ADMIN_PASSWORD: str  = ""
    SECRET_KEY: str      = ""

    # Cifrado en reposo de API keys guardadas desde el panel (v1.16.0) — Fernet
    # key generada por deploy.sh (openssl), nunca a mano. Ver core/secrets_crypto.py.
    KEYS_ENCRYPTION_SECRET: str = ""

    # ── Helpers para leer keys numeradas ──────────────────────────────────────

    def _numbered(self, prefix: str) -> list[str]:
        """Lee PROVIDER_KEY_1, _2, _3... hasta que no haya más."""
        keys = []
        i = 1
        while True:
            val = os.environ.get(f"{prefix}_{i}", "").strip()
            if not val:
                break
            keys.append(val)
            i += 1
        return keys

    def _split(self, multi: str, single: str) -> list[str]:
        if multi:
            return [k.strip() for k in multi.split(",") if k.strip()]
        if single:
            return [single]
        return []

    def get_groq_keys(self) -> list[str]:
        return self._numbered("GROQ_API_KEY") or self._split(self.GROQ_API_KEYS, self.GROQ_API_KEY)

    def get_deepgram_keys(self) -> list[str]:
        return self._numbered("DEEPGRAM_API_KEY") or self._split(self.DEEPGRAM_API_KEYS, self.DEEPGRAM_API_KEY)

    def get_fireworks_keys(self) -> list[str]:
        return self._numbered("FIREWORKS_API_KEY") or self._split(self.FIREWORKS_API_KEYS, self.FIREWORKS_API_KEY)

    def get_together_keys(self) -> list[str]:
        return self._numbered("TOGETHER_API_KEY") or self._split(self.TOGETHER_API_KEYS, self.TOGETHER_API_KEY)

    def get_openai_keys(self) -> list[str]:
        return self._numbered("OPENAI_API_KEY") or self._split(self.OPENAI_API_KEYS, self.OPENAI_API_KEY)


settings = Settings()
