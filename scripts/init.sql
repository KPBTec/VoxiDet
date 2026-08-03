-- ============================================================
--  AMD Server — Schema inicial
-- ============================================================

CREATE DATABASE IF NOT EXISTS voxidet_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE voxidet_db;

-- Clientes con sus API keys
CREATE TABLE IF NOT EXISTS clients (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    name          VARCHAR(100) NOT NULL,
    api_key       VARCHAR(64)  NOT NULL UNIQUE,   -- credencial para usar el servicio AMD
    install_token VARCHAR(64)  NOT NULL UNIQUE,   -- token solo para descargar el AGI
    active        TINYINT(1)   NOT NULL DEFAULT 1,
    daily_limit   INT          NOT NULL DEFAULT 500000,   -- 0 = sin límite
    provider      ENUM('groq','deepgram','deepgramv2','fireworks','together','openai','vosk','vosk_stream','sherpa') NOT NULL DEFAULT 'groq',
    allowed_ips   TEXT         DEFAULT NULL,              -- IPs/CIDRs permitidas, NULL = sin restricción
    notes         TEXT,
    created_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_api_key       (api_key),
    INDEX idx_install_token (install_token),
    INDEX idx_active        (active)
);

-- Log detallado por llamada
CREATE TABLE IF NOT EXISTS voxidet_logs (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    client_id   INT          NOT NULL,
    call_id     VARCHAR(100),                          -- UniqueID de Asterisk
    caller_id   VARCHAR(50),                           -- número enmascarado
    result      ENUM('HUMAN','VOICEMAIL','UNKNOWN','ERROR') NOT NULL,
    layer_used  TINYINT(1)   NOT NULL,
    mode        ENUM('batch','stream') NOT NULL DEFAULT 'batch',  -- ruta de detección usada
    provider    VARCHAR(15)  DEFAULT NULL,              -- groq / deepgram / openai / together / fireworks / NULL si layer=1
    latency_ms  SMALLINT UNSIGNED,
    audio_secs  DECIMAL(4,2),
    transcript  VARCHAR(500) DEFAULT NULL,             -- transcripción completa (log/auditoría, no participa en la decisión)
    param1      VARCHAR(100) DEFAULT NULL,             -- lead_id (batch) / lead_id (stream)
    param2      VARCHAR(100) DEFAULT NULL,             -- custom (batch) / session_id (stream)
    param3      VARCHAR(100) DEFAULT NULL,             -- custom
    param4      VARCHAR(100) DEFAULT NULL,             -- campaign_id|list_id
    beep_detected TINYINT(1) NOT NULL DEFAULT 0,        -- tono de beep detectado (experimental, ver tone_detector.py)
    created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (client_id) REFERENCES clients(id),
    INDEX idx_client_date (client_id, created_at),
    INDEX idx_result      (result),
    INDEX idx_param1      (param1),
    INDEX idx_caller_id   (caller_id),
    INDEX idx_created_at  (created_at)
);

-- Contador diario por cliente (para límites y dashboard)
CREATE TABLE IF NOT EXISTS daily_usage (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    client_id        INT  NOT NULL,
    date             DATE NOT NULL,
    total_calls      INT  DEFAULT 0,
    human_count      INT  DEFAULT 0,
    voicemail_count  INT  DEFAULT 0,
    unknown_count    INT  DEFAULT 0,
    deepgram_calls   INT  DEFAULT 0,   -- legacy, sin usar — ver layer2_calls
    layer2_calls     INT  DEFAULT 0,   -- cuántas fueron a capa 2 (cualquier proveedor)
    UNIQUE KEY uq_client_date (client_id, date),
    FOREIGN KEY (client_id) REFERENCES clients(id)
);

-- Nota: los contadores de daily_usage ya NO se escriben vía un stored
-- procedure llamado por-detección (ver app/cache/client_cache.py:
-- record_daily_usage + app/core/usage_sync.py) — con 150-200 agentes y
-- ratio alto, todas las detecciones del mismo cliente/día competían por el
-- lock de esta misma fila en InnoDB. Ahora se cuenta en Redis (sin
-- contención) y se vuelca a esta tabla cada 15s con valores absolutos.

-- Migraciones versionadas (app/db/migrations.py) — mismo patrón que
-- KPBTec_voxikam. El resto de las tablas de features (admin_users,
-- firewall_rules, provider_settings, provider_keys, client_keywords,
-- provider_stats, voxidet_keywords, fail2ban_unban_requests) las crea
-- run_pending_migrations() en el primer arranque, no viven acá — mismo
-- comportamiento que ya tenían las funciones ensure_* que reemplaza.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    VARCHAR(20) PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
