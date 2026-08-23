This confirms the vulnerability pattern: `sendTreeEntriesUnified` (in `internal/gitaly/service/commit/get_tree_entries.go`) calls `repo.ReadTree(..., WithRecursive())` which for a recursive request invokes `TreeEntry.populate()`, running `git ls-tree -r` and buffering **every** entry of the (possibly enormous) tree into memory via `listEntries()` before pagination is ever applied. Only after the entire in-memory slice is built does `paginateTreeEntries()` slice it down to the requested `limit`. This mirrors the `getTickState()` bug exactly: the requested/limit parameter does not bound the actual work performed — the cost is proportional to total tree size, not to what the client asked for. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

### Title
`GetTreeEntries` recursive listing buffers the entire tree in memory before applying pagination limit, enabling DoS via oversized trees - (File: internal/gitaly/service/commit/get_tree_entries.go)

### Summary
`GetTreeEntries` with `recursive=true` fully materializes every entry of a tree into memory before the `PaginationParameter.limit` is applied, so a client-supplied small `limit` does not bound server-side work or memory usage.

### Finding Description
`sendTreeEntriesUnified` calls `repo.ReadTree(ctx, revision, localrepo.WithRecursive())` unconditionally when `recursive` is requested [1](#0-0) . `ReadTree` with the recursive option invokes `rootEntry.populate(ctx, repo)` [6](#0-5) , which runs `git ls-tree -r` via `listEntries()` and appends **every** parsed entry to an in-memory slice with no early termination or size cap [7](#0-6) , then reconstructs a nested tree structure for the full result set [8](#0-7) .

Back in the RPC handler, `tree.Walk(...)` flattens this fully-populated structure into an `entries` slice covering the whole tree [9](#0-8) , entries are sorted, and only *after* this complete enumeration does `paginateTreeEntries()` slice the result down using the client's `limit` [10](#0-9) . The `limit`/pagination mechanism therefore only reduces what is *sent back* to the client — it does nothing to bound the git-process execution, output parsing, or memory allocation that already happened for the entire tree, no matter how large.

This is the direct analog of the reported `getTickState()` issue: a function that is supposed to support incremental/paginated iteration but instead always walks the full backing structure regardless of the requested page size, making the true cost proportional to total data size, not requested limit.

### Impact Explanation
Any user who can push content to a repository visible to Gitaly (an ordinary contributor with push access, or via a fork/import) can craft a commit whose tree recursively contains an extremely large number of entries (e.g., millions of files/directories). Any subsequent `GetTreeEntries` RPC call against that revision with `recursive=true` — even one requesting `limit=1` — forces the Gitaly server to spawn `git ls-tree -r`, parse the entire output, build the full nested `TreeEntry` graph, walk it, and sort it in memory before truncating for the response. This is a CPU and memory resource-exhaustion vector against the RPC handler, consistent with the "DoS of a handler" acceptance criterion, and can degrade or crash the Gitaly node servicing the repository regardless of the pagination limit the client specifies.

### Likelihood Explanation
Reaching this path only requires committing a tree with a very large number of entries (git itself has no meaningful limit on tree entry counts) and calling the public `GetTreeEntries` RPC with `recursive: true`, which is a standard, unprivileged, client-facing operation used by GitLab's repository browsing/API features. No special access beyond ordinary push/read permissions is needed.

### Recommendation
Bound the cost of recursive tree listing to the requested pagination limit rather than materializing the full tree first:
- Stream `git ls-tree -r` output and stop reading/parsing once `limit` matching entries (past the page token) have been produced, instead of buffering all entries via `listEntries`/`populate`.
- Alternatively, enforce a hard server-side cap on the number of tree entries processed per request (independent of client-supplied limit) and return an error/truncated result once exceeded, so a single request cannot force unbounded work.
- Apply the same fix pattern used elsewhere in the codebase where iterators are properly limited during consumption (e.g., `ListCommits`'s use of `WithSkipRevlistResult`/limit capping before exhausting the pipe) rather than after full collection.

### Proof of Concept
1. Create a repository and commit a tree with an extremely large number of files, e.g. via a script generating 5,000,000 unique blob paths in a single commit (`git mktree`/`git update-index` at scale, or many `git commit-tree` calls building nested directories).
2. Call `CommitService.GetTreeEntries` with `Recursive: true`, `Revision: <that commit>`, `Path: "."`, and `PaginationParams.Limit: 1`.
3. Observe that despite `limit=1`, the Gitaly process spends resources proportional to the full 5,000,000-entry tree (spawning `git ls-tree -r`, parsing the entire output, allocating the full `TreeEntry` graph, and sorting it) before returning a single entry — repeatable calls can be used to exhaust CPU/memory on the serving node.

### Citations

**File:** internal/gitaly/service/commit/get_tree_entries.go (L97-125)
```go
	var readTreeOpts []localrepo.ReadTreeOption
	if recursive {
		readTreeOpts = append(readTreeOpts, localrepo.WithRecursive())
	}

	var hasPageTokenTreeOID bool
	treeRevision := revision
	if p != nil && p.GetPageToken() != "" {
		// Extract root tree OID from the token, if present.
		// The root tree OID is used to ensure that subsequent paginated requests access the same tree
		_, tokenTreeOID, _ := decodePageToken(p.GetPageToken())
		if tokenTreeOID != "" {
			treeRevision = tokenTreeOID
			hasPageTokenTreeOID = true
		}
	}

	// When tree OID resolved from the previous request is used instead of the revision,
	// the path is no longer relative to the revision. Please refer https://gitlab.com/gitlab-org/gitaly/-/issues/4556#note_2004951285
	// for more details.
	if !hasPageTokenTreeOID {
		readTreeOpts = append(readTreeOpts, localrepo.WithRelativePath(path))
	}

	tree, err := repo.ReadTree(
		ctx,
		git.Revision(treeRevision),
		readTreeOpts...,
	)
```

**File:** internal/gitaly/service/commit/get_tree_entries.go (L182-223)
```go
	var entries []*gitalypb.TreeEntry
	if err := tree.Walk(func(dir string, entry *localrepo.TreeEntry) error {
		if entry.OID == tree.OID {
			return nil
		}

		objectID, err := entry.OID.Bytes()
		if err != nil {
			return fmt.Errorf("converting tree entry OID: %w", err)
		}

		newEntry, err := git.NewTreeEntry(
			revision,
			path,
			[]byte(filepath.Join(dir, entry.Path)),
			objectID,
			[]byte(entry.Mode),
		)
		if err != nil {
			return fmt.Errorf("converting tree entry: %w", err)
		}

		entries = append(entries, newEntry)

		return nil
	}); err != nil {
		return fmt.Errorf("listing tree entries: %w", err)
	}

	// We sort before we paginate to ensure consistent results with ListLastCommitsForTree
	entries, err = sortTrees(entries, sort)
	if err != nil {
		return err
	}

	cursor := ""
	if p != nil {
		entries, cursor, err = paginateTreeEntries(ctx, entries, p, tree.OID)
		if err != nil {
			return err
		}
	}
```

**File:** internal/git/localrepo/tree.go (L384-440)
```go
// listEntries lists tree entries for the given treeish.
func (repo *Repo) listEntries(
	ctx context.Context,
	treeish git.Revision,
	relativePath string,
	recursive bool,
) ([]*TreeEntry, error) {
	flags := []gitcmd.Option{gitcmd.Flag{Name: "-z"}}
	if recursive {
		flags = append(flags,
			gitcmd.Flag{Name: "-r"},
			// By default, when -r is passed, tree entries will not
			// be shown. -t will cause tree entries to be shown as
			// well even when -r is passed.
			gitcmd.Flag{Name: "-t"},
		)
	}

	if relativePath == "." {
		relativePath = ""
	}

	var stderr bytes.Buffer
	cmd, err := repo.Exec(ctx, gitcmd.Command{
		Name:  "ls-tree",
		Args:  []string{fmt.Sprintf("%s:%s", treeish, relativePath)},
		Flags: flags,
	}, gitcmd.WithStderr(&stderr), gitcmd.WithSetupStdout())
	if err != nil {
		return nil, fmt.Errorf("spawning git-ls-tree: %w", err)
	}

	objectHash, err := repo.ObjectHash(ctx)
	if err != nil {
		return nil, fmt.Errorf("detecting object hash: %w", err)
	}

	parser := NewParser(cmd, objectHash)
	var entries []*TreeEntry
	for {
		entry, err := parser.NextEntry()
		if err != nil {
			if errors.Is(err, io.EOF) {
				break
			}

			return nil, fmt.Errorf("parsing tree entry: %w", err)
		}

		entries = append(entries, entry)
	}

	if err := cmd.Wait(); err != nil {
		return nil, structerr.New("waiting for git-ls-tree: %w", err).WithMetadata("stderr", stderr.String())
	}

	return entries, nil
```

**File:** internal/git/localrepo/tree.go (L475-533)
```go
// populate scans through the output of ls-tree -r, and constructs a TreeEntry
// object.
func (t *TreeEntry) populate(
	ctx context.Context,
	repo *Repo,
) error {
	if t.OID == "" {
		return errors.New("oid is empty")
	}

	t.Entries = nil

	entries, err := repo.listEntries(
		ctx,
		git.Revision(t.OID),
		"",
		true,
	)
	if err != nil {
		return err
	}

	stack := treeStack{t}

	// The output of ls-tree -r is the following:
	// a1
	// dir1/file2
	// dir2/file3
	// f2
	// f3
	// Whenever we see a tree, push it onto the stack since we will need to
	// start adding to that tree's entries.
	// When we encounter an entry that has a lower depth than the previous
	// entry, we know that we need to pop the tree entry off to get back to the
	// parent tree.
	for _, entry := range entries {
		if levelsToPop := len(stack) - depthByPath(entry.Path); levelsToPop > 0 {
			for i := 0; i < levelsToPop; i++ {
				stack.pop()
			}
		}

		entry.Path = filepath.Base(entry.Path)
		stack.peek().Entries = append(
			stack.peek().Entries,
			entry,
		)

		if entry.Type == Tree {
			stack.push(entry)
		}
	}

	if err != nil {
		return fmt.Errorf("listing entries: %w", err)
	}

	return nil
}
```

**File:** internal/git/localrepo/tree.go (L674-738)
```go
// ReadTree gets a tree object with all of the direct children populated.
// Walk can be called to populate every level of the tree.
func (repo *Repo) ReadTree(ctx context.Context, treeish git.Revision, options ...ReadTreeOption) (*TreeEntry, error) {
	var c readTreeConfig

	for _, opt := range options {
		opt(&c)
	}

	if c.relativePath == "." {
		c.relativePath = ""
	}

	rev := git.Revision(string(treeish) + ":" + c.relativePath)

	treeOID, err := repo.ResolveRevision(ctx, rev)
	if err != nil {
		return nil, fmt.Errorf("getting revision: %w", err)
	}

	objectReader, cancel, err := repo.catfileCache.ObjectInfoReader(ctx, repo)
	if err != nil {
		return nil, fmt.Errorf("get catfile reader: %w", err)
	}
	defer cancel()

	// Perform a preliminary check on the revision to ensure it's treeish.
	obj, err := objectReader.Info(ctx, treeOID.Revision())
	if err != nil {
		// If the resolved revision does not exist, the repository is either corrupt or the revision
		// refers to the commit ID of a Git submodule. We assume it is the latter if a relative path
		// is present. Reading tree entries from a submodule is not supported and consequently an
		// error is returned.
		if errors.As(err, &catfile.NotFoundError{}) && c.relativePath != "" {
			return nil, fmt.Errorf("reading resolved revision: %w", ErrTreeNotExist)
		}

		return nil, fmt.Errorf("check object type: %w", err)
	}
	if obj.Type != "tree" {
		return nil, ErrNotTreeish
	}

	rootEntry := &TreeEntry{
		OID:  treeOID,
		Type: Tree,
		Mode: "040000",
	}

	if c.recursive {
		if err := rootEntry.populate(ctx, repo); err != nil {
			return nil, err
		}
	} else {
		if rootEntry.Entries, err = repo.listEntries(
			ctx,
			treeish,
			c.relativePath,
			c.recursive,
		); err != nil {
			return nil, fmt.Errorf("listEntries: %w", err)
		}
	}

	return rootEntry, nil
```
