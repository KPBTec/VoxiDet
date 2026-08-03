import ipaddress


def ip_allowed(client_ip: str, allowed_ips: str | None) -> bool:
    """True si allowed_ips está vacío o client_ip coincide con alguna IP/CIDR."""
    if not allowed_ips or not allowed_ips.strip():
        return True
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in allowed_ips.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            else:
                if addr == ipaddress.ip_address(entry):
                    return True
        except ValueError:
            continue
    return False
