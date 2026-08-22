# Q4034: reportPrometheusMetrics: A version-downgrade to the v1 token path with weaker checks

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `reportPrometheusMetrics` in `internal/grpc/middleware/requestinfohandler/requestinfohandler.go` by supplying a version-downgrade to the v1 token path with weaker checks, so that a request is authenticated only with a fresh valid token, secrets and absolute paths never survive into a readable error, and malformed parser input cannot crash or hang the handler is violated — specifically the weaker v1 path cannot be forced by a client — leading to transport-auth bypass?

## Target
- File/function: `internal/grpc/middleware/requestinfohandler/requestinfohandler.go` -> `reportPrometheusMetrics`
- Entrypoint: the gRPC auth interceptor, request-info/limit/panic middleware, and shared parsers on every RPC
- Attacker controls: the auth token header, request metadata, git-object/attributes/LFS-pointer bytes, and error-triggering fields
- Exploit idea: Supply a version-downgrade to the v1 token path with weaker checks; if `reportPrometheusMetrics` uses it without enforcing that the weaker v1 path cannot be forced by a client, the request escapes the intended boundary.
- Invariant to test: a request is authenticated only with a fresh valid token, secrets and absolute paths never survive into a readable error, and malformed parser input cannot crash or hang the handler.
- Expected Immunefi impact: (GitLab HackerOne class) Transport-auth bypass (HMAC replay/downgrade), secret/metadata leak through error surfaces, or DoS/panic of a request handler.
- Fast validation: Test auth interceptor version selection.
