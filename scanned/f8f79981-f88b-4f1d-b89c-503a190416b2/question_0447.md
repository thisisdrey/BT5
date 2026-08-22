# Q0447: toPanicError: An absolute storage path leaking through structerr/requestinfohandler

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `toPanicError` in `internal/grpc/middleware/panichandler/panic_handler.go` by supplying an absolute storage path leaking through structerr/requestinfohandler, so that a request is authenticated only with a fresh valid token, secrets and absolute paths never survive into a readable error, and malformed parser input cannot crash or hang the handler is violated — specifically internal paths never reach a client-readable error — leading to transport-auth bypass?

## Target
- File/function: `internal/grpc/middleware/panichandler/panic_handler.go` -> `toPanicError`
- Entrypoint: the gRPC auth interceptor, request-info/limit/panic middleware, and shared parsers on every RPC
- Attacker controls: the auth token header, request metadata, git-object/attributes/LFS-pointer bytes, and error-triggering fields
- Exploit idea: Supply an absolute storage path leaking through structerr/requestinfohandler; if `toPanicError` uses it without enforcing that internal paths never reach a client-readable error, the request escapes the intended boundary.
- Invariant to test: a request is authenticated only with a fresh valid token, secrets and absolute paths never survive into a readable error, and malformed parser input cannot crash or hang the handler.
- Expected Immunefi impact: (GitLab HackerOne class) Transport-auth bypass (HMAC replay/downgrade), secret/metadata leak through error surfaces, or DoS/panic of a request handler.
- Fast validation: Test error field extraction for path leakage.
