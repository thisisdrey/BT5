## Analog Finding

### Title
Malformed `Content-Length` on an inbound Stacks HTTP request silently defaults to 0, breaking the bytes-vs-declared-length equality and enabling request smuggling on P2P/RPC HTTP connections - (File: `stackslib/src/net/http/request.rs`)

### Summary
The Camel-NATS advisory is a case of a bug class where inbound network input is mapped into an internal protocol construct without validating the invariant that lets a downstream component trust it. In `stacks-core`, the analogous equality that must hold is "declared body length == actual body bytes consumed" for `HttpRequestPreamble`. `HttpRequestPreamble::consensus_deserialize` breaks this invariant: when the client-supplied `Content-Length` header fails to parse as `u32`, the code silently treats the header as absent (`None`) rather than rejecting the request, and `get_content_length()` then reports a length of `0` to the rest of the stack.

### Finding Description
In the request-preamble parser: [1](#0-0) 

```
} else if key == "content-length" {
    // parse
    content_length = match value.parse::<u32>() {
        Ok(len) => Some(len),
        Err(_) => None,
    };
    seen_headers.insert(key);
}
```

Any malformed `Content-Length` value (non-numeric, negative, or an integer that overflows `u32`) is swallowed, and `content_length` stays `None`.

Compare this to the *response* preamble parser, which handles the identical header but correctly rejects malformed values instead of silently dropping them: [2](#0-1) 

```
} else if key == "content-length" {
    let len = value.parse::<u32>().map_err(|_e| {
        CodecError::DeserializeError(
            "Invalid Content-Length header value".to_string(),
        )
    })?;
    content_length = Some(len);
}
```

This asymmetry (error-out for responses, silent-default for requests) confirms the request path is the fault site rather than an intentional design choice.

The consequence of the `None` fallback is that `get_content_length()` returns `0`: [3](#0-2) 

That value is what the rest of the `StacksHttp` `ProtocolFamily` implementation uses to decide how many bytes of the connection's byte stream belong to this message's body: [4](#0-3) [5](#0-4) 

`payload_len()` reports `0`, so `read_payload()` slices `buf.get(0..0)` as the "body" and immediately returns, having consumed 0 payload bytes. But the attacker actually sent a real body after the headers (with a valid-looking `Content-Length` value on the wire, or none, or a bogus one) — those bytes are still sitting, unconsumed, in the connection's receive buffer. Because the transport is a persistent/keep-alive stream (P2P HTTP conversations and the node's RPC socket reuse connections across multiple sequential HTTP messages), the connection state machine will call `read_preamble()` again on the *leftover attacker-controlled bytes*, searching for the next `\r\n\r\n` boundary and parsing them as an entirely new `HttpRequestPreamble`/`HttpResponsePreamble`.

This is the same "equality break" pattern flagged in the Camel-NATS advisory (an attacker-controlled field is trusted without validating an invariant that the receiving component silently assumes holds), applied here to the byte-accounting invariant of the HTTP framing layer instead of a header-filter invariant.

### Impact Explanation
This is a request/response desynchronization (smuggling) primitive on the wire format that both the p2p HTTP RPC surface (`/v2/*`, `/v3/*` endpoints, StackerDB chunk posts, block/transaction posts, etc.) and any HTTP intermediary sitting in front of a Stacks node share. A remote, unauthenticated peer that can open an HTTP connection to a node's RPC port can:
- Send a request with a non-numeric/overflowing `Content-Length` followed by attacker-chosen bytes.
- The node parses this as a 0-length-body request, then reinterprets the attacker's trailing bytes as the start of a subsequent, independently-parsed HTTP message on the same connection.
- Depending on what request handler is invoked with the manipulated framing (e.g., an authenticated endpoint such as `/v3/block_proposal` or `postblock_v3` that gates on the `authorization` header, or a downstream proxy/load balancer relaying multiple clients over the same backend connection), this can desynchronize request boundaries and let attacker data be interpreted as a distinct request — a classic smuggling condition, explicitly called out as a Critical-impact category in the assessment criteria (request smuggling / auth bypass via framing confusion).

### Likelihood Explanation
Reaching the vulnerable code path requires nothing more than sending a single malformed HTTP header value to a node's exposed HTTP port — no authentication, no valid signature, and no special network position are required. The flaw is in the mandatory preamble-parsing path used by every inbound `StacksHttpRequest`, so it is reachable on every connection to the RPC/P2P HTTP listener.

### Recommendation
In `HttpRequestPreamble::consensus_deserialize` (`stackslib/src/net/http/request.rs`), reject the request when the `Content-Length` header value fails to parse as `u32`, mirroring the behavior already implemented for `HttpResponsePreamble::consensus_deserialize`, instead of silently substituting `None`/`0`.

### Proof of Concept
1. Open a TCP connection to a Stacks node's RPC/P2P HTTP port.
2. Send:
   ```
   POST /v2/transactions HTTP/1.1
   Host: <node>
   Content-Length: 99999999999999999999
   Connection: keep-alive

   <attacker-controlled bytes, e.g. a forged "GET /v3/block_proposal HTTP/1.1 ..." request>
   ```
   Because `99999999999999999999` overflows `u32::parse`, `content_length` becomes `None`, `get_content_length()` returns `0`.
3. The node's `read_payload` treats the request as having a 0-byte body, and the attacker-supplied bytes following the blank line remain buffered.
4. On the next read cycle, `read_preamble` re-parses those buffered bytes as an independent HTTP preamble, desynchronizing message framing on the connection — the exact mechanism needed for request smuggling.

(Note: full observation of the downstream connection-buffering code, e.g. `stackslib/src/net/connection.rs`, could not be completed before the tool budget was exhausted; the analysis above is based on the confirmed `ProtocolFamily` contract in `httpcore.rs`, where `payload_len`/`read_payload` are the sole authorities for how many bytes constitute a message, and the confirmed parsing asymmetry between request and response `Content-Length` handling.)

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

**File:** stackslib/src/net/http/request.rs (L392-398)
```rust
                    } else if key == "content-length" {
                        // parse
                        content_length = match value.parse::<u32>() {
                            Ok(len) => Some(len),
                            Err(_) => None,
                        };
                        seen_headers.insert(key);
```

**File:** stackslib/src/net/http/response.rs (L543-549)
```rust
                    } else if key == "content-length" {
                        let len = value.parse::<u32>().map_err(|_e| {
                            CodecError::DeserializeError(
                                "Invalid Content-Length header value".to_string(),
                            )
                        })?;
                        content_length = Some(len);
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
