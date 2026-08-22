# Q3426: injectTags: A malformed git object fed to catfile.parser causing a panic

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `injectTags` in `internal/grpc/middleware/requestinfohandler/requestinfohandler.go` by supplying a malformed git object fed to catfile.parser causing a panic, so that a request is authenticated only with a fresh valid token, secrets and absolute paths never survive into a readable error, and malformed parser input cannot crash or hang the handler is violated — specifically the parser returns errors, never panics — leading to transport-auth bypass?

## Target
- File/function: `internal/grpc/middleware/requestinfohandler/requestinfohandler.go` -> `injectTags`
- Entrypoint: the gRPC auth interceptor, request-info/limit/panic middleware, and shared parsers on every RPC
- Attacker controls: the auth token header, request metadata, git-object/attributes/LFS-pointer bytes, and error-triggering fields
- Exploit idea: Supply a malformed git object fed to catfile.parser causing a panic; if `injectTags` uses it without enforcing that the parser returns errors, never panics, the request escapes the intended boundary.
- Invariant to test: a request is authenticated only with a fresh valid token, secrets and absolute paths never survive into a readable error, and malformed parser input cannot crash or hang the handler.
- Expected Immunefi impact: (GitLab HackerOne class) Transport-auth bypass (HMAC replay/downgrade), secret/metadata leak through error surfaces, or DoS/panic of a request handler.
- Fast validation: Fuzz catfile parser asserting no panic.
