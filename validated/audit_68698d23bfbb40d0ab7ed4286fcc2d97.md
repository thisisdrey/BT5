### Title
HTTP Request Smuggling via Transfer-Encoding/Content-Length Desync in Stacks HTTP Server - (File: stackslib/src/net/http/request.rs)

### Summary
The Stacks node's HTTP request parser never validates or rejects the `Transfer-Encoding` header on incoming requests, and always frames the request body strictly by `Content-Length`. This is the same TE/CL desync bug class as CVE-2020-11077 (Puma): a front-end proxy that honors `Transfer-Encoding: chunked` per RFC 7230 priority rules while the Stacks node ignores it and uses `Content-Length` instead will disagree about where one request ends and the next begins, enabling request smuggling across the persistent connection.

### Finding Description
`HttpRequestPreamble::consensus_deserialize` explicitly special-cases `host`, `content-type`, `content-length`, `connection`, and `set-cookie` headers, but any `transfer-encoding` header supplied by the client falls through to the generic `else` branch and is stored as an ordinary opaque header with no validation and no effect on parsing: [1](#0-0) 

Unlike the response-side parser, which explicitly detects and errors out on conflicting `Content-Length`/`Transfer-Encoding` combinations: [2](#0-1) 

there is no equivalent check anywhere in the request path. The request framing logic in the connection state machine (`ProtocolFamily::payload_len`) always resolves request body length purely from `Content-Length`, never treating requests as chunk-encoded: [3](#0-2) 

and `stream_payload` explicitly asserts that HTTP requests can never be chunk-encoded, which is enforced only by never reading `Transfer-Encoding` for requests, not by rejecting the header outright: [4](#0-3) 

This breaks the equality that a compliant reverse proxy and the origin server must agree on: where a request body ends. If an attacker sends a request with both `Content-Length: N` and `Transfer-Encoding: chunked` to a proxy that prioritizes `Transfer-Encoding` (as most compliant proxies do, per RFC 7230 §3.3.3), the proxy will forward the chunked-terminated body, but the Stacks node will read exactly `N` bytes and treat any trailing bytes as the start of a *new, independent* HTTP request on the same persistent connection — the classic TE.CL smuggling pattern that CVE-2020-11077 patched in Puma.

### Impact Explanation
Any deployment that fronts the Stacks node's RPC HTTP interface with a reverse proxy or load balancer that reuses persistent (`keep-alive`) connections is exposed to request smuggling: an attacker-supplied hidden request can be spliced into another client's connection, resulting in cross-client response confusion, cache poisoning, or bypass of proxy-enforced access controls (e.g., IP allow-lists or auth headers stripped/added by the proxy). This matches the Critical impact category "request smuggling or auth bypass."

### Likelihood Explanation
Remote and unauthenticated — requires no keys, no admin role, and only a few crafted HTTP requests to a keep-alive connection through a compliant proxy. The vulnerability is a direct parser omission (missing `Transfer-Encoding` validation/rejection for requests) rather than a probabilistic or volumetric issue.

### Recommendation
In `HttpRequestPreamble::consensus_deserialize`, explicitly detect the `transfer-encoding` header the same way the response parser does, and reject (or normalize) requests that specify both `Transfer-Encoding` and `Content-Length`, and reject any `Transfer-Encoding` value other than nothing at all for requests (since requests are documented as never being chunked). This closes the framing ambiguity outright rather than relying on proxies to resolve it consistently with this codebase's behavior.

### Proof of Concept
Send, through a proxy that prioritizes `Transfer-Encoding`, a request such as:
```
POST /v2/... HTTP/1.1
Host: victim
Content-Length: 4
Transfer-Encoding: chunked

0

GET /admin-or-other-endpoint HTTP/1.1
Host: victim
...
```
The proxy treats the body as the zero-length chunked terminator (`0\r\n\r\n`) and forwards the smuggled second request as part of the same body/frame; the Stacks node instead reads exactly 4 bytes (`Content-Length: 4`) as the first request's body and treats the remaining smuggled bytes as an independent second request on the same connection, per the parsing behavior shown in `stackslib/src/net/http/request.rs:392-421` and `stackslib/src/net/httpcore.rs:1550-1559`.

### Citations

**File:** stackslib/src/net/http/request.rs (L392-421)
```rust
                    } else if key == "content-length" {
                        // parse
                        content_length = match value.parse::<u32>() {
                            Ok(len) => Some(len),
                            Err(_) => None,
                        };
                        seen_headers.insert(key);
                    } else if key == "connection" {
                        // parse
                        if value.to_lowercase() == "close" {
                            keep_alive = false;
                        } else if value.to_lowercase() == "keep-alive" {
                            keep_alive = true;
                        } else {
                            return Err(CodecError::DeserializeError(
                                "Inavlid HTTP request: invalid Connection: header".to_string(),
                            ));
                        }
                        seen_headers.insert(key);
                    } else if key == "set-cookie" {
                        set_cookie.push(value);
                    } else {
                        headers
                            .entry(key)
                            .and_modify(|entry| {
                                entry.push_str(", ");
                                entry.push_str(&value);
                            })
                            .or_insert(value);
                    }
```

**File:** stackslib/src/net/http/response.rs (L561-580)
```rust
                    } else if key == "transfer-encoding" {
                        if value.to_lowercase() == "chunked" {
                            chunked_encoding = true;
                        } else {
                            return Err(CodecError::DeserializeError(format!(
                                "Unsupported transfer-encoding '{}'",
                                value
                            )));
                        }
                    } else {
                        headers.insert(key, value);
                    }
                }

                if content_length.is_some() && chunked_encoding {
                    return Err(CodecError::DeserializeError(
                        "Invalid HTTP response: incompatible transfer-encoding and content-length"
                            .to_string(),
                    ));
                }
```

**File:** stackslib/src/net/httpcore.rs (L1550-1559)
```rust
    fn payload_len(&mut self, preamble: &StacksHttpPreamble) -> Option<usize> {
        match *preamble {
            StacksHttpPreamble::Request(ref http_request_preamble) => {
                Some(http_request_preamble.get_content_length() as usize)
            }
            StacksHttpPreamble::Response(ref http_response_preamble) => http_response_preamble
                .content_length
                .map(|len| len as usize),
        }
    }
```

**File:** stackslib/src/net/httpcore.rs (L1619-1623)
```rust
        match preamble {
            StacksHttpPreamble::Request(_) => {
                // HTTP requests can't be chunk-encoded, so this should never be reached
                return Err(NetError::InvalidState);
            }
```
