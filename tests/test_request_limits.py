import asyncio

from app.services.request_limit_service import RequestLimitMiddleware, RequestLimitSettings


def run_request(events, *, headers=None, maximum=8, method="POST", timeout=1):
    calls = []
    responses = []

    async def app(scope, receive, send):
        calls.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return events.pop(0)

    async def send(event):
        responses.append(event)

    middleware = RequestLimitMiddleware(
        app, RequestLimitSettings(maximum, timeout_seconds=timeout)
    )
    scope = {"type": "http", "method": method, "headers": headers or []}
    asyncio.run(middleware(scope, receive, send))
    return responses, calls, middleware


def test_oversized_content_length_is_rejected_without_consuming_upload():
    events = [{"type": "http.request", "body": b"unread"}]
    responses, calls, middleware = run_request(events, headers=[(b"content-length", b"9")])
    assert responses[0]["status"] == 413
    assert not calls
    assert len(events) == 1
    assert middleware._active_requests == 0


def test_streamed_body_cannot_bypass_limit_without_content_length():
    responses, calls, _ = run_request([
        {"type": "http.request", "body": b"12345", "more_body": True},
        {"type": "http.request", "body": b"6789", "more_body": False},
    ])
    assert responses[0]["status"] == 413
    assert not calls


def test_bounded_chunked_upload_reaches_app_unchanged():
    responses, calls, _ = run_request([
        {"type": "http.request", "body": b"1234", "more_body": True},
        {"type": "http.request", "body": b"5678", "more_body": False},
    ])
    assert responses[0]["status"] == 200
    assert calls == [{"type": "http.request", "body": b"12345678", "more_body": False}]


def test_malformed_or_mismatched_content_length_is_rejected():
    for value in (b"-1", b"bad", b"1,1", b"4"):
        responses, calls, _ = run_request(
            [{"type": "http.request", "body": b"abc", "more_body": False}],
            headers=[(b"content-length", value)],
        )
        assert responses[0]["status"] == 400
        assert not calls


def test_upload_timeout_releases_slot_and_does_not_enter_app():
    async def scenario():
        responses = []

        async def app(*_args):
            raise AssertionError("Incomplete body must not reach the application")

        async def receive():
            await asyncio.Event().wait()

        async def send(event):
            responses.append(event)

        middleware = RequestLimitMiddleware(app, RequestLimitSettings(8, timeout_seconds=0.01))
        await middleware({"type": "http", "method": "POST", "headers": []}, receive, send)
        assert responses[0]["status"] == 408
        assert middleware._active_requests == 0

    asyncio.run(scenario())


def test_concurrent_body_limit_rejects_excess_but_health_stays_available():
    async def scenario():
        entered = asyncio.Event()
        finish = asyncio.Event()
        responses = []

        async def app(scope, receive, send):
            if scope["method"] == "POST":
                entered.set()
                await finish.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(event):
            responses.append(event)

        middleware = RequestLimitMiddleware(app, RequestLimitSettings(8, max_concurrent=1))
        scope = {"type": "http", "method": "POST", "headers": []}
        first = asyncio.create_task(middleware(scope, receive, send))
        await entered.wait()
        await middleware(scope, receive, send)
        assert responses[0]["status"] == 503
        assert dict(responses[0]["headers"])[b"retry-after"] == b"5"
        responses.clear()
        await middleware({**scope, "method": "GET"}, receive, send)
        assert responses[0]["status"] == 200
        finish.set()
        await first
        assert middleware._active_requests == 0

    asyncio.run(scenario())
