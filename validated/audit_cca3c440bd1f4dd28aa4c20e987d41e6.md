Based on my research, I've confirmed the strongest analog. `DeleteRefs`'s `refsToRemove` function calls `repo.GetReferences(ctx)` (backed by `gitcmd.GetReferences`) which runs `git-for-each-ref` **without any `--count` limit** and accumulates every single matching reference into an in-memory Go slice before any deletion work begins, whenever the RPC is invoked with `ExceptWithPrefix` instead of an explicit `Refs` list.

### Title
Unbounded reference enumeration in `DeleteRefs` (`ExceptWithPrefix` mode) causes RPC-handler resource exhaustion / DoS - (File: `internal/gitaly/service/ref/delete_refs.go`)

### Summary
The `massUpdatePools` report describes a class of bug where a function unconditionally iterates over an entire, attacker/user-growable collection with no pagination or cap, causing the operation (and dependent operations) to fail or become prohibitively expensive once the collection grows large enough. The Gitaly analog is `DeleteRefs`, which — when called with `ExceptWithPrefix` — loads **every** reference in the repository into memory via an uncapped `git-for-each-ref` invocation before it can compute the set of refs to delete.

### Finding Description
`DeleteRefs` supports two mutually exclusive selection modes: an explicit `Refs` list, or `ExceptWithPrefix`, which deletes "all refs except those with a given prefix." For the latter mode, `refsToRemove` calls: [1](#0-0) 

`repo.GetReferences` delegates to `gitcmd.GetReferences`, which builds a `for-each-ref` command and only appends a `--count` flag when `cfg.Limit > 0`; `refsToRemove` never sets `Limit`, so the call is unbounded: [2](#0-1) [3](#0-2) 

Unlike `ListRefs` (which is a genuinely paginated, streaming RPC with a bounded per-page `opts.Limit` derived from `PaginationParameter`) [4](#0-3) , or `FindAllTags` (which enforces `opts.Limit` inside its result loop) [5](#0-4) , `DeleteRefs` is a **unary** RPC that must materialize the complete reference set into a single Go slice (`existingRefs`) and then build a second slice (`refs`) before any deletion or voting logic runs. There is no limit, streaming, or chunking — the entire scan-and-filter step must complete, in memory, in one RPC call, exactly analogous to `massUpdatePools`'s `for (uint256 pid = 0; pid < length; ++pid)` loop that must fully complete before the surrounding transaction can proceed.

### Impact Explanation
Any ordinary user with write/mutator access to a repository (the same access level required to invoke `DeleteRefs` at all) can grow the repository's reference count arbitrarily (e.g. via many small pushes creating branches/tags, or via `UpdateReferences`), and thereby increase the cost of any future `DeleteRefs(ExceptWithPrefix=...)` call originating from that repository (used by GitLab, for example, to prune refs after operations like fork deletion or environment cleanup). Once the reference count is large enough, the RPC's CPU time, memory allocation, and gRPC request handling duration grow linearly (or worse, given `hasAnyPrefix`'s O(n·prefixes) filtering) with the number of refs, which can lead to request timeouts, elevated memory pressure on the Gitaly node, and denial of service for that RPC (and potentially for co-located requests sharing worker resources) — without requiring any privileged actor, leaked token, or malicious peer.

### Likelihood Explanation
Likelihood is moderate: it requires a repository to first accumulate an unusually large number of references (attacker-controlled, since ordinary push/branch/tag creation is unprivileged) and then a caller (which could be triggered indirectly through normal GitLab workflows that invoke `DeleteRefs` with `ExceptWithPrefix`) to hit that repository. No authentication bypass or malicious peer is needed — only sustained, unprivileged ref creation followed by a normal `DeleteRefs` invocation.

### Recommendation
Bound the reference enumeration used by `refsToRemove` when `ExceptWithPrefix` is set: either (a) stream references via `RepositoryExecutor`/`for-each-ref` and filter/delete incrementally instead of materializing the full slice, (b) impose a configurable maximum reference count for this code path and fail fast with an explicit `ResourceExhausted` error above that threshold, or (c) convert the `ExceptWithPrefix` deletion path to a streaming/paginated implementation consistent with `ListRefs`.

### Proof of Concept
1. Using unprivileged push/`UpdateReferences` access, create a very large number of references (e.g. hundreds of thousands of `refs/heads/*` or `refs/tags/*`) in a repository.
2. Invoke `DeleteRefs` with `ExceptWithPrefix` set (not `Refs`).
3. Observe that `refsToRemove` (`internal/gitaly/service/ref/delete_refs.go:139-151`) must fully execute an unbounded `git-for-each-ref` and build the complete `existingRefs`/`refs` slices in memory before any further processing, causing the RPC's latency and memory usage to scale with total reference count, up to gRPC deadline exhaustion or OOM pressure on the Gitaly node.

### Citations

**File:** internal/gitaly/service/ref/delete_refs.go (L139-151)
```go
	existingRefs, err := repo.GetReferences(ctx)
	if err != nil {
		return nil, err
	}

	var refs []git.ReferenceName
	for _, existingRef := range existingRefs {
		if hasAnyPrefix(existingRef.Name.String(), prefixes) {
			continue
		}

		refs = append(refs, existingRef.Name)
	}
```

**File:** internal/git/localrepo/refs.go (L107-113)
```go
// GetReferences returns references matching any of the given patterns. If no patterns are given,
// all references are returned.
func (repo *Repo) GetReferences(ctx context.Context, patterns ...string) ([]git.Reference, error) {
	return gitcmd.GetReferences(ctx, repo, gitcmd.GetReferencesConfig{
		Patterns: patterns,
	})
}
```

**File:** internal/git/gitcmd/reference.go (L23-63)
```go
// GetReferences enumerates references in the given repository. By default, it returns all references that exist in the
// repository. This behaviour can be tweaked via the `GetReferencesConfig`.
func GetReferences(ctx context.Context, repoExecutor RepositoryExecutor, cfg GetReferencesConfig) ([]git.Reference, error) {
	flags := []Option{Flag{Name: "--format=%(refname)%00%(objectname)%00%(symref)"}}
	if cfg.Limit > 0 {
		flags = append(flags, Flag{Name: fmt.Sprintf("--count=%d", cfg.Limit)})
	}

	cmd, err := repoExecutor.Exec(ctx, Command{
		Name:  "for-each-ref",
		Flags: flags,
		Args:  cfg.Patterns,
	}, WithSetupStdout())
	if err != nil {
		return nil, err
	}

	scanner := bufio.NewScanner(cmd)

	var refs []git.Reference
	for scanner.Scan() {
		line := bytes.SplitN(scanner.Bytes(), []byte{0}, 3)
		if len(line) != 3 {
			return nil, errors.New("unexpected reference format")
		}

		if len(line[2]) == 0 {
			refs = append(refs, git.NewReference(git.ReferenceName(line[0]), git.ObjectID(line[1])))
		} else {
			refs = append(refs, git.NewSymbolicReference(git.ReferenceName(line[0]), git.ReferenceName(line[1])))
		}
	}

	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("reading standard input: %w", err)
	}
	if err := cmd.Wait(); err != nil {
		return nil, err
	}

	return refs, nil
```

**File:** internal/gitaly/service/ref/list_refs.go (L48-53)
```go
	sorting := sortDirectionByEnum[in.GetSortBy().GetDirection()] + sortKeyByEnum[in.GetSortBy().GetKey()]
	paginationParams := in.GetPaginationParams()
	if paginationParams != nil && paginationParams.GetPageToken() != "" {
		decodedToken := decodeListRefsPageToken(paginationParams.GetPageToken())
		paginationParams.PageToken = decodedToken
	}
```

**File:** internal/gitaly/service/ref/find_all_tags.go (L69-79)
```go
	limit := opts.Limit
	i := 0

	parser := catfile.NewParser()

	for catfileObjectsIter.Next() {
		tag := catfileObjectsIter.Result()

		if i >= limit {
			break
		}
```
