### Title
HTTP request smuggling via unvalidated `Transfer-Encoding` header alongside `Content-Length` in `HttpRequestPreamble` parsing - (File: `stackslib/src/net/http/request.rs`)

### Summary
`HttpRequestPreamble::consensus_deserialize` determines the length of every HTTP request body solely from the `Content-Length` header, and never inspects or rejects a `Transfer-Encoding` header on incoming requests. If a `Transfer-Encoding: chunked` header is smuggled in alongside `Content-Length: N`, the Stacks node accepts the request, silently stores `Transfer-Encoding` as an opaque, unvalidated header, and reads exactly `N` bytes as the body — exactly the "Content-Length vs. Transfer-Encoding" ambiguity that underlies CVE-2017-7658.

### Finding Description
The response parser explicitly detects and rejects this ambiguity for HTTP responses: [1](#0-0) 

But the analogous check is entirely absent from the request parser. `HttpReservedHeader::is_reserved` only recognizes `content-length`, `content-type`, and `host`: [2](#0-1) 

In `HttpRequestPreamble::consensus_deserialize`, the header loop explicitly handles `host`, `content-type`, `content-length`, `connection`, and `set-cookie`; any other header name — including `transfer-encoding` — falls through to the generic "unknown header" branch and is stored verbatim with no semantic validation: [3](#0-2) 

The framing logic in `httpcore.rs` then uses `get_content_length()` unconditionally to determine how many bytes constitute the request body, with zero regard for any `Transfer-Encoding` header: [4](#0-3) [5](#0-4) 
Note the explicit assumption baked into `stream_payload` that "HTTP requests can't be chunk-encoded" — a claim not actually enforced during parsing: [6](#0-5) 

The equality broken here is: *the number of bytes the node consumes as "this request's body" must equal the number of bytes any front-facing intermediary (reverse proxy, load balancer, or TLS terminator commonly placed in front of the Stacks RPC API) consumes as "this request's body."* RFC 7230 §3.3.3 mandates that when both `Content-Length` and `Transfer-Encoding` are present, `Transfer-Encoding` takes precedence and `Content-Length` must be ignored/rejected. Since the Stacks node does the opposite (uses `Content-Length` and ignores `Transfer-Encoding`), an attacker who controls the raw bytes reaching the proxy can construct a request where the proxy parses the body length using chunked framing (consuming, say, 20 bytes as the "real" request+trailer) while the Stacks node reads only `Content-Length: N` bytes and treats the remaining smuggled bytes on the same keep-alive TCP connection as the start of an entirely new, injected pipelined HTTP request — bypassing whatever the proxy intended to authorize/filter for that connection.

This matches the request-preamble default of `keep_alive: true` for HTTP/1.1, so smuggled bytes on the same persistent connection will indeed be parsed as a subsequent request: [7](#0-6) [8](#0-7) 

### Impact Explanation
This is a classic request-smuggling primitive (CWE-444) reachable by any unauthenticated peer who can place traffic in front of a Stacks node deployed behind a standards-compliant reverse proxy/load balancer (a common production topology for RPC endpoints). It can be used to smuggle a request past proxy-level access controls (IP allow-lists, auth headers stripped/added by the proxy, rate limiting) directly to the Stacks node's RPC or P2P HTTP surface — matching the "request smuggling or auth bypass" Critical-impact category in scope. The smuggled request executes with whatever authority the legitimate connection carries (or with no restriction, since the proxy's own gate is bypassed).

### Likelihood Explanation
Exploitation requires no privileges and no secret key material — only the ability to send a crafted HTTP request through the intermediary to the target Stacks node. It does require a proxy/intermediary in the path that (per spec) prioritizes `Transfer-Encoding` over `Content-Length`; this is normal, RFC-compliant behavior for many popular reverse proxies. The vulnerability is purely in this repo's request parser failing to reject the ambiguous combination that the response parser already rejects, so it is a straightforward, low-effort defect rather than a theoretical one.

### Recommendation
In `HttpRequestPreamble::consensus_deserialize` (`stackslib/src/net/http/request.rs`), detect a `Transfer-Encoding` header on requests and either (a) reject the request outright with a 400-equivalent error if `Transfer-Encoding` is present at all (since chunked requests aren't supported), mirroring the response-side check at `stackslib/src/net/http/response.rs:575-586`, or (b) reject specifically when both `Content-Length` and `Transfer-Encoding` are present. This closes the ambiguity so any intermediary and the Stacks node necessarily agree on request framing.

### Proof of Concept
Send the following raw bytes over a persistent (keep-alive) connection to a Stacks node sitting behind a proxy that honors `Transfer-Encoding: chunked` over `Content-Length` (per RFC 7230):
```
POST /v2/some_endpoint HTTP/1.1
Host: node:20443
Content-Type: application/octet-stream
Content-Length: 6
Transfer-Encoding: chunked

0\r\n
\r\n
GET /v2/admin_only_endpoint HTTP/1.1
Host: node:20443

```
The proxy, per RFC 7230, parses the chunked body (`0\r\n\r\n` = empty body) and forwards the connection expecting the next bytes to be a new request from the same already-authorized/allow-listed client. The Stacks node, however, reads exactly `Content-Length: 6` bytes (`0\r\n\r\nG`) as this request's body and then re-enters the preamble parser on the remaining bytes (`ET /v2/admin_only_endpoint HTTP/1.1\r\nHost: ...`), interpreting them as an independently pipelined second request that the proxy never separately vetted — demonstrating the smuggling/desync condition rooted in `stackslib/src/net/http/request.rs`'s failure to validate `Transfer-Encoding`.

### Citations

**File:** stackslib/src/net/http/response.rs (L575-586)
```rust
                if content_length.is_some() && chunked_encoding {
                    return Err(CodecError::DeserializeError(
                        "Invalid HTTP response: incompatible transfer-encoding and content-length"
                            .to_string(),
                    ));
                }

                if content_length.is_none() && !chunked_encoding {
                    return Err(CodecError::DeserializeError(
                        "Invalid HTTP response: missing Content-Type, Content-Length".to_string(),
                    ));
                }
```

**File:** stackslib/src/net/http/common.rs (L24-30)
```rust
/// HTTP version (1.0 or 1.1)
#[derive(Debug, Clone, PartialEq, Copy, Hash)]
#[repr(u8)]
pub enum HttpVersion {
    Http10 = 0x10,
    Http11 = 0x11,
}
```

**File:** stackslib/src/net/http/common.rs (L41-43)
```rust
    pub fn is_reserved(header: &str) -> bool {
        matches!(header, "content-length" | "content-type" | "host")
    }
```

**File:** stackslib/src/net/http/request.rs (L346-349)
```rust
                let mut keep_alive = match version {
                    HttpVersion::Http10 => false,
                    HttpVersion::Http11 => true,
                };
```

**File:** stackslib/src/net/http/request.rs (L381-421)
```rust
                    if key == "host" {
                        peerhost = match value.parse::<PeerHost>() {
                            Ok(ph) => Some(ph),
                            Err(_) => None,
                        };
                        seen_headers.insert(key);
                    } else if key == "content-type" {
                        // parse
                        let ctype = value.to_lowercase().parse::<HttpContentType>()?;
                        content_type = Some(ctype);
                        seen_headers.insert(key);
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

**File:** stackslib/src/net/httpcore.rs (L1722-1730)
```rust
            StacksHttpPreamble::Request(ref http_request_preamble) => {
                // all requests have a known length
                let len = http_request_preamble.get_content_length() as usize;
                let Some(buf_data) = buf.get(0..len) else {
                    return Err(NetError::InvalidState);
                };

                trace!("read http request payload of {} bytes", len);

```
