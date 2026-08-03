# nftables/

Configuración del firewall. Dos partes: config base estática (este directorio) y el fragmento
dinámico generado por `scripts/gen_nftables.py`. Mismo patrón que VoxiKam: **una sola tabla**,
el `include` va dentro del `chain input` y trae un fragmento de reglas, no una tabla aparte.

## Archivos

```
nftables/
  nftables.conf          ← config base, copiada a /etc/nftables.conf por deploy.sh
  nftables.d/
    voxidet.nft           ← placeholder inicial — gen_nftables.py lo regenera cada 5 min
```

## nftables.conf — base

`flush ruleset` + una tabla (`table inet filter`) con SSH y el puerto web/API siempre abiertos
(decisión explícita: SSH sin restricción de origen), más el `include` del fragmento dinámico
dentro del mismo `chain input`.

Placeholders (sustituidos por `apply_conf()` en `deploy.sh`, mismo mecanismo `sed` que VoxiKam):
- `__SSH_PORT__` — auto-detectado (`ss`/`sshd_config`, misma lógica bash que VoxiKam)
- `__WEB_PORT__` — de `API_PORT` en `credentials.conf` si se personalizó, si no `8000`

**Importante:** como SSH y el puerto web están con `accept` incondicional *antes* del `include`,
y `accept` es un veredicto terminal en nftables, las reglas de SSH/API del panel (`firewall_rules`
con `service=ssh` o `service=api`) generadas por `gen_nftables.py` **nunca se alcanzan** mientras
esos accepts estáticos sigan ahí — se generan de todas formas por si en el futuro se quita el
accept incondicional. El panel de Firewall sigue sirviendo para `DENY` (esas sí se evalúan antes)
y para restringir API si se decide cerrar ese accept estático más adelante.

## nftables.d/voxidet.nft — dinámico (gen_nftables.py)

**Es un fragmento, no una tabla completa** — mismo patrón que `carriers.nft`/`customers.nft` de
VoxiKam. Usa `define nombre = { ip1, ip2 }` (sustitución de texto, válida en cualquier lugar) en
vez de `set` (que en nftables solo puede declararse a nivel de tabla, no dentro de un `chain{}` —
por eso VoxiKam tampoco usa `set` en sus fragmentos incluidos).

`gen_nftables.py` lee `firewall_rules` de la DB y genera:
- `define blocked_ips` / `ssh_allowed` / `api_allowed` según las reglas activas
- `ip saddr $blocked_ips drop` — esta sí se evalúa siempre (antes del accept estático no aplica
  aquí porque DENY no tiene un accept que lo bloquee primero)

Si no hay reglas en DB, el archivo queda con un comentario ("Sin reglas configuradas en el panel")
— igual al placeholder inicial.

`apply()` ya no borra una tabla nombrada aparte (no existe): escribe el fragmento y recarga
`/etc/nftables.conf` completo con `nft -f` — mismo mecanismo que `apply_nftables()` de VoxiKam.

## Permisos

```
/etc/nftables.conf        → root:root 644
/etc/nftables.d/          → root:voxidet 775  (voxidet escribe el .nft)
/etc/nftables.d/voxidet.nft → voxidet:voxidet (creado por gen_nftables.py)
```

`voxidet` puede ejecutar `sudo /usr/sbin/nft` sin password — ver `sudoers/voxidet`.

## Aplicar cambios manualmente

```bash
# Verificar sintaxis
nft -c -f /etc/nftables.conf

# Aplicar
sudo nft -f /etc/nftables.conf

# Ver reglas activas
nft list ruleset

# Forzar regeneración desde DB ahora
sudo -u voxidet python3 /opt/voxidet/scripts/gen_nftables.py
```

## Firewall panel web → nftables

El panel Admin → Firewall escribe en la tabla `firewall_rules`. `gen_nftables.py` lee esa tabla
(cron cada 5 min como safety net, o al instante cuando se guarda una regla desde el panel).
