import pytest
import httpx

from app.routers.resources import _stream_download_response


class _AsyncBytes(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"image-bytes"


@pytest.mark.asyncio
async def test_stream_download_response_forces_attachment():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_AsyncBytes(),
            headers={"Content-Type": "image/png", "Content-Length": "11"},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await _stream_download_response(
            "https://storage.example.com/files/test.png",
            "测试图片.png",
            client=client,
        )
        chunks = [chunk async for chunk in response.body_iterator]

    assert b"".join(chunks) == b"image-bytes"
    assert response.media_type == "image/png"
    assert response.headers["content-length"] == "11"
    content_disposition = response.headers["content-disposition"]
    assert content_disposition.startswith("attachment;")
    assert "filename*=" in content_disposition
