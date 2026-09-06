Confirmed: `Transfer-Encoding` never appears anywhere in `stackslib/src/net/http/request.rs` — only in `response.rs`. This confirms requests never recognize/validate the `Transfer-Encoding` header at all, while responses do.

### Title
HTTP Request Smuggling via unvalidated `Transfer-Encoding` header on incoming Stacks HTTP requests - (File: `stackslib/src/net/http/request.rs`)

### Summary
`HttpRequestPreamble::consensus_deserialize` (the incoming-request preamble parser) recognizes and validates `Host`, `Content-Type`, `Content-Length`, `Connection`, and `Set-Cookie`, but has no case for `Transfer-Encoding` at all. A `Transfer-Encoding: chunked` header on an inbound request is silently folded into the generic `headers` `BTreeMap` and completely ignored for framing purposes. Downstream, `StacksHttp::payload_len` for requests unconditionally uses `get_content_length()` [1](#0-0)  — i.e. the Stacks HTTP server always frames request bodies by `Content-Length`, never by chunked encoding, and never rejects a request that carries both headers.

### Finding Description
The response parser explicitly guards against the ambiguous combination that enables classic request smuggling: [2](#0-1) 

The request parser has no equivalent branch — `key == "transfer-encoding"` is never matched, so it falls into the generic header bucket: [3](#0-2) 

And there is no `chunked_encoding`/`Transfer-Encoding` field on `HttpRequestPreamble` at all: [4](#0-3) .

Because `payload_len()` for `StacksHttpPreamble::Request` always trusts `Content-Length` and there is no rejection of a simultaneous `Transfer-Encoding` header, this breaks the equality that HTTP/1.1 intermediaries rely on: "the frontend and backend agree on where one request ends and the next begins." If a stacks-node is deployed behind any HTTP/1.1-aware reverse proxy, load balancer, or CDN (a common production topology for public RPC endpoints) that honors `Transfer-Encoding` when both headers are present (per RFC 7230 §3.3.3, TE should take priority over CL), the frontend will parse the connection using chunked framing while stacks-node parses it using `Content-Length` framing. An attacker can smuggle a second, fully-formed HTTP request inside what the frontend considers the "chunked body" of the first request; the backend (stacks-node) will treat only the `Content-Length` bytes as request #1's body and the remainder — attacker-controlled bytes appearing after the declared `Content-Length` — as the start of a new pipelined request on the same keep-alive connection, exactly the "CL.TE" class described in the Tomcat trailer-parsing report.

### Impact Explanation
This maps to the listed Critical impact category "request smuggling or auth bypass." On a keep-alive connection shared by multiple downstream users behind a proxy, request smuggling can let an attacker inject requests that get attributed to, or interleaved with, another legitimate client's connection (e.g. poisoning what the proxy forwards next, or bypassing proxy-level access control/IP allow-lists in front of RPC endpoints such as `/v3/block_proposal` or `/v2/blocks/upload`, which are otherwise gated by an `Authorization` header check that the proxy might be relied upon to enforce).

### Likelihood Explanation
Reaching this code path requires only a single unauthenticated raw HTTP request with both `Content-Length` and `Transfer-Encoding: chunked` headers set — no signature, key, or privileged role is needed. It is remote and requires no special conditions beyond a standard reverse-proxy deployment, which is typical for exposed Stacks RPC endpoints.

### Recommendation
Reject any incoming request that specifies `Transfer-Encoding` (particularly combined with `Content-Length`), mirroring the check already present in `HttpResponsePreamble::consensus_deserialize` at `stackslib/src/net/http/response.rs:575-580`. At minimum, add a `transfer-encoding` branch in `HttpRequestPreamble::consensus_deserialize` that errors out if a chunked (or unsupported) transfer-encoding is present, since request framing here is Content-Length-only and can never legitimately be paired with a chunked encoding.

### Proof of Concept
```
POST /v2/transactions HTTP/1.1
Host: victim-node:20443
Content-Type: application/octet-stream
Content-Length: 6
Transfer-Encoding: chunked

0

GET /v2/blocks/upload/evilhash HTTP/1.1
Host: victim-node:20443
...
```
A compliant HTTP/1.1 proxy in front of the node treats `Transfer-Encoding` as authoritative and forwards the "0\r\n\r\n" as terminating the chunked body, then treats the remaining bytes (`GET /v2/blocks/upload/...`) as belonging to the next pipelined request on the connection. stacks-node's `consensus_deserialize`/`payload_len` instead only recognizes `Content-Length: 6`, so it consumes exactly 6 bytes (`0\r\n\r\n`) as the body and then re-parses the smuggled bytes as the start of a second, attacker-controlled request — desynchronizing the proxy's and the node's view of the connection [5](#0-4) .

### Citations

**File:** stackslib/src/net/httpcore.rs (L1550-1554)
```rust
    fn payload_len(&mut self, preamble: &StacksHttpPreamble) -> Option<usize> {
        match *preamble {
            StacksHttpPreamble::Request(ref http_request_preamble) => {
                Some(http_request_preamble.get_content_length() as usize)
            }
```

**File:** stackslib/src/net/httpcore.rs (L1716-1732)
```rust
    fn read_payload(
        &mut self,
        preamble: &StacksHttpPreamble,
        buf: &[u8],
    ) -> Result<(StacksHttpMessage, usize), NetError> {
        match preamble {
            StacksHttpPreamble::Request(ref http_request_preamble) => {
                // all requests have a known length
                let len = http_request_preamble.get_content_length() as usize;
                let Some(buf_data) = buf.get(0..len) else {
                    return Err(NetError::InvalidState);
                };

                trace!("read http request payload of {} bytes", len);

                match self.try_parse_request(http_request_preamble, buf_data) {
                    Ok(data_request) => Ok((StacksHttpMessage::Request(data_request), len)),
```

**File:** stackslib/src/net/http/response.rs (L561-586)
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

                if content_length.is_none() && !chunked_encoding {
                    return Err(CodecError::DeserializeError(
                        "Invalid HTTP response: missing Content-Type, Content-Length".to_string(),
                    ));
                }
```

**File:** stackslib/src/net/http/request.rs (L37-56)
```rust
pub struct HttpRequestPreamble {
    /// HTTP version (1.0 or 1.1)
    pub version: HttpVersion,
    /// HTTP verb
    pub verb: String,
    /// Fully-qualified HTTP request path, including query string
    pub path_and_query_str: String,
    /// `Host:` value
    pub host: PeerHost,
    /// `Content-Type:` value, if given.  Not all requests need this.
    pub content_type: Option<HttpContentType>,
    /// `Content-Length:` value, if given.  Not all requests need this.
    pub content_length: Option<u32>,
    /// true if `Connection: keep-alive` was set
    pub keep_alive: bool,
    /// Other headers that were not consumed in parsing
    pub headers: BTreeMap<String, String>,
    /// `Set-Cookie` headers
    pub set_cookie: Vec<String>,
}
```

**File:** stackslib/src/net/http/request.rs (L392-422)
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
                }
```
