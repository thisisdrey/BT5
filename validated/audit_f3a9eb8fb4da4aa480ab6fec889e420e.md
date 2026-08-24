### Title
Uncontrolled Resource Consumption via Unbounded HTTP Response Body in `CreateRepositoryFromSnapshot` - (File: `internal/gitaly/service/repository/create_repository_from_snapshot.go`)

### Summary
The `CreateRepositoryFromSnapshot` RPC fetches an archive from a caller-supplied `http_url` and streams the raw HTTP response body directly into `tar -xf -` without ever bounding the number of bytes read. This mirrors the Tendermint CVE-2019-25072 pattern: a remote/attacker-influenced HTTP response with no size limiting can force the receiving process to consume unbounded system resources.

### Finding Description
`untar()` builds an HTTP GET request against `in.GetHttpUrl()` (a value fully controlled by the RPC caller) and, once a 2xx/3xx-range status is returned, pipes `rsp.Body` straight into the stdin of a spawned `tar -xf -` process with no `io.LimitReader`, no `Content-Length` check, and no maximum extracted-size accounting: [1](#0-0) 

The HTTP client (`httpClient` / `newResolvedHTTPClient`) only limits *connection setup* timing (dial timeout, TLS handshake timeout, response-header timeout) via `httpTransport`: [2](#0-1) 

None of these settings cap the total time to read the body or the total number of bytes transferred. A slow or malicious HTTP endpoint can therefore:
- stream an effectively unbounded (or extremely large) response body that `tar` will keep extracting to disk, exhausting disk space and inodes, or
- stream data at an extremely low rate ("slow-loris"-style) to hold the connection, the `tar` subprocess, and the associated goroutine/file descriptors open indefinitely, since `ResponseHeaderTimeout` only bounds the time to receive headers, not the body.

The code comment in `CreateRepositoryFromSnapshot` itself acknowledges the received archive is "trusted *a lot*" and that hardening is required "before pointing this RPC at endpoints not under our control": [3](#0-2) 

This is a direct root cause: the RPC accepts a caller-provided URL (potentially pointing to any external or malicious HTTP server) and imposes no ceiling on response size or overall transfer duration before extracting it as a repository snapshot.

### Impact Explanation
An ordinary caller of this RPC (e.g., via project/repository import flows that invoke `CreateRepositoryFromSnapshot` with a `http_url`) can point Gitaly at a server they control that returns an oversized or slow-drip response body. Because there is no size cap, this can exhaust Gitaly storage disk space (via `tar` writing extracted content) or hold connections/goroutines/subprocesses open, denying service to legitimate RPCs on the same Gitaly node. This matches the "DoS of a handler" acceptance criterion.

### Likelihood Explanation
Likelihood is moderate-to-high: the RPC is reachable with attacker-controlled input (`http_url`, `http_auth`, optionally `resolved_address`) and requires no privileged access beyond whatever normally invokes repository-from-snapshot creation (e.g., project import). No malicious peer/node collusion or leaked token is needed — the caller supplies the URL directly as part of the RPC request, and the "malicious server" role is trivially satisfiable by the same caller (self-hosted HTTP endpoint).

### Recommendation
- Wrap `rsp.Body` in an `io.LimitReader` (or equivalent `http.MaxBytesReader`-style guard) sized to a configured maximum snapshot size before passing it to the `tar` subprocess's stdin, and treat truncation as an error.
- Enforce an overall deadline for the full body transfer (not just headers) using `context.WithTimeout` around the read loop, since `ResponseHeaderTimeout` does not bound body read duration.
- Consider tracking bytes already extracted and aborting (killing the `tar` process) once a configurable ceiling is exceeded, to prevent disk exhaustion from decompression/extraction.
- Update the in-code comment/TODO into an actual tracked hardening requirement, since it explicitly flags this gap as unresolved.

### Proof of Concept
1. Stand up an HTTP server that responds to a GET request with HTTP 200 and streams an effectively infinite (or multi-hundred-GB) byte stream (optionally as a slow trickle to also hold the connection open).
2. Invoke `CreateRepositoryFromSnapshot` against a Gitaly node with `http_url` pointing at that server.
3. Observe that `untar()` keeps reading/extracting via `tar -xf -` indefinitely, consuming disk space and/or holding the connection, subprocess, and goroutine open well beyond any header-level timeout, since no limit exists at [4](#0-3)  to cap body size or total transfer time.

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L20-46)
```go
// httpTransport defines a http.Transport with values that are more restrictive
// than for http.DefaultTransport.
//
// They define shorter TLS Handshake, and more aggressive connection closing
// to prevent the connection hanging and reduce FD usage.
var httpTransport = &http.Transport{
	Proxy: http.ProxyFromEnvironment,
	DialContext: (&net.Dialer{
		Timeout:   30 * time.Second,
		KeepAlive: 10 * time.Second,
	}).DialContext,
	MaxIdleConns:          2,
	IdleConnTimeout:       30 * time.Second,
	TLSHandshakeTimeout:   10 * time.Second,
	ExpectContinueTimeout: 10 * time.Second,
	ResponseHeaderTimeout: 30 * time.Second,
}

// httpClient defines a http.Client that uses the specialized httpTransport
// (above). It also disables following redirects, as we don't expect this to be
// required for this RPC.
var httpClient = &http.Client{
	Transport: correlation.NewInstrumentedRoundTripper(otelhttp.NewTransport(httpTransport)),
	CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	},
}
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L87-120)
```go
func (s *server) untar(ctx context.Context, path string, in *gitalypb.CreateRepositoryFromSnapshotRequest) error {
	req, err := http.NewRequestWithContext(ctx, "GET", in.GetHttpUrl(), nil)
	if err != nil {
		return structerr.NewInvalidArgument("Bad HTTP URL: %w", err)
	}

	client := httpClient
	if resolvedAddress := in.GetResolvedAddress(); resolvedAddress != "" {
		client, err = newResolvedHTTPClient(in.GetHttpUrl(), resolvedAddress)
		if err != nil {
			return structerr.NewInvalidArgument("creating resolved HTTP client: %w", err)
		}
	}

	if in.GetHttpAuth() != "" {
		req.Header.Set("Authorization", in.GetHttpAuth())
	}

	rsp, err := client.Do(req)
	if err != nil {
		return structerr.NewInternal("HTTP request failed: %w", err)
	}
	defer rsp.Body.Close()

	if rsp.StatusCode < http.StatusOK || rsp.StatusCode >= http.StatusMultipleChoices {
		return structerr.NewInternal("HTTP server: %s", rsp.Status)
	}

	cmd, err := command.New(ctx, s.logger, []string{"tar", "-C", path, "-xvf", "-"}, command.WithStdin(rsp.Body))
	if err != nil {
		return err
	}

	return cmd.Wait()
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L135-144)
```go
		// The archive contains a partial git repository, missing a config file and
		// other important items. Initializing a new bare one and extracting the
		// archive on top of it ensures the created git repository has everything
		// it needs (especially, the config file and hooks directory).
		//
		// NOTE: The received archive is trusted *a lot*. Before pointing this RPC
		// at endpoints not under our control, it should undergo a lot of hardening.
		if err := s.untar(ctx, path, in); err != nil {
			return structerr.NewInternal("extracting snapshot: %w", err)
		}
```
