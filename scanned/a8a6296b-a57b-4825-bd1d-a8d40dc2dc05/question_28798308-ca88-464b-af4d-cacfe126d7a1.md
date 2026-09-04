[File: stackslib/src/net/http/request.rs -> Scope: Critical] Can a remote HTTP client set both `Content-Length` and `Transfer-Encoding: chunked` in the same request so that `HttpRequestPreamble::consensus_deserialize` (stackslib/src/net/http/request.rs:302-441) accepts `content_length: Some(n)` from the `content-length` branch (line 392-398) while never validating against a `transfer-encoding` header at all (no such branch exists in the header loop at lines 355-422), breaking AUTHENTICATION/BOUNDS equality declared framing mechanism == actual framing mechanism used to read the body, causing scoped impact: request smuggling if a length-based and chunk-based reader disagree on where the body ends across a proxy/reverse layer? Proof idea: craft `Content-Length: 4
Transfer-Encoding: chunked

1
A
0

` and assert the server consistently rejects or picks exactly one framing mode rather than reading a body length inconsistent with what a chunked-aware peer would compute.
