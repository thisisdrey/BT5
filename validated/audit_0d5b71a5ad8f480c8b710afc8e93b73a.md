### Title
Unbounded flat-path computation in `GetTreeEntries` allows resource-consumption DoS via wide trees - ([File: internal/gitaly/service/commit/get_tree_entries.go])

### Summary
`GetTreeEntries` computes a "flat path" for every tree-type entry returned in a non-recursive listing by issuing up to `defaultFlatTreeRecursion` (10) sequential `catfile.TreeEntries` calls per entry. This computation happens on the full, unbounded entry list whenever the client does not set a `PaginationParameter` (a normal, unauthenticated-to-Gitaly, ordinary client request), meaning an attacker who can push one commit containing a tree with a very large number of subdirectory entries can force the Gitaly node to perform an extremely large number of catfile round-trips on every subsequent (cheap, read-only) browse of that path — the same "cheap one-time setup, cheap repeated trigger, expensive backend processing" DoS pattern described in the source report about op-geth's gas tracker.

### Finding Description
In `sendTreeEntriesUnified`, tree entries for a single directory level are collected via `tree.Walk` into `entries` with no cap on count [1](#0-0) . Pagination (`paginateTreeEntries`) is applied only `if p != nil` [2](#0-1) ; when the caller does not set `PaginationParameter` — the common case for browsing/tooling that does not explicitly page — `entries` still contains every child of the requested tree.

Immediately after, for non-recursive, non-`skipFlatPaths` requests, `populateFlatPath` is invoked on this full, unbounded `entries` slice [3](#0-2) . Inside `populateFlatPath`, for every tree-typed entry the code performs up to `defaultFlatTreeRecursion - 1` (9) additional `catfile.TreeEntries` calls, each one walking one level deeper into the subtree looking for the first "interesting" ancestor directory [4](#0-3) . The code's own comment acknowledges this is "_really_ inefficient" [5](#0-4) .

Git tree objects can legitimately contain a very large number of entries (tens or hundreds of thousands) created by a single, cheap `git commit`/push from any user who can write to the repository (e.g. a fork/personal project, or a merge request source branch). Once such a tree exists, any ordinary, unprivileged read of that directory (e.g. web UI file browsing, which does not necessarily set an explicit pagination limit) triggers `entries_count * up-to-9` sequential catfile object lookups on the Gitaly node, all within a single RPC handler invocation, with no overall cap on total work performed. This mirrors the report's core root cause: overhead that scales with attacker-controlled structure size is not bounded relative to the "cheapness" of the triggering request, and is repeatable at low cost by any caller who can read the path (not just the party who created the tree).

### Impact Explanation
Each additional `GetTreeEntries` call against the crafted wide tree consumes significant CPU/IO on the Gitaly node (catfile object reads scale as O(N × 10) for N direct entries), and because the RPC is a normal read-path RPC used by GitLab's UI/API for repository browsing, it can be triggered repeatedly and cheaply by any user (or automated crawler) with read access to the repository. Under load this can starve catfile processes and CPU on the Gitaly node, degrading or denying service for other repositories/tenants served by the same Gitaly node — a resource-consumption DoS analogous to the reported gas-tracker issue where inexpensive triggers caused unbounded backend work.

### Likelihood Explanation
Creating a tree with a very large number of entries requires only a single ordinary commit/push, which is within reach of any user who can write to a repository (including on GitLab.com-style multi-tenant hosting where users push to their own namespaces/forks). Triggering the expensive path requires only a normal, unauthenticated-to-Gitaly (i.e., ordinary RPC caller) `GetTreeEntries` request without an explicit pagination limit — the default browsing behavior in many UI/API integrations. No special privileges, timing races, or MITM conditions are required, making this readily reachable through ordinary push + fetch/browse operations.

### Recommendation
- Enforce a pagination limit (or an internal cap) for `GetTreeEntries` responses even when the caller does not supply `PaginationParameter`, so `entries` is bounded before `populateFlatPath` runs.
- Cap the total number of entries eligible for flat-path computation per request (or the total number of `catfile.TreeEntries` calls performed per request), independent of client-supplied limits.
- Consider making flat-path computation opt-in (default `skip_flat_paths = true`) or computing it lazily/on-demand for only the entries actually returned to the client after pagination is applied.
- Add server-side timeouts/quotas around per-RPC catfile invocation counts to bound worst-case resource consumption regardless of tree shape.

### Proof of Concept
1. As an ordinary user with push access, create a commit whose tree contains a very large number of subdirectory entries (e.g., 100,000 empty subdirectories) — a single, cheap `git commit`/push.
2. As any user with read access, call `GetTreeEntries` on that directory with `Recursive=false`, `SkipFlatPaths=false`, and no `PaginationParams` set (the default for many callers).
3. Observe that Gitaly performs on the order of `entries_count × up to 9` sequential `catfile.TreeEntries` calls in `populateFlatPath` before returning any response, consuming disproportionate CPU/IO relative to the cost of steps 1–2, and that this can be repeated indefinitely at negligible cost to the caller.

### Citations

**File:** internal/gitaly/service/commit/get_tree_entries.go (L50-77)
```go
func populateFlatPath(
	ctx context.Context,
	objectReader catfile.ObjectContentReader,
	entries []*gitalypb.TreeEntry,
) error {
	for _, entry := range entries {
		entry.FlatPath = entry.GetPath()

		if entry.GetType() != gitalypb.TreeEntry_TREE {
			continue
		}

		for i := 1; i < defaultFlatTreeRecursion; i++ {
			subEntries, err := catfile.TreeEntries(ctx, objectReader, entry.GetCommitOid(), string(entry.GetFlatPath()))
			if err != nil {
				return err
			}

			if len(subEntries) != 1 || subEntries[0].GetType() != gitalypb.TreeEntry_TREE {
				break
			}

			entry.FlatPath = subEntries[0].GetPath()
		}
	}

	return nil
}
```

**File:** internal/gitaly/service/commit/get_tree_entries.go (L182-209)
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
```

**File:** internal/gitaly/service/commit/get_tree_entries.go (L217-223)
```go
	cursor := ""
	if p != nil {
		entries, cursor, err = paginateTreeEntries(ctx, entries, p, tree.OID)
		if err != nil {
			return err
		}
	}
```

**File:** internal/gitaly/service/commit/get_tree_entries.go (L231-253)
```go
	if !recursive && !skipFlatPaths {
		// When we're not doing a recursive request, then we need to populate flat
		// paths. A flat path of a tree entry refers to the first subtree of that
		// entry which either has at least one blob or more than two subtrees. In
		// other terms, it refers to the first "non-empty" subtree such that it's
		// easy to skip navigating the intermediate subtrees which wouldn't carry
		// any interesting information anyway.
		//
		// Unfortunately, computing flat paths is _really_ inefficient: for each
		// tree entry, we recurse up to 10 levels deep into that subtree. We do so
		// by requesting the tree entries via a catfile process, which to the best
		// of my knowledge is as good as we can get. Doing this via git-ls-tree(1)
		// wouldn't fly: we'd have to spawn a separate process for each of the
		// subtrees, which is a lot of overhead.
		objectReader, cancel, err := s.catfileCache.ObjectReader(stream.Context(), repo)
		if err != nil {
			return err
		}
		defer cancel()
		if err := populateFlatPath(ctx, objectReader, entries); err != nil {
			return err
		}
	}
```
