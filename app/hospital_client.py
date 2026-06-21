# app/hospital_client.py

import httpx
from typing import Optional

BASE_URL = "https://hospital-directory.onrender.com"

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=httpx.Timeout(
                connect=5.0,   # time to establish TCP connection
                read=10.0,     # time to wait for server response
                write=5.0,     # time to send request body
                pool=2.0,      # time to wait for a connection from pool
            ),
            limits=httpx.Limits(
                max_connections=20,        # total connections across all hosts
                max_keepalive_connections=10,  # idle connections kept alive
                keepalive_expiry=30.0,     # seconds before idle conn is closed
            ),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
    return _client


async def close_client():
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()


async def create_hospital(
    name: str,
    address: str,
    phone: Optional[str],
    batch_id: str,
) -> dict:
    client = get_client()
    payload = {
        "name": name,
        "address": address,
        "creation_batch_id": batch_id,
    }
    if phone:
        payload["phone"] = phone

    response = await client.post("/hospitals/", json=payload)
    response.raise_for_status()
    return response.json()


async def activate_batch(batch_id: str) -> bool:
    client = get_client()
    response = await client.patch(f"/hospitals/batch/{batch_id}/activate")
    return response.status_code == 200