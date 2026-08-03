# fail2ban/

Protección contra fuerza bruta — dos jails independientes. `deploy.sh` instala fail2ban, copia
estos archivos (con sustitución de puerto SSH) y activa el servicio.

## Archivos

```
fail2ban/
  filter.d/
    voxidet-security.conf  ← regex para rechazos de la app (API key, login, IP, User-Agent)
  jail.d/
    voxidet.conf            ← jails sshd + voxidet-security (plantilla, __SSH_PORT__)
```

## Jails

### `sshd` (filtro estándar de fail2ban, ya viene con el paquete)
5 intentos fallidos en 10 min → ban 1h. Puerto = el detectado por `deploy.sh`
(mismo mecanismo que `nftables.conf`: `ss`/`sshd_config`, no hardcodeado a 22).

### `voxidet-security` (filtro propio, `filter.d/voxidet-security.conf`)
10 rechazos en 1 min → ban 1h. Lee `/opt/voxidet/logs/security.log` (bind mount del volumen
Docker — fail2ban corre en el host, no dentro del contenedor).

**Por qué NO cuenta rate-limit:** el filtro solo matchea `reason=blocked_ua`, `invalid_api_key`,
`ip_not_allowed`, `invalid_admin_key` y `login_failed` — señales que un cliente legítimo (API key
válida) **nunca** dispara. A propósito excluye `reason=rate_limit`: un dialer Vicidial de alto
volumen puede exceder el límite de requests/min en tráfico 100% normal (CLAUDE.md documenta
soporte para 150+ agentes simultáneos) — banearlo por eso sería un auto-DoS. El rate limit de
`app/core/security.py` sigue devolviendo 429 igual, solo que eso no alimenta un ban de red.

## banaction — nftables, no iptables

El proyecto ya usa nftables (`nftables/nftables.conf`) para el firewall. `banaction =
nftables-allports` hace que fail2ban cree su **propio** set nftables independiente para IPs
baneadas — no toca la tabla `filter` ni interfiere con `gen_nftables.py`. Si se dejara el
`banaction` por defecto (iptables), correría el riesgo del mismo conflicto iptables-nft/Docker que
ya se resolvió en `deploy.sh` para el firewall principal (ver `CHANGELOG.md`).

## Origen de los logs que lee `voxidet-security`

`app/core/security.py` y `app/api/deps.py` (más `app/api/admin/auth.py` para el login del panel)
loguean `SECURITY_REJECT ip=<ip> reason=<motivo> path=<path>` vía el logger `voxidet.security`
(y `voxidet.deps`), ruteado a `/srv/logs/security.log` dentro del contenedor —
`app/log_config.json` lo define. `docker-compose.yml` monta ese directorio como bind mount en
`/opt/voxidet/logs` para que fail2ban lo lea desde el host.

## Comandos útiles

```bash
# Estado general
fail2ban-client status

# Estado de un jail específico
fail2ban-client status sshd
fail2ban-client status voxidet-security

# Desbanear una IP
fail2ban-client set sshd unbanip <IP>
fail2ban-client set voxidet-security unbanip <IP>

# Probar el filtro contra el log real
fail2ban-regex /opt/voxidet/logs/security.log /etc/fail2ban/filter.d/voxidet-security.conf
```

## Pendiente (no implementado en esta sesión)

Ver IPs baneadas / desbanear desde el panel admin (como Firewall) — requiere un endpoint que
llame `fail2ban-client` (via sudo, como ya hace `gen_nftables.py` con `nft`) y una sección nueva
en la UI. Por ahora es solo CLI en el servidor.
