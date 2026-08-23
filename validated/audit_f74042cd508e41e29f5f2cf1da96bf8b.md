This confirms no size cap exists anywhere in the smudge configuration or pktline handling that would bound `content.Write(data)` accumulation.

### Title
Unbounded in-memory buffering of attacker-controlled blob content in git long-running filter process causes memory exhaustion - ([File: cmd/gitaly-lfs-smudge/smudge.go])

### Summary
`process()` in `smudge.go` implements Git's long-running filter-process protocol for LFS smudging. When the client (git) streams a blob's content in the `processStateSmudgeContent` state, every pktline payload is appended to an in-memory `bytes.Buffer` (`content.Write(data)`) with no size limit, and the buffer is only flushed once a flush packet terminates the blob's content stream.

### Finding Description
An unprivileged user who owns/controls a repository can commit a `.gitattributes` file assigning `filter=lfs` to a path, then commit a large ordinary (non-LFS-pointer) blob at that path. When any RPC that performs a checkout/archive of that ref runs (e.g. `GetArchive`, `UserCheckout`-style flows), `git` invokes `gitaly-lfs-smudge` as a long-running filter process for that blob. In `process()`, the code enters `processStateSmudgeContent` at [1](#0-0) , and for every pktline chunk that is not a flush packet, it does: [2](#0-1) 
which appends the entire blob to `content` with no cap. Only after the *entire* blob has been received (flush packet seen) is `smudgeOneObject` called, which tries `lfs.DecodeFrom` to detect a real LFS pointer at [3](#0-2) ; if decoding fails (i.e., it's not a pointer, just attacker's large blob), the code falls back to echoing the whole buffered content back verbatim. There is no size limit configured anywhere in `smudge.Config` (`internal/git/smudge/config.go`) nor in the pktline scanner setup (`internal/git/pktline/pktline.go`) that would cap the aggregate size of `content`; the only cap (`MaxPktSize = 65520`, [4](#0-3) ) bounds a single packet, not the accumulated buffer across many packets. The comment at lines 263-268 explicitly acknowledges this is unavoidable "impossible" to avoid slurping the full content into memory for non-pointer blobs, confirming the root cause is by design and unmitigated by any size bound.

### Impact Explanation
An attacker-controlled blob of several GB assigned `filter=lfs` in `.gitattributes` will cause the `gitaly-lfs-smudge` subprocess (spawned per blob, one per concurrent smudge invocation during checkout/archive) to allocate memory proportional to the blob size, with no upper bound. Multiple such blobs or concurrent RPCs (e.g., concurrent `GetArchive`/`GetSnapshot`/checkout operations) compound the effect, since each spawns its own `gitaly-lfs-smudge` process. This can OOM-kill the smudge process, the invoking `git` process, and potentially destabilize the Gitaly node hosting the RPC, matching a resource-exhaustion/DoS impact class.

### Likelihood Explanation
This requires only that the attacker control content of a repository they own/push to (setting `.gitattributes` `filter=lfs` and pushing a large non-pointer blob) and then trigger any RPC that checks out or archives that ref — an action normal unprivileged GitLab users can perform. No special privileges, secrets, or non-default configuration are needed, since LFS smudging via `filter.lfs.process`/`filter.lfs.smudge` is default Gitaly behavior for LFS-enabled repositories. It is fully repeatable and requires no race conditions.

### Recommendation
Impose a configurable maximum size on the accumulated `content` buffer in `processStateSmudgeContent` (e.g., cap total bytes written before erroring out or switching to a temp-file-backed buffer/spooled buffer instead of an unbounded `bytes.Buffer`), and/or perform pointer-format detection incrementally on only the first pktline chunk(s) (LFS pointers are small, well under 1 pktline) before deciding whether to buffer the rest, aborting or streaming through unmodified once it's clear the content is not an LFS pointer.

### Proof of Concept
1. In a repository, add `.gitattributes`: `bigfile filter=lfs diff=lfs merge=lfs -text`.
2. Commit a several-GB non-LFS-pointer file named `bigfile`.
3. Trigger `GetArchive` (or any checkout path) for that ref via gRPC.
4. Observe `gitaly-lfs-smudge` process RSS growing linearly with the size of `bigfile`, unbounded, confirmed by the code path at `cmd/gitaly-lfs-smudge/smudge.go:263-271` where `content.Write(data)` is called for every pktline chunk with no size check, and no `smudge.Config` field or pktline constant enforcing a total buffer size limit.

### Citations

**File:** cmd/gitaly-lfs-smudge/smudge.go (L193-211)
```go
		case processStateSmudgeContent:
			// When we receive a flush packet we know that the client is done sending us
			// the clean data.
			if pktline.IsFlush(line) {
				smudgedReader, err := smudgeOneObject(ctx, cfg, client, &content, logger)
				if err != nil {
					logger.WithError(err).Error("failed smudging LFS pointer")

					if _, err := pktline.WriteString(writer, "status=error\n"); err != nil {
						return fmt.Errorf("reporting failure: %w", err)
					}

					if err := pktline.WriteFlush(writer); err != nil {
						return fmt.Errorf("flushing error: %w", err)
					}

					state = processStateCommand
					break
				}
```

**File:** cmd/gitaly-lfs-smudge/smudge.go (L263-271)
```go
			// Write the pktline into our buffer. Ideally, we could avoid slurping the
			// whole content into memory first. But unfortunately, this is impossible in
			// the context of long-running processes: the server-side _must not_ answer
			// to the client before it has received all contents. And in the case we got
			// a non-LFS-pointer as input, this means we have to slurp in all of its
			// contents so that we can echo it back to the caller.
			if _, err := content.Write(data); err != nil {
				return fmt.Errorf("could not write clean data: %w", err)
			}
```

**File:** cmd/gitaly-lfs-smudge/smudge.go (L290-295)
```go
func smudgeOneObject(ctx context.Context, cfg smudge.Config, gitlabClient *gitlab.HTTPClient, from io.Reader, logger log.Logger) (io.ReadCloser, error) {
	ptr, contents, err := lfs.DecodeFrom(from)
	if err != nil {
		// This isn't a valid LFS pointer. Just copy the existing pointer data.
		return io.NopCloser(contents), nil
	}
```

**File:** internal/git/pktline/pktline.go (L20-24)
```go
	// MaxPktSize is the maximum size of content of a Git pktline side-band-64k
	// packet, including size of length and band number
	// https://gitlab.com/gitlab-org/git/-/blob/v2.30.0/pkt-line.h#L216
	MaxPktSize = 65520
)
```
