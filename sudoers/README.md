# sudoers/

Permisos sudo del usuario de servicio `voxidet` (nologin) — mismo patrón que VoxiKam: archivo
estático versionado en el repo, `deploy.sh` lo copia a `/etc/sudoers.d/voxidet` en vez de
generarlo inline con un heredoc.

## Archivos

```
sudoers/
  voxidet    ← copiado a /etc/sudoers.d/voxidet, permisos 440
```

## Por qué solo `nft`

`voxidet` corre como usuario sin login (`--shell /usr/sbin/nologin`) y sin privilegios — la única
razón por la que necesita `sudo` es que `scripts/gen_nftables.py` debe poder aplicar reglas de
firewall (`nft -f ...`) cuando se guardan cambios desde el panel admin. Ningún otro comando está
permitido.

## Validar después de editar

```bash
visudo -c -f /etc/sudoers.d/voxidet
```
