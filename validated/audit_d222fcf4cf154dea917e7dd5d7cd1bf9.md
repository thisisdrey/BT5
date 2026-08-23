### Title
Unbounded snapshot stream piped into `tar` allows disk-exhaustion / resource-exhaustion DoS - ([File: internal/gitaly/service/repository/create_repository_from_snapshot.go])

### Summary
`s.untar` (internal/gitaly/service/repository/create_repository_from_snapshot.go:87-121) performs an HTTP GET against an attacker-influenceable `HttpUrl`/`ResolvedAddress` and pipes the raw `rsp.Body` directly into `tar -C path -xvf -` with no `Content-Length` validation, no cap on bytes read, and no independent timeout on body streaming beyond the caller-supplied RPC context. A malicious or attacker-controlled snapshot server can therefore stream an effectively unbounded or extremely slow response to force unbounded disk consumption or to hold the Gitaly worker/`tar` process alive indefinitely.

### Finding Description
`CreateRepositoryFromSnapshot` (create_repository_from_snapshot.go:123-160) creates a new bare repository and calls `s.untar(ctx, path, in)` to populate it from a remote HTTP snapshot. Inside `untar` (lines 87-121):

- The request is made with `client.Do(req)` (line 105); the only network-level protections are `httpTransport`'s `ResponseHeaderTimeout: 30 * time.Second` (line 35) and `TLSHandshakeTimeout`/`DialContext` timeouts (lines 27-35) — these bound how long it takes to receive the *headers*, not the body.
- There is no check of `rsp.ContentLength`, no `io.LimitReader` wrapping `rsp.Body`, and no per-read/idle timeout applied while streaming the body.
- `rsp.Body` is passed straight to `command.New(ctx, ..., command.WithStdin(rsp.Body))` (line 115), which spawns `tar -C path -xvf -` and blocks on `cmd.Wait()` (line 120) until the stream ends or the context is cancelled.
- The only cancellation mechanism is the RPC's own `ctx`, which is attacker-controlled if the caller sets no deadline (gRPC calls without a deadline run until the server enforces one, and there is no server-side timeout enforced here specifically for the snapshot fetch).

The code's own comment acknowledges the trust assumption: "NOTE: The received archive is trusted *a lot*. Before pointing this RPC at endpoints not under our control, it should undergo a lot of hardening." (lines 140-141), confirming this is a known, unmitigated gap rather than a protected path — there is no `storage.ValidateRelativePath`-style limit, no request/body size cap, and no archive-bomb protection anywhere in this flow.

An attacker who can direct `HttpUrl` at a server they control (e.g., via an import/snapshot-restore flow that lets a user supply an external URL) can:
1. Return an arbitrarily large or infinitely-growing (chunked) gzip/tar stream, causing `tar` to keep writing to `path` on Gitaly's storage, exhausting disk space for the storage shard (affecting all repositories on that shard).
2. Return headers promptly (satisfying `ResponseHeaderTimeout`) then trickle bytes slowly forever, keeping the `tar` process, its Gitaly `command.New` wrapper goroutine, and the associated repository-creation transaction alive for the life of the RPC context, tying up worker capacity.

### Impact Explanation
This is a resource-exhaustion / availability issue matching the GitLab bounty "Denial of Service" impact class: an attacker can exhaust storage-shard disk space (impacting other repositories/tenants on the same shard) and/or pin Gitaly worker processes/goroutines for extended periods, degrading `RepositoryService` for other users. It does not grant code execution, data exfiltration, or cross-repository object access — impact is scoped to availability degradation of the storage node.

### Likelihood Explanation
Feasibility depends entirely on whether an unprivileged user can cause Gitaly to call this RPC with an `HttpUrl`/`ResolvedAddress` pointing at a server they control (e.g., through a GitLab feature that lets users trigger snapshot-based repository creation from an external URL). If this RPC is only ever invoked by trusted, privileged GitLab Rails backup/restore code paths with URLs it controls, this is not attacker-reachable and the finding would not apply to an unprivileged threat model. I could not confirm from the available Go RPC-caller code in this repo whether an unprivileged GitLab user can supply `HttpUrl`/`ResolvedAddress` values directly (the calling context lives in GitLab Rails, outside this repo, and the proto comments/callers were not found in the indexed Gitaly code). Assuming the reachability stated in the question (attacker controls the snapshot server and can trigger the RPC), the exploit itself is straightforward, deterministic, and repeatable with a simple `httptest` server.

### Recommendation
- Enforce an explicit, configurable maximum snapshot size (e.g., via `io.LimitReader(rsp.Body, maxSnapshotBytes)`) and reject `Content-Length` values exceeding the limit before extraction.
- Apply a dedicated read/idle timeout on the streaming body (e.g., wrap `rsp.Body` with a deadline-aware reader or use `http.NewRequestWithContext` plus a bounded child context specific to the snapshot fetch, independent of an unbounded caller-supplied RPC deadline).
- Monitor/limit extracted-file counts and expanded size (defend against compressed "tar/zip-bomb" amplification), and fail fast, cleaning up the partially-created repository directory on limit violation.
- Consider requiring this RPC to only be reachable through trusted/internal callers (already partially true) and document/enforce that `HttpUrl` may not be set to endpoints outside an allow-list if it is ever exposed to less-trusted callers.

### Proof of Concept
```go
func TestCreateRepositoryFromSnapshot_UnboundedBody(t *testing.T) {
    // httptest server that streams infinitely without closing.
    srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        w.WriteHeader(http.StatusOK)
        flusher, _ := w.(http.Flusher)
        buf := make([]byte, 1<<20) // 1MB chunks of zeros (or a crafted gzip bomb)
        for {
            if _, err := w.Write(buf); err != nil {
                return
            }
            if flusher != nil {
                flusher.Flush()
            }
        }
    }))
    defer srv.Close()

    ctx := context.Background() // no deadline set by caller
    req := &gitalypb.CreateRepositoryFromSnapshotRequest{
        Repository: repo,
        HttpUrl:    srv.URL,
    }

    // Call CreateRepositoryFromSnapshot in a goroutine and observe:
    // - disk usage under `path` growing unbounded over time (df/du polling), and/or
    // - the RPC / tar process still running well past any expected extraction time,
    //   only stoppable by external context cancellation.
    _, err := client.CreateRepositoryFromSnapshot(ctx, req)
    // Expect: without a caller-imposed deadline, this call and its child `tar`
    // process never terminate on their own and disk usage grows without bound.
}
```
Expected assertion: disk usage under the target storage path increases without bound while the RPC is in flight, and the `tar` process/goroutine remains alive until either disk is exhausted or the caller externally cancels the context — demonstrating no server-enforced size/time cap exists in `untar`. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L25-36)
```go
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
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L105-120)
```go
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

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L140-142)
```go
		// NOTE: The received archive is trusted *a lot*. Before pointing this RPC
		// at endpoints not under our control, it should undergo a lot of hardening.
		if err := s.untar(ctx, path, in); err != nil {
```
