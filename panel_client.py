"""Talks to a deployed VLESS panel itself (not Railway) — used for health
checks and remote password rotation."""

import httpx


async def check_health(domain: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"https://{domain}/health")
        return resp.status_code == 200
    except Exception:
        return False


async def rotate_password(domain: str, current_password: str, new_password: str) -> bool:
    """Logs into the panel with the current password and changes it. Returns
    True on success. The panel's own auth (post-first-login) lives in its
    DB, not the Railway env var, so this is how we keep our record in sync."""
    async with httpx.AsyncClient(timeout=15, base_url=f"https://{domain}") as client:
        login = await client.post("/api/login", json={"password": current_password})
        if login.status_code != 200:
            return False
        cookies = login.cookies
        change = await client.post(
            "/api/change-password",
            json={"current_password": current_password, "new_password": new_password},
            cookies=cookies,
        )
        return change.status_code == 200
