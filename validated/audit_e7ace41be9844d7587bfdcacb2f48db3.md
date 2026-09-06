HTTP request smuggling (CL.TE-style desync) analog found in the Stacks node's HTTP request preamble parser.

### Title
HTTP Request Smuggling via ignored `Transfer-Encoding` header in `HttpRequestPreamble` parsing - (File: `stackslib/src/net/http/request.rs`)

### Summary
The Stacks node's HTTP/1.1 request parser (`HttpRequestPreamble::consensus_deserialize`) explicitly parses and validates `Host`, `Content-Type`, `Content-Length`, `Connection`, and `Set-Cookie`, but has **no branch for `Transfer-Encoding`** — it silently falls into the generic "other headers" bucket and is never inspected or rejected. [1](#0-0) 

Meanwhile, the framing layer that drives how many body bytes to read for a request always uses `Content-Length` and unconditionally documents that chunked requests are impossible: [2](#0-1) [3](#0-2) [4](#0-3) 

This is the structural equivalent of the actix-http CVE-2021-38512 bug class: a backend that trusts `Content-Length` while ignoring/mis-handling `Transfer-Encoding` creates a CL.TE desynchronization opportunity whenever the request passes through any intermediary (reverse proxy, load balancer, or the signer/API gateway) that instead honors `Transfer-Encoding` (per RFC 7230 §3.3.3, `Transfer-Encoding` takes precedence over `Content-Length` when both are present, and most compliant proxies enforce/prefer TE framing or reject the ambiguous message — but if the front-end doesn't reject it, it will forward/tunnel the message using TE framing).

### Finding Description
When both `Content-Length` and `Transfer-Encoding: chunked` are present on a single request to the Stacks node's HTTP RPC/API endpoint:
- Any TE-aware front-end (or an HTTP/1.1 client library not itself hardened against this ambiguity) may treat the message boundary via `Transfer-Encoding`.
- The Stacks node itself only ever reads `Content-Length` bytes as the request body — the `transfer-encoding` header value is parsed into the free-form `headers: BTreeMap` and completely ignored for framing purposes (see the `else` branch of the header loop in `consensus_deserialize`, `stackslib/src/net/http/request.rs:411-421`).
- Because the node keeps the connection alive by default for HTTP/1.1 (`keep_alive` defaults `true` for `HttpVersion::Http11`, `stackslib/src/net/http/request.rs:346-349`), any leftover bytes after the node's shorter/longer Content-Length-based read are treated as the start of the **next** pipelined request on the same connection.

This breaks the equality "bytes an intermediary believes were consumed for request N" vs "bytes the Stacks node actually consumed for request N," which is exactly the desynchronization primitive from GHSA-8928-2fgm-6x9x / CVE-2021-38512 — the node is the "backend" that disagrees with a TE-honoring "front-end" about request boundaries.

By contrast, the HTTP *response* parser in this same module explicitly rejects the ambiguous combination: [5](#0-4) 
— confirming the developers were aware of and specifically guarded against this ambiguity on the response side, but the identical guard is absent on the request side.

### Impact Explanation
If the Stacks node's RPC/API HTTP port is placed behind any TE-honoring intermediary (a common production deployment pattern for exposing `/v2/*` endpoints), an unauthenticated remote attacker can smuggle a hidden, attacker-chosen HTTP request into another user's/service's TCP connection to the node, or desynchronize the proxy and node's view of the stream. Depending on what sits in front, this can be leveraged to bypass any proxy-level access controls (e.g., IP allow-lists or auth added only at the proxy) or corrupt/cross responses between unrelated callers, i.e., authentication/authorization bypass and data smuggling into StackerDB/RPC POST endpoints such as `postblock` or `poststackerdbchunk`, whose handlers only check `preamble.get_content_length()` and never validate framing consistency (`stackslib/src/net/api/poststackerdbchunk.rs:74-84`, `stackslib/src/net/api/postblock_v3.rs:87-97`).

### Likelihood Explanation
Exploitation requires the attacker to control the raw HTTP bytes sent to a connection that traverses both a TE-honoring component and the Stacks node — a realistic topology for any node exposed via a reverse proxy or load balancer for TLS termination, which is standard operational practice. No authentication, node secret, or privileged role is needed to send the ambiguous request itself.

### Recommendation
Mirror the response-parser's guard in `HttpRequestPreamble::consensus_deserialize`: explicitly detect a `Transfer-Encoding` header on incoming requests and reject the request outright (`CodecError::DeserializeError`) if `Transfer-Encoding` is present at all (since chunked requests are unsupported), or at minimum reject when both `Content-Length` and `Transfer-Encoding` are simultaneously present, matching RFC 7230 §3.3.3 guidance.

### Proof of Concept
Send the following raw bytes to the node's HTTP port through (or simulating) a TE-honoring intermediary:
```
POST /v2/stackerdb/SP000.../foo/chunks HTTP/1.1
Host: node:20443
Content-Type: application/json
Content-Length: 4
Transfer-Encoding: chunked

0

GET /some-other-smuggled-path HTTP/1.1
Host: node:20443

```
The Stacks node's `read_payload` consumes exactly 4 bytes (`Content-Length: 4` → `"0\r\n"`-ish) as the body of the POST and then reinterprets the remaining bytes (starting at the smuggled `GET`) as the next request on the keep-alive connection [4](#0-3) , while any TE-compliant intermediary in front would instead treat the chunked body as ending at the `0\r\n\r\n` sequence and forward/queue the appended `GET` as a *separate* pipelined request to the node — producing exactly the request/response desynchronization described in CVE-2021-38512.

### Citations

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
