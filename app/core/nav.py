"""
Sidebar del panel admin, data-driven — reemplaza el bloque `<nav>` que antes
vivía hardcodeado en base.html (un `<a>` + SVG inline por link, repetido a
mano ~14 veces). Agregar una sección o un link nuevo es agregar una entrada
acá, nunca tocar el markup de base.html. Mismo patrón conceptual que el
`Sidebar.tsx` data-driven de VoxiKam, adaptado a Jinja2 (un loop sobre esta
lista en vez de un array consumido por React).
"""

ICONS: dict[str, str] = {
    "clients": (
        '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>'
        '<circle cx="9" cy="7" r="4"/>'
        '<path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>'
    ),
    "logs": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "keywords": (
        '<polyline points="4 7 4 4 20 4 20 7"/>'
        '<line x1="9" y1="20" x2="15" y2="20"/><line x1="12" y1="4" x2="12" y2="20"/>'
    ),
    "providers": '<path d="M17.5 19H9a7 7 0 1 1 6.71-9h.79a4.5 4.5 0 1 1 0 9z"/>',
    "provider_keys": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "stats": '<line x1="12" y1="20" x2="12" y2="10"/><line x1="18" y1="20" x2="18" y2="4"/><line x1="6" y1="20" x2="6" y2="16"/>',
    "timeseries": '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    "reports": (
        '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
        '<polyline points="14 2 14 8 20 8"/>'
        '<line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>'
    ),
    "firewall": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    "system": (
        '<rect x="2" y="3" width="20" height="14" rx="2"/>'
        '<line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>'
    ),
    "users": (
        '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/>'
        '<circle cx="9" cy="7" r="4"/>'
        '<path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>'
    ),
    "dashboard": (
        '<rect x="3" y="3" width="7" height="9" rx="1"/>'
        '<rect x="14" y="3" width="7" height="5" rx="1"/>'
        '<rect x="14" y="12" width="7" height="9" rx="1"/>'
        '<rect x="3" y="16" width="7" height="5" rx="1"/>'
    ),
    "system_logs": (
        '<path d="M4 4h16v16H4z" opacity="0"/>'
        '<polyline points="8 9 5 12 8 15"/><polyline points="16 9 19 12 16 15"/>'
        '<line x1="13" y1="6" x2="11" y2="18"/>'
    ),
    "sites": (
        '<path d="M21 10c0 7-9 12-9 12s-9-5-9-12a9 9 0 0 1 18 0z"/>'
        '<circle cx="12" cy="10" r="3"/>'
    ),
}

# Ítems pinneados sobre los grupos (sin acordeón, siempre visibles) — mismo
# patrón que los 2 ítems fijos arriba del Sidebar.tsx de VoxiKam (Dashboard/Live).
PINNED_ITEMS: list[dict] = [
    {"key": "dashboard", "href": "/dashboard", "label": "Dashboard", "icon": "dashboard"},
]

NAV_GROUPS: list[dict] = [
    {
        "label": "Operación",
        "items": [
            {"key": "clients", "href": "/clients", "label": "Clientes", "icon": "clients"},
            {"key": "sites",   "href": "/sites",   "label": "Sedes",    "icon": "sites"},
            {"key": "logs",    "href": "/logs",    "label": "Logs en vivo", "icon": "logs"},
        ],
    },
    {
        "label": "Detección",
        "items": [
            {"key": "keywords",      "href": "/keywords",        "label": "Keywords",    "icon": "keywords"},
            {"key": "providers",     "href": "/providers",       "label": "Proveedores", "icon": "providers"},
            {"key": "provider_keys", "href": "/providers/keys",  "label": "API Keys",    "icon": "provider_keys"},
            {"key": "stats",         "href": "/providers/stats", "label": "Consumo",     "icon": "stats"},
            {"key": "timeseries",    "href": "/timeseries",      "label": "Detecciones", "icon": "timeseries"},
            {"key": "reports",       "href": "/reports",         "label": "Reportes",    "icon": "reports"},
        ],
    },
    {
        "label": "Sistema",
        "items": [
            {"key": "firewall",     "href": "/firewall",    "label": "Firewall",     "icon": "firewall"},
            {"key": "system",       "href": "/system",      "label": "Sistema",      "icon": "system"},
            {"key": "system_logs",  "href": "/system/logs", "label": "Logs backend", "icon": "system_logs"},
            {"key": "users",        "href": "/users",       "label": "Usuarios",     "icon": "users"},
        ],
    },
]
