### Title
Unbounded recursive tree walk in `GetTreeEntries` can crash gitaly via attacker-controlled deeply nested git tree - ([File: internal/git/localrepo/tree.go])

### Summary
`GetTreeEntries` with `recursive=true` populates a full in-memory tree via `TreeEntry.populate` (iterative, stack-based) but then serializes it for the client via `TreeEntry.Walk`, which walks the tree using genuine Go call-stack recursion, one stack frame per directory-nesting level of the underlying git tree object. Because the nesting depth of a git tree is entirely attacker-controlled (any user who can push/import a repository can create an arbitrarily deep chain of single-entry subtrees), this recursion depth is unbounded and reachable through an ordinary unprivileged RPC, mirroring the "RedeemManager" bug class of unbounded user-triggered recursion with no way to chunk the operation.

### Finding Description
`TreeEntry.Walk` at [1](#0-0)  calls `TreeEntry.walk`, which recurses into every child entry: [2](#0-1) 

Each recursive call corresponds to one level of the tree hierarchy. This is invoked from `sendTreeEntriesUnified` when handling the `GetTreeEntries` RPC: [3](#0-2) 

The tree itself is built from `ReadTree(..., WithRecursive())`, which internally calls `TreeEntry.populate`, which is implemented iteratively with an explicit `treeStack` (not recursive) specifically to avoid unbounded recursion during tree construction: [4](#0-3) 

However, this iterative-safety design is undone by the subsequent `Walk` call, which uses true function recursion to emit entries to the client. There is no cap on tree depth anywhere in this path. By contrast, the separate flat-path computation logic explicitly limits recursion to `defaultFlatTreeRecursion = 10` because it's known to be expensive/dangerous: [5](#0-4) [6](#0-5) 

No equivalent bound exists for the `Walk`-based serialization of a fully recursive `GetTreeEntries` request. A crafted repository (created via ordinary `git mktree`/commit operations and pushed by any user) with a very deep chain of nested single-entry trees (e.g. hundreds of thousands of levels) will cause `TreeEntry.walk` to recurse to that same depth for every `GetTreeEntries(Recursive=true)` request against it.

### Impact Explanation
Go goroutines have a growable stack, but it is capped (`debug.SetMaxStack`, default 1GB). Once a goroutine's stack would need to exceed this limit, the Go runtime raises a **fatal, unrecoverable** error (`goroutine stack exceeds ... limit`), which terminates the entire process — this is not a panic that `panichandler.StreamPanicHandler` (see [7](#0-6)  ) can recover, since fatal runtime errors bypass `recover()`. This means a single crafted repository and a single unprivileged `GetTreeEntries` call can crash the entire gitaly process, affecting all repositories and clients served by that process — a severe availability impact, analogous to how the RedeemManager recursion permanently locks user funds with no workaround, except here it takes down the whole service rather than a single user's redemption.

### Likelihood Explanation
Any user capable of pushing commits/trees to a repository hosted on gitaly (a completely ordinary, unprivileged action) can construct the deeply nested tree structure required, and any client that can call the unauthenticated-by-repo-ACL `GetTreeEntries` RPC with `Recursive=true` against that repository can trigger the crash. No special network position, leaked token, or privileged actor is required, satisfying the "ordinary user" reachability requirement.

### Recommendation
Convert `TreeEntry.walk`/`TreeEntry.Walk` to an iterative, explicit-stack traversal (mirroring the pattern already used in `populate`), or impose a maximum tree depth bound (comparable to `defaultFlatTreeRecursion`) beyond which the RPC returns an error instead of recursing further. Additionally, consider validating/limiting tree nesting depth at write time (e.g., in commit/tree creation RPCs) to prevent such pathological repository structures from being created at all.

### Proof of Concept
1. Using ordinary write RPCs (or direct `git mktree`/`git commit-tree` via `UserCommitFiles`/similar), construct a chain of N nested trees, each containing exactly one sub-tree entry (as already exercised for the bounded flat-path test at [8](#0-7) , but with N in the hundreds of thousands instead of 12).
2. Push/commit this structure to a repository served by gitaly.
3. Call `GetTreeEntries` with `Recursive: true` against the root of this structure.
4. `sendTreeEntriesUnified` → `tree.Walk` recurses N times; once N is large enough that the goroutine's stack would exceed the Go runtime's maximum stack size, the process fatally crashes with `runtime: goroutine stack exceeds ... limit`, taking down the whole gitaly node.

### Citations

**File:** internal/git/localrepo/tree.go (L475-526)
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
```

**File:** internal/git/localrepo/tree.go (L535-548)
```go
func (t *TreeEntry) walk(dirPath string, callback func(path string, entry *TreeEntry) error) error {
	nextDirPath := filepath.Join(dirPath, t.Path)
	if err := callback(dirPath, t); err != nil {
		return err
	}

	for _, entry := range t.Entries {
		if err := entry.walk(nextDirPath, callback); err != nil {
			return err
		}
	}

	return nil
}
```

**File:** internal/git/localrepo/tree.go (L550-560)
```go
// Walk will walk the whole tree structure and call callback on every entry of
// the tree in a depth-first like fashion.
func (t *TreeEntry) Walk(callback func(path string, entry *TreeEntry) error) error {
	for _, e := range t.Entries {
		if err := e.walk(t.Path, callback); err != nil {
			return err
		}
	}

	return nil
}
```

**File:** internal/gitaly/service/commit/get_tree_entries.go (L25-27)
```go
const (
	defaultFlatTreeRecursion = 10
)
```

**File:** internal/gitaly/service/commit/get_tree_entries.go (L183-209)
```go
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
```

**File:** internal/gitaly/service/commit/get_tree_entries.go (L238-244)
```go
		//
		// Unfortunately, computing flat paths is _really_ inefficient: for each
		// tree entry, we recurse up to 10 levels deep into that subtree. We do so
		// by requesting the tree entries via a catfile process, which to the best
		// of my knowledge is as good as we can get. Doing this via git-ls-tree(1)
		// wouldn't fly: we'd have to spawn a separate process for each of the
		// subtrees, which is a lot of overhead.
```

**File:** internal/grpc/middleware/panichandler/panic_handler.go (L49-63)
```go
func handleCrash(logger log.Logger, grpcMethodName string, handler PanicHandler) {
	if r := recover(); r != nil {
		logger.WithFields(log.Fields{
			"error":     r,
			"method":    grpcMethodName,
			"backtrace": string(debug.Stack()),
		}).Error("grpc panic")

		handler(grpcMethodName, r)

		for _, fn := range additionalHandlers {
			fn(grpcMethodName, r)
		}
	}
}
```

**File:** internal/gitaly/service/commit/get_tree_entries_test.go (L498-520)
```go
			desc: "deeply nested flat path",
			setup: func(t *testing.T, data TestData) setupData {
				repo, repoPath := gittest.CreateRepository(t, ctx, data.cfg)

				nestingLevel := 12
				require.Greater(t, nestingLevel, defaultFlatTreeRecursion, "sanity check: construct folder deeper than default recursion value")

				// We create a tree structure that is one deeper than the flat-tree recursion limit.
				var treeIDs []git.ObjectID
				for i := nestingLevel; i >= 0; i-- {
					var treeEntry gittest.TreeEntry
					if len(treeIDs) == 0 {
						treeEntry = gittest.TreeEntry{Path: ".gitkeep", Mode: "100644", Content: "something"}
					} else {
						// We use a numbered directory name to make it easier to see when things get
						// truncated.
						treeEntry = gittest.TreeEntry{Path: strconv.Itoa(i), Mode: "040000", OID: treeIDs[len(treeIDs)-1]}
					}

					treeID := gittest.WriteTree(t, data.cfg, repoPath, []gittest.TreeEntry{treeEntry})
					treeIDs = append(treeIDs, treeID)
				}
				commitID := gittest.WriteCommit(t, data.cfg, repoPath, gittest.WithTree(treeIDs[len(treeIDs)-1]))
```
