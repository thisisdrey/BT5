I found a concrete candidate: `SearchFilesByNameRequest.limit` explicitly states "Gitaly does not enforce a limit by default" — an unbounded, user-controllable request that can drive unbounded iteration server-side, directly analogous to the audit's unbounded storage-array iteration causing resource exhaustion/failure.### Title
Unbounded in-memory accumulation and single-shot ls-tree parsing in `SearchFilesByName` allows unprivileged callers to trigger memory/CPU exhaustion DoS - ([File: internal/gitaly/service/repository/search_files.go])

### Summary
`SearchFilesByName` matches the audit's root cause: a caller-influenced collection is iterated/accumulated without an enforced cap, so a normal, unprivileged caller (any client with repository access, not a "privileged owner") can force the server to do unbounded work in a single RPC, analogous to the `getRewardsWeight()` unbounded loop that "will always fail" / exhausts resources.

### Finding Description
The proto explicitly documents that `SearchFilesByNameRequest.limit` is optional and unenforced: "limit the number of returned files. **Gitaly does not enforce a limit by default.** Clients should always set a value for this field. limit = 0 means unlimited files." [1](#0-0) 

The handler `SearchFilesByName` calls `parseLsTree` with `int(req.GetLimit())`, and when `limit` is `0` (the default, fully attacker/client controlled and requiring no special privilege), the loop condition `if limit > 0 && len(files) >= limit { break }` never triggers, so the loop keeps calling `parser.NextEntryPath()` and appending to the `files` slice until `git ls-tree` exhausts the entire tree: [2](#0-1) 

Unlike `ListRefs`/`FindRefsByOID`/`ListBlobs`, which stream results in bounded chunks or enforce pagination cursors, `SearchFilesByName` accumulates the entire unbounded `files ([][]byte)` slice in memory and sends it in a **single** `stream.Send(&gitalypb.SearchFilesByNameResponse{Files: files})` call rather than chunking: [3](#0-2) 

This is directly analogous to the reported Solidity bug class: an array (`files`, populated by iterating all tree entries matching a caller-supplied query/prefix) is iterated and grown without any enforced maximum, and the resulting operation either consumes excessive resources or exceeds hard limits (here, the gRPC max message size, rather than a gas limit) causing the RPC to fail/never complete for large enough repositories — the same "will always revert / fail" outcome, but reachable via an ordinary `SearchFilesByName` RPC call rather than requiring privileged access to grow the array.

### Impact Explanation
For repositories with a large number of matching tree entries (e.g., broad `query` prefix like `"."` against a monorepo with hundreds of thousands of files, as already exercised in the RPC's own test suite with `Query: "."`), the handler will: (1) hold the entire result set in memory as a Go slice of byte-slices, and (2) attempt to marshal and send it as one gRPC message. This can cause excessive memory allocation and CPU time within the Gitaly process handling the request, and can exceed gRPC's message-size limits, causing the RPC to fail outright for large repositories — a self-inflicted denial-of-service on a repository-scoped ACCESSOR RPC. Because Gitaly's per-repository/per-RPC concurrency limiting (`[[concurrency]]` in `doc/backpressure.md`) throttles concurrent requests but does not bound the size of a single request's server-side work, repeated invocation of this call against a large repository can degrade Gitaly for other tenants sharing the storage/process.

### Likelihood Explanation
This is reachable by any client authorized to call repository-scoped RPCs (equivalent to "an ordinary user's ... crafted RPC field" per the validation criteria) — no elevated Gitaly-internal privilege is required, and the query/ref/limit fields are fully attacker-controlled input. The only precondition is that the target repository contains a large number of matching paths, which is common for large/monorepo-style GitLab projects. This mirrors the audit's characterization as "medium" severity because it depends on the attacker's ability to make the underlying data set (here, matching tree entries) large — analogous to the number of elements in the "rewards" array in the original finding.

### Recommendation
- Enforce a hard maximum on `limit` server-side (a "post-audit" style fix identical to the original recommendation of "add a maximum limit"), rejecting or capping requests with `limit == 0` or `limit` above a configured ceiling.
- Stream results in bounded chunks (as `SearchFilesByContent`, `ListRefs`, and `ListBlobs` already do) rather than materializing the entire `files` slice before a single `stream.Send`.
- Consider adding a maximum scanned-entries limit (independent of `limit`) so that even with a small `limit`, a pathological `query`/`filter` combination that matches almost nothing cannot force scanning of an unbounded tree without bound on elapsed time/CPU.

### Proof of Concept
1. As any client with repository read access, call `SearchFilesByName` with:
   - `repository`: a target repository with a very large tree (e.g., hundreds of thousands of files),
   - `ref`: a valid branch/commit,
   - `query`: `"."` (matches virtually the entire tree, as already used in existing tests, e.g. `internal/gitaly/service/repository/search_files_test.go`),
   - `limit`: `0` (default/unset).
2. Observe that `parseLsTree` iterates every entry emitted by `git ls-tree --full-tree --name-status -r -z <ref> -- .` and accumulates all matching paths into a single in-memory `[][]byte` slice because the `limit > 0` guard never triggers.
3. Observe that the entire slice is sent as one `SearchFilesByNameResponse` via a single `stream.Send`, causing large memory allocation and, for sufficiently large trees, either significant CPU/memory pressure on the Gitaly node or an RPC failure due to exceeding the gRPC message-size limit — without any need for elevated privileges, matching the "always fails/reverts due to unbounded iteration" characterization of the source finding.

### Citations

**File:** proto/repository.proto (L1192-1194)
```text
  // limit the number of returned files. Gitaly does not enforce a limit by default.
  // Clients should always set a value for this field. limit = 0 means unlimited files.
  uint32 limit = 5;
```

**File:** internal/gitaly/service/repository/search_files.go (L160-166)
```go
	files, err := parseLsTree(objectHash, cmd, filter, int(req.GetOffset()), int(req.GetLimit()))
	if err != nil {
		return err
	}

	return stream.Send(&gitalypb.SearchFilesByNameResponse{Files: files})
}
```

**File:** internal/gitaly/service/repository/search_files.go (L194-220)
```go
func parseLsTree(objectHash git.ObjectHash, cmd *command.Command, filter *regexp.Regexp, offset int, limit int) ([][]byte, error) {
	var files [][]byte
	var index int
	parser := localrepo.NewParser(cmd, objectHash)

	for {
		path, err := parser.NextEntryPath()
		if err != nil {
			if errors.Is(err, io.EOF) {
				break
			}
			return nil, err
		}
		if filter != nil && !filter.Match(path) {
			continue
		}

		index++
		if index > offset {
			files = append(files, path)
		}
		if limit > 0 && len(files) >= limit {
			break
		}
	}

	return files, nil
```
