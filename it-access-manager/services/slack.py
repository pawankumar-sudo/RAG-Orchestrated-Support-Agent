import os
import httpx

SLACK_API_TOKEN = os.getenv("SLACK_API_TOKEN", "")
SLACK_BASE_URL = "https://slack.com/api"

HEADERS = {
    "Authorization": f"Bearer {SLACK_API_TOKEN}",
    "Content-Type": "application/json",
}


async def get_user_by_email(email: str) -> dict | None:
    """Look up a Slack user by email. Returns user dict or None."""
    url = f"{SLACK_BASE_URL}/users.lookupByEmail"
    params = {"email": email}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=HEADERS, params=params)
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok") and data.get("user"):
            user = data["user"]
            profile = user.get("profile", {})
            return {
                "id": user.get("id"),
                "email": profile.get("email", email),
                "name": profile.get("real_name", user.get("name", "")),
                "status": "deactivated" if user.get("deleted", False) else "active",
                "is_admin": user.get("is_admin", False),
                "is_owner": user.get("is_owner", False),
                "role": _determine_role(user),
            }
    return None


def _determine_role(user: dict) -> str:
    """Derive a human-readable role string from Slack user flags."""
    if user.get("is_owner"):
        return "Owner"
    if user.get("is_admin"):
        return "Admin"
    if user.get("is_ultra_restricted"):
        return "Single-Channel Guest"
    if user.get("is_restricted"):
        return "Multi-Channel Guest"
    return "Member"


async def get_all_users() -> list[dict]:
    """Fetch every Slack user in the workspace (handles cursor pagination)."""
    url = f"{SLACK_BASE_URL}/users.list"
    all_users = []
    cursor = None

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            resp = await client.get(url, headers=HEADERS, params=params)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                break
            for u in data.get("members", []):
                if u.get("is_bot") or u.get("id") == "USLACKBOT":
                    continue
                profile = u.get("profile", {})
                all_users.append({
                    "id": u.get("id"),
                    "email": profile.get("email", ""),
                    "name": profile.get("real_name", u.get("name", "")),
                    "status": "deactivated" if u.get("deleted") else "active",
                    "role": _determine_role(u),
                })
            next_cursor = data.get("response_metadata", {}).get("next_cursor", "")
            if not next_cursor:
                break
            cursor = next_cursor

    return all_users


async def deactivate_user(user_id: str) -> bool:
    """
    Deactivate a Slack user. Tries admin.users.remove first,
    then falls back to the legacy users.admin.setInactive endpoint.
    """
    url = f"{SLACK_BASE_URL}/admin.users.remove"
    team_id = os.getenv("SLACK_TEAM_ID", "")
    payload = {"team_id": team_id, "user_id": user_id}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, headers=HEADERS, json=payload)
        data = resp.json()
        if data.get("ok"):
            return True

        legacy_url = f"{SLACK_BASE_URL}/users.admin.setInactive"
        legacy_resp = await client.post(
            legacy_url, headers=HEADERS, json={"user": user_id}
        )
        legacy_data = legacy_resp.json()
        return legacy_data.get("ok", False)
