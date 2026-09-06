### Title
HTTP Request Smuggling via silently-ignored `Transfer-Encoding` header in request preamble parsing - ([File: stackslib/src/net/http/request.rs])

### Summary
`HttpRequestPreamble::consensus_deserialize` in `stackslib/src/net/http/request.rs` parses incoming HTTP request headers and special-cases only `host`, `content-type`, `content-length`, `connection`, and `set-cookie`. Any other header — including `Transfer-Encoding` — is stored verbatim into the generic `headers` map with no validation, and body length is determined solely by `Content-Length` via `get_content_length()`. There is no rejection of a request that carries both `Content-Length` and `Transfer-Encoding`, and no chunked-decoding path exists for requests at all (the sibling `stream_payload` function explicitly states "HTTP requests can't be chunk-encoded" and returns `NetError::InvalidState` if ever invoked on a request).

### Finding Description
`HttpRequestPreamble::consensus_deserialize` [1](#0-0)  loops over parsed headers and only recognizes `host`, `content-type`, `content-length`, `connection`, and `set-cookie` as reserved/special headers; anything else, including a `transfer-encoding` header, falls into the generic `else` branch and is inserted into `self.headers` unchanged — it is never inspected, rejected, or reconciled against `Content-Length`.

Body length for requests is computed exclusively from `Content-Length`: `ProtocolFamily::payload_len` returns `Some(http_request_preamble.get_content_length() as usize)` for `StacksHttpPreamble::Request` [2](#0-1) , and `get_content_length()` simply falls back to `content_length.unwrap_or(0)` [3](#0-2) . Because `payload_len` returns `Some(..)` for every request, `stream_payload` (the chunked-decoding path) is never reached for requests, and is even documented/coded to error out if it were: `"HTTP requests can't be chunk-encoded, so this should never be reached"` [4](#0-3) .

Contrast this with the **response** parser, `HttpResponsePreamble::consensus_deserialize`, which does track `transfer-encoding` and explicitly rejects the combination of `Content-Length` and chunked `Transfer-Encoding` as an inconsistent message: `"Invalid HTTP response: incompatible transfer-encoding and content-length"` [5](#0-4) . The equivalent check is absent for requests — this is exactly the equality that CVE-2022-32213's bug class breaks: a message's true framing length as determined by one parser (or an upstream intermediary honoring `Transfer-Encoding`) can diverge from the length this HTTP layer uses to delimit the request body.

If a reverse proxy, load balancer, or any HTTP/1.1-compliant intermediary in front of a Stacks node's RPC server honors `Transfer-Encoding: chunked` per RFC 7230 (which mandates T-E take precedence over Content-Length when both are present), while the node itself always frames the request by `Content-Length` and treats `Transfer-Encoding` as an arbitrary, ignored header, the two components disagree on where one request ends and the next begins on the same persistent connection — the classic CL.TE smuggling primitive. An attacker-supplied request with conflicting `Content-Length`/`Transfer-Encoding` headers is accepted without any error from this node, whereas RFC 7230 §3.3.3 requires such a message to be rejected as ambiguous.

### Impact Explanation
This breaks the framing "bytes vs length" equality required for correct request/response pairing on a shared connection. In deployments where the stacks node's HTTP RPC endpoint sits behind any proxy/gateway that implements Transfer-Encoding chunking, an attacker can smuggle a hidden, unauthenticated follow-up request that the proxy believes is data (or vice versa), enabling request splitting/smuggling: bypassing proxy-enforced ACLs, poisoning connection reuse, or getting privileged internal-only requests processed against the node using another user's proxy-authenticated connection. This falls under the Critical category "request smuggling or auth bypass."

### Likelihood Explanation
Reaching this only requires sending a single crafted, unauthenticated HTTP request to a node's RPC listener with both `Content-Length` and `Transfer-Encoding: chunked` headers set inconsistently — no privileged key, signer role, or node secret is required, and no volumetric traffic is needed. However, real-world exploitability depends on the node being deployed behind an intermediary that also processes `Transfer-Encoding`; on a direct network-facing node with no proxy, there's no second parser to desynchronize against, which is a mitigating deployment factor I could not verify further given the indexed code scope (no proxy/deployment configuration was found in-repo).

### Recommendation
In `HttpRequestPreamble::consensus_deserialize`, explicitly detect a `transfer-encoding` header and either (a) reject the request outright with a `CodecError::DeserializeError` if `Transfer-Encoding` is present at all (Stacks HTTP requests are documented as never chunked), mirroring the response-side rejection of conflicting `Content-Length`/`Transfer-Encoding`, or (b) reject if both `Content-Length` and `Transfer-Encoding` headers are simultaneously present, per RFC 7230 §3.3.3.

### Proof of Concept
Send to any Stacks node RPC endpoint:
```
POST /v2/transactions HTTP/1.1
Host: node:20443
Content-Length: 6
Transfer-Encoding: chunked

0

G
```
`HttpRequestPreamble::consensus_deserialize` parses this successfully, storing `transfer-encoding` as an ordinary header and `content_length = Some(6)` [6](#0-5) ; the node will read exactly 6 bytes as the body (`"0\r\n\r\nG"`) via `get_content_length()` [7](#0-6) , while any RFC7230-compliant intermediary in front of it would instead treat the message as a zero-length chunked body ending after `0\r\n\r\n`, leaving the trailing `G...` bytes to be interpreted as the start of a smuggled next request on the same connection.

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
