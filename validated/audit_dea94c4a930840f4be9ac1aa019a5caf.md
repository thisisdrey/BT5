## Finding

Request framing in `stackslib/src/net/http/request.rs` silently ignores the `Transfer-Encoding` header while `Content-Length` is authoritative for computing how many payload bytes to read. This mirrors the root cause of the Hono `bodyLimit` bypass (RFC-mandated precedence of `Transfer-Encoding` over `Content-Length` not being honored), but here it manifests as request-framing ambiguity rather than a size-limit-only issue.

### Title
HTTP request desync via ignored `Transfer-Encoding` header when `Content-Length` is also present — ([File: stackslib/src/net/http/request.rs])

### Summary
`HttpRequestPreamble::consensus_deserialize` parses every request header and special-cases `host`, `content-type`, `content-length`, `connection`, and `set-cookie`, but has **no handling at all for `transfer-encoding`** — it falls through to the generic branch and is stored as an ordinary, uninterpreted header value. [1](#0-0) 

Contrast this with the response-preamble parser in the same crate, which explicitly detects `transfer-encoding: chunked` and rejects the message if it conflicts with `Content-Length`: [2](#0-1) 

No equivalent check exists for requests. `HttpRequestPreamble::get_content_length()` always returns the (possibly attacker-supplied, possibly zero) `Content-Length` value, defaulting to `0` when absent: [3](#0-2) 

And the stream framing layer (`ProtocolFamily::payload_len`) uses exactly that value — never chunked framing — to decide how many bytes belong to the current request's body before the next message begins: [4](#0-3) 

`read_payload` then slices exactly `len = get_content_length()` bytes as this request's body from the buffered stream; anything beyond that is treated as the start of the **next** pipelined/keep-alive HTTP message: [5](#0-4) 

### Finding Description
The equality that should hold is: *the number of bytes the framing layer treats as "this request's body" must equal the number of bytes any front-end component (reverse proxy, load balancer) treats as "this request's body."* Per RFC 9112 §6.3, when both `Content-Length` and `Transfer-Encoding` are present, `Transfer-Encoding` takes precedence and `Content-Length` must be disregarded (this is precisely the rule Hono's fix (GHSA-92vj-g62v-jqhh) implements).

This node's request parser does the opposite of the compliant behavior: it never recognizes `Transfer-Encoding` for requests, so a request containing both headers is framed strictly by `Content-Length`. If a compliant, spec-following intermediary (reverse proxy/load balancer commonly placed in front of Stacks RPC nodes) forwards such a request by chunked framing (i.e., treats the whole chunked body as one request), but this node slices off only `Content-Length` bytes as the body, the remaining bytes of the "single" front-end request are left in the connection's read buffer. Because the connection is HTTP/1.1 keep-alive by default, `read_preamble`/`read_payload` will then parse those leftover bytes as an **entirely new, attacker-smuggled HTTP request** — one the front-end never saw as a discrete request and therefore could not apply access controls, routing, or rate limiting to. This is the classic CL.TE request-smuggling desync pattern.

### Impact Explanation
This breaks the "one front-end request == one back-end request" equality relied upon by any authorization or routing decisions made at a reverse proxy in front of the node (e.g., restricting `/v2/*` admin-ish endpoints, IP allow-lists, path-based auth for endpoints like block-proposal or StackerDB chunk posting which self-check an `Authorization` header). A smuggled request is injected as if it arrived on the same trusted connection, potentially bypassing front-end access controls and reaching handlers such as `RPCPostStackerDBChunkRequestHandler` or `RPCPostBlockRequestHandler` without the scrutiny the deployment topology assumed. This falls under the "auth bypass / request smuggling" critical-impact category.

### Likelihood Explanation
Exploitability depends on the specific reverse-proxy/CDN/load-balancer configuration in front of a given node — if the operator's front end also disregards `Transfer-Encoding` (or normalizes/strips it), there is no desync. Likelihood is therefore deployment-dependent rather than universal, but the underlying node-side parser provides no defense-in-depth: it doesn't reject ambiguous/conflicting `Content-Length` + `Transfer-Encoding` requests the way the response parser already does.

### Recommendation
Mirror the response-preamble logic in `HttpRequestPreamble::consensus_deserialize`: detect `transfer-encoding` headers, and if present alongside `Content-Length`, reject the request outright (`CodecError::DeserializeError`) rather than silently falling back to `Content-Length`-only framing. At minimum, reject any inbound request containing `Transfer-Encoding` at all, since chunked request bodies are not implemented/supported per `payload_len`'s unconditional `Some(content_length)` for requests.

### Proof of Concept
1. Attacker sends, through a spec-compliant reverse proxy in front of a Stacks node, a single POST with both:
   ```
   POST /v2/... HTTP/1.1
   Host: node
   Content-Length: 0
   Transfer-Encoding: chunked

   0\r\n\r\nPOST /v2/stackerdb/.../chunks HTTP/1.1\r\nHost: node\r\nContent-Length: <n>\r\n\r\n<smuggled body>
   ```
2. The proxy (honoring `Transfer-Encoding` per spec) treats everything up through the terminating chunk as the body of one request and forwards it unmodified on the keep-alive connection to the node.
3. The node's `HttpRequestPreamble::consensus_deserialize` ignores `Transfer-Encoding`, uses `Content-Length: 0`, and `read_payload` slices 0 bytes as this request's body [6](#0-5) .
4. The remaining bytes (the "chunk data" the proxy considered part of request #1's body) are parsed by the node as an independent, smuggled request #2 that never passed through the proxy's own per-request checks.

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

**File:** stackslib/src/net/http/request.rs (L381-422)
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

**File:** stackslib/src/net/httpcore.rs (L1549-1559)
```rust
    /// how big is this message?  Might not know if we're dealing with chunked encoding.
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
