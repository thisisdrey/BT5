### Title
`linkedToRepository` treats a match on only the first alternates entry as fully linked, letting additional attacker-planted alternate lines survive `Link` unmodified - ([File: internal/git/objectpool/link.go])

### Summary
`linkedToRepository` in `internal/git/objectpool/link.go` only inspects `altInfo.ObjectDirectories[0]` to decide whether a repository is already correctly linked to a pool. If the first line happens to equal the expected pool object path, the function returns `linked = true` and `Link` returns immediately without ever re-writing or validating the rest of the `objects/info/alternates` file. Any additional lines in that file — which `ReadAlternatesFile` fully parses and Git itself will honor at runtime — are left untouched.

### Finding Description
`Link` (`internal/git/objectpool/link.go:28-84`) calls `linkedToRepository(ctx, pool, repo)` before deciding whether to write a new alternates file: [1](#0-0) 

`linkedToRepository` reads the alternates file via `stats.AlternatesInfoForRepository`, which parses *every* non-empty, non-comment line into `ObjectDirectories`: [2](#0-1) 

But the linkage decision only compares the **first** entry: [3](#0-2) 

If `ObjectDirectories[0]` equals `expectedRelPath` (the correct relative path to the legitimate pool), the function returns `true` and `Link` short-circuits, casting a vote and returning without touching the file at all. Any subsequent line(s) in `objects/info/alternates` — e.g., a path to a sibling repository's `objects` directory — are never inspected, never validated, and never removed. Since Git's alternates mechanism honors every line in the file (not just the first), a crafted multi-line alternates file that starts with a "correct-looking" first entry followed by a malicious second entry will remain fully active after a `LinkRepositoryToObjectPool` call, giving the member repository (and thus anything reading through it) access to objects of the arbitrary path referenced by the extra line.

The check does not validate that the file contains exactly one line, nor does it reject or overwrite files containing extra entries — it only errors out when the sole/first entry is an unrecognized absolute pool path (`filepath.Clean(relPath) != filepath.Join(poolPath, "objects")`), and even that branch is unreachable once entry 0 already matched `expectedRelPath`.

### Impact Explanation
If an attacker can get an `objects/info/alternates` file with a crafted first line plus additional malicious lines into their own repository (e.g., via a repository-cloning/import/replication path that copies raw repository files, or via any other Gitaly operation that writes to that file with attacker-influenced content) before `LinkRepositoryToObjectPool` runs, `linkedToRepository`'s first-line-only trust causes `Link` to silently no-op and leave the extra alternate active. Repository isolation would be violated — Git operations against the member repo would transparently resolve objects from the extra alternate directory, exposing objects (blobs, commits, private history) from a repository the attacker does not otherwise have access to. This matches GitLab's "cross-repository object access / broken isolation" bounty impact class.

### Likelihood Explanation
This requires the attacker to first achieve a write of a *multi-line* `objects/info/alternates` file into their own repository, where line 0 must equal the exact expected relative path Gitaly will compute between that repo and a legitimate pool (`getRelativeObjectPath`). This is a nontrivial precondition: `objects/info/alternates` is not part of the normal git push/fetch wire protocol, and I could not confirm within this investigation an unprivileged, default-configuration RPC path that lets an attacker place arbitrary raw file content (specifically a crafted multi-line alternates file with a correctly-guessed first line) into `objects/info/alternates` of a repository they control, prior to a `LinkRepositoryToObjectPool` call. Without a verified, reachable write primitive into that specific file with attacker-chosen content, the exact reachable path from attacker input required by the audit rules is not established with certainty in the code reviewed.

### Recommendation
`linkedToRepository` should validate that the alternates file contains **exactly one** entry and that it equals `expectedRelPath`; any additional or unexpected content should cause `Link` to treat the repository as not-yet-correctly-linked (or error out) so that the file gets rewritten/sanitized rather than left as-is.

### Proof of Concept
Not able to construct a concrete, reproducible RPC-level PoC within this investigation because the exact unprivileged write path that plants a crafted multi-line `objects/info/alternates` file (with an attacker-guessable first line matching `expectedRelPath`) prior to `LinkRepositoryToObjectPool` was not confirmed. A Go-level unit test against `Link`/`linkedToRepository` directly (bypassing the unconfirmed write primitive) would trivially show the bug: pre-write an alternates file with the pool's correct relative object path as line 1 and a sibling repo's `objects` path as line 2, call `Link`, and assert that line 2 is still present and that the returned `linked` is `true` without the file being rewritten — but this does not by itself demonstrate an attacker-reachable exploit chain from an RPC.

### Citations

**File:** internal/git/objectpool/link.go (L39-52)
```go
	linked, err := linkedToRepository(ctx, pool, repo)
	if err != nil {
		return err
	}

	if linked {
		// When the repository is already linked to the repository, cast a vote to ensure the
		// repository is consistent with the other replicas.
		if err := transaction.VoteOnContext(ctx, txManager, voting.VoteFromData([]byte("repository linked")), voting.Synchronized); err != nil {
			return fmt.Errorf("vote on linked repository: %w", err)
		}

		return nil
	}
```

**File:** internal/git/objectpool/link.go (L185-203)
```go
	if !altInfo.Exists || len(altInfo.ObjectDirectories) == 0 {
		return false, nil
	}

	relPath := altInfo.ObjectDirectories[0]
	expectedRelPath, err := getRelativeObjectPath(ctx, pool, repo)
	if err != nil {
		return false, err
	}

	if relPath == expectedRelPath {
		return true, nil
	}

	if filepath.Clean(relPath) != filepath.Join(poolPath, "objects") {
		return false, fmt.Errorf("unexpected alternates content: %q", relPath)
	}

	return false, nil
```

**File:** internal/git/stats/repository_info.go (L613-642)
```go
func ReadAlternatesFile(repoPath string) ([]string, error) {
	file, err := os.Open(AlternatesFilePath(repoPath))
	if err != nil {
		return nil, fmt.Errorf("open: %w", err)
	}
	defer file.Close()

	var alternatePaths []string
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := scanner.Bytes()

		switch {
		case len(line) == 0:
			// Empty lines are skipped by Git.
			continue
		case bytes.HasPrefix(line, []byte("#")):
			// Lines starting with a '#' are comments and thus need to be skipped.
			continue
		default:
			alternatePaths = append(alternatePaths, scanner.Text())
		}
	}

	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("scanning alternate paths: %w", err)
	}

	return alternatePaths, nil
}
```
