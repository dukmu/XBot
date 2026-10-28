# Shared wire protocol

`api/models.py` declares the shared HTTP and event envelopes, `api/version.py` the protocol version, `api/sse.py` SSE encoding and incremental decoding, and `api/http_util.py` common HTTP error and streaming helpers. These modules do not discover plugins or own business state.

The wire models depend on core domain values; HTTP helpers use FastAPI. An SSE decoder holds only its current incremental buffer; parsing and serialization perform no network I/O. There is no plugin configuration. For example, encode a `ServerEvent` with `encode_server_event(event)` and feed decoded text lines into `SseDecoder` before calling `decode_server_event` on complete messages.
