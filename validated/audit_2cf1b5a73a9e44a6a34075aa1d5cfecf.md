## Finding

### Title
Unbounded in-memory buffering of git-filter-process smudge content causes DoS - ([File: cmd/gitaly-lfs-smudge/smudge.go])

### Summary
`gitaly-lfs-smudge` implements Git's long-running filter-process protocol used as the LFS smudge filter during checkout/archive operations. When git streams the "clean" (pre-smudge) blob content to this helper, the helper reads pktline packets in a loop and accumulates the **entire content into a single unbounded `bytes.Buffer`** before any size check or LFS-pointer validation is performed.

### Finding Description
In `process()` [1](#0-0) , a `bytes.Buffer` named `content` is created once per filter session and reused across smudge commands. In the `processStateSmudgeContent` state, every pktline payload received from git is appended into this buffer with no cap: [2](#0-1) 

The comment explicitly acknowledges this design tradeoff: *"Ideally, we could avoid slurping the whole content into memory first. But unfortunately, this is impossible... we have to slurp in all of its contents so that we can echo it back to the caller."* Only after the flush packet (end of blob content) is seen does `smudgeOneObject()` call `lfs.DecodeFrom(&content)` [3](#0-2)  to check whether it's a real LFS pointer — meaning a large non-pointer blob is fully buffered in memory regardless of size before any validation occurs.

This filter process is invoked by `git` itself whenever a checkout-like operation processes a path matching a `filter=lfs` gitattribute. One reachable Gitaly RPC path is `GetArchive` with `include_lfs_blobs` set, which configures the smudge filter environment/config and runs `git archive` against attacker-influenced repository content: [4](#0-3) 

An attacker with ordinary push access can:
1. Commit a `.gitattributes` entry marking a path with `filter=lfs`.
2. Commit an arbitrarily large blob (not a real, small LFS pointer file) at that path.
3. Have any user (or automated system) request `GetArchive` with `IncludeLfsBlobs: true` on that ref.

When git invokes the smudge filter for that path, the entire blob content (which is not size-limited by pktline framing — only individual packets are, the aggregate is not) is streamed to `gitaly-lfs-smudge` and buffered wholesale in `content`.

### Impact Explanation
A single ordinary push of a very large blob under a `filter=lfs` path, followed by an archive request that includes LFS blobs, forces the `gitaly-lfs-smudge` subprocess (and transitively the git process it's chained to, and the Gitaly RPC handler blocking on it) to allocate memory proportional to the full blob size. Repeated or parallel requests against such a repository can exhaust host memory, causing process crashes/OOM-kills — a denial of service on the Gitaly node, directly analogous to the reported Fastify unbounded-JSON-body issue (build up entire payload in memory before any size-based rejection).

### Likelihood Explanation
Likelihood is high for any repository/deployment enabling LFS smudging for archive/checkout paths: the attacker only needs ordinary push permissions to add a `.gitattributes` rule and a large blob, no special privileges, no malicious peer/node behavior, and no dependency-only issue — the vulnerable buffering logic lives directly in Gitaly's own `cmd/gitaly-lfs-smudge` binary.

### Recommendation
Impose a maximum size limit before buffering smudge content (e.g., cap `content` growth to the size of a legitimate LFS pointer file plus margin — LFS pointers are always small, well under a few KB) and abort/stream-through once the limit is exceeded instead of unconditionally slurping the whole blob. Alternatively, peek only enough bytes to detect the LFS pointer signature and, if it doesn't match, switch to streaming the remaining bytes through without full buffering.

### Proof of Concept
1. In a repository, add `.gitattributes`: `bigfile filter=lfs diff=lfs merge=lfs -text`.
2. Commit a file `bigfile` containing several GB of arbitrary (non-LFS-pointer) data.
3. Call the `GetArchive` RPC with `IncludeLfsBlobs: true` for a commit including this file (see `handleArchive` wiring the smudge filter into `git archive`: [4](#0-3) ).
4. Observe that `gitaly-lfs-smudge`'s `process()` loop buffers the entire multi-GB file into the `content` `bytes.Buffer` before it is ever checked to be a valid LFS pointer, causing large memory growth proportional to attacker-controlled blob size.

### Citations

**File:** cmd/gitaly-lfs-smudge/smudge.go (L66-82)
```go
func process(ctx context.Context, cfg smudge.Config, to io.Writer, from io.Reader, logger log.Logger) error {
	client, err := gitlab.NewHTTPClient(logger, cfg.Gitlab, cfg.TLS, prometheus.Config{})
	if err != nil {
		return fmt.Errorf("creating HTTP client: %w", err)
	}

	scanner := pktline.NewScanner(from)

	writer := bufio.NewWriter(to)

	buf := make([]byte, pktline.MaxPktSize-4)
	var content bytes.Buffer

	clientSupportsVersion2 := false
	clientSupportsSmudgeCapability := false

	state := processStateAnnounce
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

**File:** internal/gitaly/service/repository/archive.go (L217-240)
```go
	if p.in.GetIncludeLfsBlobs() {
		smudgeCfg := smudge.Config{
			GlRepository: p.in.GetRepository().GetGlRepository(),
			Gitlab:       s.cfg.Gitlab,
			TLS:          s.cfg.TLS,
			DriverType:   smudge.DriverTypeProcess,
		}

		smudgeEnv, err := smudgeCfg.Environment()
		if err != nil {
			return fmt.Errorf("setting up smudge environment: %w", err)
		}

		smudgeGitConfig, err := smudgeCfg.GitConfiguration(s.cfg)
		if err != nil {
			return fmt.Errorf("setting up smudge gitconfig: %w", err)
		}

		env = append(
			env,
			smudgeEnv,
		)
		gitConfig = append(gitConfig, smudgeGitConfig)
	}
```
