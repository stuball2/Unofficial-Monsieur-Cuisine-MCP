import httpx
import pytest
import respx


async def test_unmocked_http_request_is_blocked():
    with pytest.raises(respx.models.AllMockedAssertionError):
        async with httpx.AsyncClient() as client:
            await client.get("https://mc-api.tecpal.com/api/v1/recipes/filters")


def test_raw_socket_is_blocked():
    import socket
    s = socket.socket()
    with pytest.raises(RuntimeError, match="network access blocked"):
        s.connect(("93.184.216.34", 443))
    s.close()
