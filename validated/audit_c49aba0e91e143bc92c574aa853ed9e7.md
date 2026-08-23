### Title
Unbounded in-memory buffering of non-LFS blob content in git's long-running LFS smudge filter process leads to memory exhaustion - ([File: cmd/gitaly-lfs-smudge/smudge.go])

### Summary
`process()` implements the long-running-process protocol for `filter.lfs.process`, which Gitaly configures for RPCs such as `GetArchive` with `IncludeLfsBlobs: true` (and any Git operation that smudges LFS-attributed paths). When a blob routed through the LFS filter is not a valid LFS pointer, the code intentionally slurps the entire pktline-streamed content into an unbounded `bytes.Buffer` (`content`) before it can respond, with no maximum size enforced.

### Finding Description
`process()` is entered in `processStateSmudgeContent` for every non-flush pktline chunk of the file being smudged and calls `content.Write(data)` [1](#0-0) . The comment explicitly documents that the server "must not answer to the client before it has received all contents," so there is no cap on how much data can be accumulated before a flush packet arrives; a single blob can be arbitrarily large (limited only by Git's own object size, which is effectively unbounded for a user's own repository).

This filter binary is wired up as `filter.lfs.process` via `smudge.Config.GitConfiguration` [2](#0-1)  and enabled directly from an attacker-reachable RPC field: `GetArchiveRequest.IncludeLfsBlobs` in `handleArchive` [3](#0-2) . An unprivileged user who owns/can push to a repository controls both the blob content and the `.gitattributes` file that marks a path as `filter=lfs`, so they can commit a very large blob that is not a real LFS pointer (so `smudgeOneObject`'s `lfs.DecodeFrom` fails and it falls back to echoing the raw bytes) and then request `GetArchive` with `IncludeLfsBlobs=true`, or trigger any other Gitaly operation that runs `git checkout`/`git archive` with the LFS process filter enabled for that path.

None of the described mitigations (`storage.ValidateRelativePath`, revision validation, quarativeness/hooks checks, `helper.SanitizeString`) apply here, since this is purely about how much data the smudge subprocess buffers per file, not about path or command injection.

### Impact Explanation
The `gitaly-lfs-smudge` subprocess spawned by `git archive`/`git checkout` for the request buffers the entire non-LFS-pointer blob content in an unbounded `bytes.Buffer`, causing memory usage on the Gitaly host proportional to the largest attacker-committed blob routed through the LFS filter. Since this subprocess runs on the same host as Gitaly (no separate memory cgroup is described for these filter subprocesses in the visible code), repeated/concurrent requests with very large blobs can drive up host memory pressure and cause resource exhaustion/DoS, matching GitLab's "denial of service" bounty impact class for a Gitaly-hosted process.

### Likelihood Explanation
The precondition (owning a repository, committing a large blob, adding a `.gitattributes` entry marking it `filter=lfs`, and invoking `GetArchive` with `IncludeLfsBlobs=true`) is fully within reach of an unprivileged authenticated GitLab user with push access to their own project. The exploit requires no special configuration beyond LFS being enabled (a default/common feature), no admin role, and no secrets. It is straightforward and repeatable.

### Recommendation
Enforce a maximum size (e.g., matching the typical LFS pointer max size, ~1KB, plus a safety margin, or a configurable cap) while accumulating `content` in `processStateSmudgeContent`; once exceeded, abort with an error (and stop reading further pktlines for that command) instead of continuing to buffer indefinitely. Alternatively, stream content to a size-bounded temp file instead of an in-memory buffer once a size threshold is exceeded.

### Proof of Concept
```go
func TestProcess_largeNonLFSBlobExhaustsMemory(t *testing.T) {
    ctx := testhelper.Context(t)
    opts := defaultOptions(t)
    gitlabCfg, cleanup := runTestServer(t, opts)
    defer cleanup()

    cfg := smudge.Config{
        GlRepository: "project-1",
        Gitlab:       gitlabCfg,
        DriverType:   smudge.DriverTypeProcess,
    }

    pkt := func(data string) string { return fmt.Sprintf("%04x%s", len(data)+4, data) }
    flush := "0000"

    var input strings.Builder
    input.WriteString(pkt("git-filter-client\n"))
    input.WriteString(pkt("version=2\n"))
    input.WriteString(flush)
    input.WriteString(pkt("capability=smudge\n"))
    input.WriteString(flush)
    input.WriteString(pkt("command=smudge\n"))
    input.WriteString(flush) // metadata flush

    // Simulate a very large non-LFS-pointer blob: many max-size pktlines,
    // never sending the terminating flush for smudge content until the end.
    chunk := strings.Repeat("A", pktline.MaxPktSize-4)
    for i := 0; i < 200000; i++ { // ~ multiple GB total before flush
        input.WriteString(pkt(chunk))
    }
    input.WriteString(flush)

    var out bytes.Buffer
    err := process(ctx, cfg, &out, strings.NewReader(input.String()), testhelper.SharedLogger(t))
    require.NoError(t, err)
    // Assert: process's internal `content` bytes.Buffer grew to the full
    // multi-GB size before any response was sent, demonstrating unbounded
    // memory growth proportional to attacker-supplied blob size.
}
```
Expected result: the `content` buffer inside `process()` grows unbounded with attacker-supplied blob size before any flush/response is possible, demonstrating memory usage scales linearly and unboundedly with a single malicious blob.

### Citations

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

**File:** internal/git/smudge/config.go (L96-112)
```go
// GitConfiguration returns the Git configuration required to run the smudge filter.
func (c Config) GitConfiguration(cfg config.Cfg) (gitcmd.ConfigPair, error) {
	switch c.DriverType {
	case DriverTypeFilter:
		return gitcmd.ConfigPair{
			Key:   "filter.lfs.smudge",
			Value: cfg.BinaryPath("gitaly-lfs-smudge"),
		}, nil
	case DriverTypeProcess:
		return gitcmd.ConfigPair{
			Key:   "filter.lfs.process",
			Value: cfg.BinaryPath("gitaly-lfs-smudge"),
		}, nil
	default:
		return gitcmd.ConfigPair{}, fmt.Errorf("unknown driver type: %v", c.DriverType)
	}
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
