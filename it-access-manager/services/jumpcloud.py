import os
import httpx

JUMPCLOUD_API_KEY = os.getenv("JUMPCLOUD_API_KEY", "")
JUMPCLOUD_BASE_URL = "https://console.jumpcloud.com/api"

HEADERS = {
    "x-api-key": JUMPCLOUD_API_KEY,
    "Content-Type": "application/json",
    "Accept": "application/json",
}


async def get_user_by_email(email: str) -> dict | None:
    """Search JumpCloud for a user by email. Returns user dict or None."""
    url = f"{JUMPCLOUD_BASE_URL}/systemusers"
    params = {"filter": f"email:$eq:{email}"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=HEADERS, params=params)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if results:
            user = results[0]
            return {
                "id": user.get("_id"),
                "email": user.get("email"),
                "name": f"{user.get('firstname', '')} {user.get('lastname', '')}".strip(),
                "status": "suspended" if user.get("suspended", False) else "active",
                "source": "jumpcloud",
            }
    return None


async def get_all_users(limit: int = 100, skip: int = 0) -> list[dict]:
    """Fetch all JumpCloud users (paginated)."""
    url = f"{JUMPCLOUD_BASE_URL}/systemusers"
    params = {"limit": limit, "skip": skip}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=HEADERS, params=params)
        resp.raise_for_status()
        data = resp.json()
        users = []
        for u in data.get("results", []):
            users.append({
                "id": u.get("_id"),
                "email": u.get("email"),
                "name": f"{u.get('firstname', '')} {u.get('lastname', '')}".strip(),
                "status": "suspended" if u.get("suspended", False) else "active",
                "source": "jumpcloud",
            })
        return users


async def suspend_user(user_id: str) -> bool:
    """Suspend (disable) a JumpCloud user by their ID. Returns True on success."""
    url = f"{JUMPCLOUD_BASE_URL}/systemusers/{user_id}"
    payload = {"suspended": True}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(url, headers=HEADERS, json=payload)
        resp.raise_for_status()
        return True
