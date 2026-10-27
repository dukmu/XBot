import httpx

from XBotv2.client import XBotClient


async def test_catalog_methods_request_one_page_with_cursor():
    requests = []

    def respond(request):
        requests.append(request)
        data = {"next_cursor": "next", "previous_cursor": "previous", "event_cursor": 7}
        data["items" if request.url.path == "/workspaces" else "sessions"] = []
        if request.url.path.startswith("/workspaces/"):
            data["archived_session_ids"] = ["archived"]
        return httpx.Response(200, json=data)

    async with XBotClient("http://test", transport=httpx.MockTransport(respond)) as client:
        pages = [
            await client.list_sessions(limit=2, cursor="c", workspace_root="/work"),
            await client.list_workspaces(limit=2, cursor="c"),
            await client.list_workspace_sessions("workspace", limit=2, cursor="c"),
            await client.list_archived_sessions(limit=2, cursor="c"),
        ]
    assert [request.url.path for request in requests] == [
        "/sessions", "/workspaces", "/workspaces/workspace/sessions", "/workspaces/archived-sessions"
    ]
    assert all(request.url.params["limit"] == "2" and request.url.params["cursor"] == "c" for request in requests)
    assert requests[0].url.params["workspace_root"] == "/work"
    assert all(page.next_cursor == "next" and page.previous_cursor == "previous" for page in pages)
    assert pages[2].archived_session_ids == pages[3].archived_session_ids == ["archived"]
