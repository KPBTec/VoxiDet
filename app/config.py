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
