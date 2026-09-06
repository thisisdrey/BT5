### Title
HTTP Request Smuggling via Ignored `Transfer-Encoding` Header in Request Preamble Parser - (File: `stackslib/src/net/http/request.rs`)

### Summary
`HttpRequestPreamble::consensus_deserialize` in `stackslib/src/net/http/request.rs` never inspects the `Transfer-Encoding` header at all. Unlike the response-preamble parser, which explicitly recognizes `transfer-encoding`, rejects unsupported values, and errors if both `Content-Length` and chunked encoding are present, the request parser silently drops `Transfer-Encoding` into the generic headers map and always frames the request body using `Content-Length` (defaulting to 0 if absent). This reproduces the exact CVE-2019-16786 bug class: a message-framing field (`Transfer-Encoding`) is ignored, so a front-end proxy and the Stacks node can disagree about where one HTTP request ends and the next begins.

### Finding Description
In `stackslib/src/net/http/request.rs`, the header loop (lines 355-422) matches on `"host"`, `"content-type"`, `"content-length"`, `"connection"`, and `"set-cookie"`; any other header, including `"transfer-encoding"`, falls into the catch-all `else` branch and is stored as an ordinary opaque header [1](#0-0) .

Contrast this with `HttpResponsePreamble::consensus_deserialize`, which explicitly parses `transfer-encoding`, sets `chunked_encoding`, rejects any value other than `chunked`, and refuses a preamble that carries both `Content-Length` and chunked transfer-encoding [2](#0-1) . No equivalent logic exists on the request side.

Framing of the request body is then driven purely by `Content-Length`: `ProtocolFamily::payload_len` for `StacksHttpPreamble::Request` always returns `Some(http_request_preamble.get_content_length() as usize)` [3](#0-2) , and `get_content_length()` falls back to `0` when no `Content-Length` header was present [4](#0-3) . `read_payload` reads exactly that many bytes as the request body and then resumes parsing the next preamble from the following bytes on the same (typically `keep_alive`) connection [5](#0-4) .

This breaks the equality that a front-end (any reverse proxy/load balancer honoring `Transfer-Encoding` per RFC 7230, terminating TLS in front of the Stacks RPC/P2P HTTP API) and the Stacks node must agree on. A crafted request containing both `Content-Length: N` and `Transfer-Encoding: chunked` (or a comma-separated/obfuscated variant such as `Transfer-Encoding: chunked, identity` or `xchunked`) will be framed by a compliant proxy as chunked, but the Stacks node ignores `Transfer-Encoding` entirely and instead consumes exactly the bytes indicated by `Content-Length` as the current request's body, treating whatever bytes follow as the start of a new, independent HTTP request/response on the same keep-alive connection [6](#0-5) .

### Impact Explanation
On a keep-alive HTTP/1.1 connection (the default for Stacks HTTP requests — `keep_alive` defaults to `true` for `HttpVersion::Http11` [7](#0-6) ), an attacker who can route traffic through any intermediary that honors `Transfer-Encoding` for framing (a common reverse-proxy/CDN deployment pattern in front of Stacks API endpoints) can smuggle a hidden, attacker-controlled HTTP request inside the body of a legitimate one. The smuggled request is parsed by the Stacks node as if it arrived independently over the shared connection, potentially bypassing proxy-level access controls, request filtering, or IP allow-lists placed in front of privileged/authenticated endpoints (e.g. `/v3/block_proposal`, `/v3/transactions/simulate`), and can desynchronize the proxy's and node's view of the request stream on shared/pooled connections. This aligns with the Critical bucket ("request smuggling or auth bypass") defined in scope.

### Likelihood Explanation
Exploitability requires only a single crafted HTTP request with conflicting `Content-Length`/`Transfer-Encoding` headers sent through an intermediary that honors `Transfer-Encoding`; no authentication or special network position is needed to reach the request parser itself, since `consensus_deserialize` is invoked unconditionally on every inbound HTTP connection to the node's RPC interface [8](#0-7) . Real-world impact is conditioned on a specific deployment topology (a `Transfer-Encoding`-aware proxy in front of the node), which is common but not universal, so likelihood is assessed as depending on deployment rather than being universally exploitable.

### Recommendation
Mirror the response-preamble logic in the request parser: explicitly recognize the `transfer-encoding` header, reject any value other than `chunked`, and reject a preamble that specifies both `Content-Length` and `Transfer-Encoding: chunked` (or any transfer-encoding at all, since request chunked-decoding is not implemented for the request path) by returning a `400`/parse error instead of silently falling back to `Content-Length` framing.

### Proof of Concept
Send the following over a persistent (`keep-alive`) connection to the Stacks HTTP RPC listener, through a `Transfer-Encoding`-honoring intermediary:
```
POST /v2/transactions HTTP/1.1
Host: <node>:20443
Content-Length: 4
Transfer-Encoding: chunked

0

GET /v3/block_proposal HTTP/1.1
Host: <node>:20443
...
```
The intermediary frames this as one chunked request with an empty body followed by a second, fully independent request (`GET /v3/block_proposal...`). The Stacks node, per `HttpRequestPreamble::consensus_deserialize` and `payload_len`, ignores `Transfer-Encoding`, reads exactly 4 bytes (`"0\r\n\r"`) as the body of the first request, and then parses the remaining smuggled bytes as a new request on the same connection [5](#0-4) .

### Citations

**File:** stackslib/src/net/http/request.rs (L202-207)
```rust
    /// Content-Length for this request.
    /// If there is no valid Content-Length header, then
    /// the Content-Length is 0
    pub fn get_content_length(&self) -> u32 {
        self.content_length.unwrap_or(0)
    }
```

**File:** stackslib/src/net/http/request.rs (L346-349)
```rust
                let mut keep_alive = match version {
                    HttpVersion::Http10 => false,
                    HttpVersion::Http11 => true,
                };
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

**File:** stackslib/src/net/httpcore.rs (L1561-1599)
```rust
    /// Read the next HTTP preamble (be it a request or a response), and return the preamble and
    /// the number of bytes consumed while reading it.
    fn read_preamble(&mut self, buf: &[u8]) -> Result<(StacksHttpPreamble, usize), NetError> {
        // does this contain end-of-headers marker, including the last four bytes of preamble we
        // saw?
        if self.body_start.is_none() {
            for i in 0..=buf.len() {
                let window = self.body_start_search_window(i, buf);
                if window == [b'\r', b'\n', b'\r', b'\n'] {
                    self.body_start = Some(self.num_preamble_bytes + i);
                }
            }
        }
        if self.body_start.is_none() {
            // haven't found the body yet, so update `last_four_preamble_bytes`
            // and report underflow
            let len = buf.len();
            let last_four_preamble_bytes = self.body_start_search_window(len, buf);
            self.num_preamble_bytes += len;
            self.last_four_preamble_bytes = last_four_preamble_bytes;
            return Err(NetError::UnderflowError(
                "Not enough bytes to form HTTP preamble".into(),
            ));
        }

        let mut cursor = io::Cursor::new(buf);

        let preamble = {
            let mut rd =
                BoundReader::from_reader(&mut cursor, HTTP_PREAMBLE_MAX_ENCODED_SIZE as u64);
            let preamble: StacksHttpPreamble = read_next(&mut rd)?;
            preamble
        };

        let preamble_len = cursor.position() as usize;
        self.set_preamble(&preamble)?;

        Ok((preamble, preamble_len))
    }
```

**File:** stackslib/src/net/httpcore.rs (L1716-1730)
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

```
