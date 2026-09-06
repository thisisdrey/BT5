### Title
`HttpRequestPreamble::consensus_deserialize` accepts ambiguous `Content-Length` + `Transfer-Encoding: chunked` requests, unlike the response parser - ([File: stackslib/src/net/http/request.rs])

### Summary
`HttpResponsePreamble::consensus_deserialize` explicitly rejects a response preamble that carries both `Content-Length` and `Transfer-Encoding: chunked` [1](#0-0) , but the request-side parser, `HttpRequestPreamble::consensus_deserialize`, has no equivalent branch at all for `Transfer-Encoding` - it is not recognized as a special header and is silently stored as an opaque header while `Content-Length` is parsed and used on its own [2](#0-1) . This is a genuine asymmetry between the two implementations in the same module.

### Finding Description
The response-side deserializer tracks a `chunked_encoding` boolean set from the `transfer-encoding` header and enforces the invariant `content_length.is_some() && chunked_encoding => reject` [1](#0-0) . The request-side deserializer's header loop only special-cases `host`, `content-type`, `content-length`, `connection`, and `set-cookie`; any other header including `transfer-encoding` falls into the generic `else` branch and is stored unexamined in the `headers` map [3](#0-2) . There is no `chunked_encoding` variable, no `Transfer-Encoding` field on `HttpRequestPreamble`, and no rejection check anywhere in this function. `content_length` is parsed independently and always becomes the sole framing signal used later via `get_content_length`/`payload_len` [4](#0-3) .

Because the request path never interprets `Transfer-Encoding` at all, this node's own body-length determination is internally consistent (always Content-Length-based) — there is no first-pass/second-pass desync purely within this repository's own request handler. However, per RFC 7230 §3.3.3, a compliant intermediary (reverse proxy, load balancer, or another HTTP/1.1-aware component in front of the RPC port) is required to treat `Transfer-Encoding` as authoritative when both headers are present, and either reject the request or use chunked framing. Since this node instead honors `Content-Length` unconditionally and never validates the illegal combination, an attacker who can place such an intermediary between themselves and the node (or who controls both ends of a pipelined connection with any such intermediary) can construct classic CL.TE smuggling payloads: the proxy consumes the body via chunked semantics while this node reads exactly `Content-Length` bytes, causing the remainder of the "chunk" to be reinterpreted as the start of a new, attacker-controlled pipelined request on the same keep-alive connection.

### Impact Explanation
On the node itself, the missing check does not cause an internal double-parse of the same request (there is only one path: Content-Length), so no direct single-node desync occurs. The real impact is that this code fails to implement the RFC-mandated defensive rejection that the response parser already implements, making the RPC port an eligible "back-end" for classic HTTP request smuggling in any deployment that fronts the node with a reverse proxy - a common production topology. This can let an attacker's pipelined follow-on bytes be misattributed to a different logical request on the same connection, potentially smuggling requests to auth-gated endpoints or poisoning other clients' responses on shared keep-alive connections at the proxy layer.

### Likelihood Explanation
Directly against the bare RPC port with no intermediary, the missing check has no observable effect since the node deterministically uses `Content-Length`. The higher-severity smuggling scenario requires a reverse proxy or similar intermediary in front of the node, which is a very common but not universal deployment. The attacker needs no privileges, no secret, and no admin role - only the ability to send an HTTP request to the RPC port - matching the "unprivileged remote attacker" threat model.

### Recommendation
Add the same guard used in `HttpResponsePreamble::consensus_deserialize` to `HttpRequestPreamble::consensus_deserialize`: track a `chunked_encoding` flag when the `transfer-encoding` header is seen, and reject the preamble with `CodecError::DeserializeError` if both `Content-Length` and `Transfer-Encoding: chunked` are present, mirroring `stackslib/src/net/http/response.rs:575-580`.

### Proof of Concept
Add a test in `stackslib/src/net/http/request.rs`'s test module that feeds:
```
POST /v2/transactions HTTP/1.1\r\nHost: 127.0.0.1:20443\r\nContent-Length: 10\r\nTransfer-Encoding: chunked\r\n\r\n
```
into `HttpRequestPreamble::consensus_deserialize`, and assert it returns `Err(CodecError::DeserializeError(_))`. Currently this assertion fails because the call returns `Ok(HttpRequestPreamble { content_length: Some(10), .. })` with the `Transfer-Encoding` header silently dropped into the generic headers map [5](#0-4) .

### Citations

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

**File:** stackslib/src/net/http/request.rs (L202-207)
```rust
    /// Content-Length for this request.
    /// If there is no valid Content-Length header, then
    /// the Content-Length is 0
    pub fn get_content_length(&self) -> u32 {
        self.content_length.unwrap_or(0)
    }
```

**File:** stackslib/src/net/http/request.rs (L302-420)
```rust
    fn consensus_deserialize<R: Read>(fd: &mut R) -> Result<HttpRequestPreamble, CodecError> {
        // realistically, there won't be more than HTTP_PREAMBLE_MAX_NUM_HEADERS headers
        let mut headers = [httparse::EMPTY_HEADER; HTTP_PREAMBLE_MAX_NUM_HEADERS];
        let mut req = httparse::Request::new(&mut headers);

        let buf_read = read_to_crlf2(fd)?;

        // consume request
        match req.parse(&buf_read).map_err(|e| {
            CodecError::DeserializeError(format!("Failed to parse HTTP request: {:?}", &e))
        })? {
            httparse::Status::Partial => {
                // partial
                return Err(CodecError::UnderflowError(
                    "Not enough bytes to form a HTTP request preamble".to_string(),
                ));
            }
            httparse::Status::Complete(_) => {
                // consumed all headers.  body_offset points to the start of the request body
                let version = match req
                    .version
                    .ok_or(CodecError::DeserializeError("No HTTP version".to_string()))?
                {
                    0 => HttpVersion::Http10,
                    1 => HttpVersion::Http11,
                    _ => {
                        return Err(CodecError::DeserializeError(
                            "Invalid HTTP version".to_string(),
                        ));
                    }
                };

                let verb = req
                    .method
                    .ok_or(CodecError::DeserializeError("No HTTP method".to_string()))?
                    .to_string();
                let path_and_query_str = req
                    .path
                    .ok_or(CodecError::DeserializeError("No HTTP path".to_string()))?
                    .to_string();

                let mut peerhost = None;
                let mut content_type = None;
                let mut content_length = None;
                let mut keep_alive = match version {
                    HttpVersion::Http10 => false,
                    HttpVersion::Http11 => true,
                };

                let mut headers: BTreeMap<String, String> = BTreeMap::new();
                let mut seen_headers: HashSet<String> = HashSet::new();
                let mut set_cookie = vec![];

                for req_header in req.headers.iter() {
                    let value = String::from_utf8(req_header.value.to_vec()).map_err(|_e| {
                        CodecError::DeserializeError(
                            "Invalid HTTP header value: not utf-8".to_string(),
                        )
                    })?;
                    if !value.is_ascii() {
                        return Err(CodecError::DeserializeError(
                            "Invalid HTTP request: header value is not ASCII-US".to_string(),
                        ));
                    }
                    if value.len() > HTTP_PREAMBLE_MAX_ENCODED_SIZE as usize {
                        return Err(CodecError::DeserializeError(
                            "Invalid HTTP request: header value is too big".to_string(),
                        ));
                    }

                    let key = req_header.name.to_lowercase();

                    if seen_headers.contains(&key) {
                        return Err(CodecError::DeserializeError(format!(
                            "Invalid HTTP request: duplicate header \"{}\"",
                            key
                        )));
                    }

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
```
