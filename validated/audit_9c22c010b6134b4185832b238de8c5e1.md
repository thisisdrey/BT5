### Title
Filename containing a tab byte breaks the fixed "3-field" assumption in `git-merge-tree(1)` conflict output parsing, causing a permanent DoS of merge/rebase/cherry-pick/revert/conflict-resolution RPCs - ([File: internal/git/localrepo/merge.go])

### Summary
`parseConflictingFileInfo` in `internal/git/localrepo/merge.go` assumes that each conflicting-file-info line emitted by `git-merge-tree(1)` can always be split into exactly two parts by a single `\t` character (`<mode> <object> <stage>\t<filename>`), and that the first part always splits into exactly three whitespace-delimited fields. [1](#0-0) 
This is the same bug class as the Solana report: parsing code hard-codes an expected structural shape (fixed field/instruction count) of attacker-influenceable data instead of tolerating arbitrary valid input, so an attacker who controls that data can break the parse and force the operation to fail.

### Finding Description
`Repo.MergeTree` invokes `git merge-tree --write-tree -z` and, on conflict (exit code 1), calls `parseMergeTreeError`, which in turn calls `parseConflictingFileInfo` for each conflicting file-info line. [2](#0-1) 
For every conflict, `parseConflictingFileInfo` splits the line on `\t` and hard-fails unless it produces exactly 2 tokens, then splits the first token on whitespace and hard-fails unless it produces exactly 3 tokens: [3](#0-2) 

Git tree entries may legally contain any byte in the filename except `/` and NUL, including a literal tab (`\t`) character. Because `git-merge-tree`'s `-z` output uses NUL as the record separator but a raw tab as the field separator between `<mode> <object> <stage>` and `<filename>`, a filename that itself contains a tab byte produces more than 2 tokens when split on `\t`, tripping the `len(infoAndFilename) != 2` check and returning `structerr.NewInternal("parsing conflicting file info: %s", infoLine)`.

This parser is reachable from every code path that calls `MergeTree` to detect or resolve conflicts as part of ordinary user-triggered operations: rebase, cherry-pick, revert, apply-patch, and conflict listing/resolution, per the call sites found: [4](#0-3) 

### Impact Explanation
An attacker who can push a commit or branch containing a file whose name embeds a tab character (a valid, if unusual, git tree entry name) can force any subsequent merge attempt that conflicts on that file to fail with an internal parsing error rather than a proper `MergeTreeConflictError`. Because the failure happens deep inside output parsing rather than in git itself, callers (merge branch, rebase, cherry-pick, revert, resolve/list conflicts) cannot distinguish this from an unexpected internal fault, and the operation is permanently blocked for as long as the conflicting file with the crafted name exists in the affected branches — a repeatable, low-cost denial of service against these RPC handlers, directly analogous to the Solana report where stuffing extra structure broke a fixed-shape parser and blocked finalization.

### Likelihood Explanation
Any ordinary user capable of pushing a commit (creating a file with a tab in its name is trivial with `git update-index`/`git mktree`, no special git client support needed) and later causing a merge/rebase/cherry-pick to conflict on that file can trigger this deterministically. No privileged access, leaked tokens, or MITM position is required — this fits squarely in the "unprivileged, reachable via ordinary push/fork" category.

### Recommendation
Do not assume a fixed field count in `git-merge-tree(1)` output. Instead:
- Split conflicting-file-info lines on the *last* tab occurrence (`strings.LastIndex(infoLine, "\t")`) rather than expecting exactly one tab, since the filename is always the suffix and `<mode> <object> <stage>` never itself contains a tab.
- Similarly, parse `<mode> <object> <stage>` using bounded reads on the first two space-delimited fields, then treat the remainder as the stage field, rather than requiring `strings.Fields` to yield exactly 3 tokens.
- Add regression tests using filenames containing tab and other unusual-but-legal bytes to `parseConflictingFileInfo`/`parseMergeTreeError`.

### Proof of Concept
1. Create a repository with two branches that both modify a file whose path/name contains a literal tab byte (e.g. `a\tb`), such that merging them produces a conflict on that file.
2. Call `Repo.MergeTree` (or trigger the equivalent gRPC operation, e.g. `UserMergeBranch`/`UserRebaseConfirmable`/`ResolveConflicts`/`ListConflictFiles`) between the two branches.
3. `git merge-tree` exits with status 1 and prints the conflicting file info line containing the embedded tab, e.g. `100644 <oid> 2\ta\tb`.
4. `parseConflictingFileInfo` splits this line on `\t` and gets 3 tokens instead of 2, returning `structerr.NewInternal("parsing conflicting file info: %s", infoLine)` instead of a proper conflict result, causing the RPC to fail every time this conflict is encountered.

### Citations

**File:** internal/git/localrepo/merge.go (L69-75)
```go
// MergeTree calls git-merge-tree(1) with arguments, and parses the results from
// stdout.
func (repo *Repo) MergeTree(
	ctx context.Context,
	ours, theirs string,
	mergeTreeOptions ...MergeTreeOption,
) (git.ObjectID, error) {
```

**File:** internal/git/localrepo/merge.go (L107-126)
```go
	var stdout, stderr bytes.Buffer
	err = repo.ExecAndWait(
		ctx,
		gitcmd.Command{
			Name:  "merge-tree",
			Flags: flags,
			Args:  []string{ours, theirs},
		},
		gitcmd.WithStderr(&stderr),
		gitcmd.WithStdout(&stdout),
	)
	if err != nil {
		exitCode, success := command.ExitStatus(err)
		if !success {
			return "", structerr.NewInternal("could not parse exit status of merge-tree(1)")
		}

		if exitCode == 1 {
			return parseMergeTreeError(objectHash, config, stdout.String())
		}
```

**File:** internal/git/localrepo/merge.go (L198-217)
```go
func parseConflictingFileInfo(objectHash git.ObjectHash, cfg mergeTreeConfig, mergeTreeConflictError *MergeTreeConflictError, conflicts []string) error {
	mergeTreeConflictError.ConflictingFileInfo = make([]ConflictingFileInfo, len(conflicts))

	// From git-merge-tree(1), the information is of the format `<mode> <object> <stage> <filename>`
	// unless the `--name-only` option is used, in which case only the filename is output.
	// Note: that there is \t before the filename (https://gitlab.com/gitlab-org/git/blob/v2.40.0/builtin/merge-tree.c#L481)
	for i, infoLine := range conflicts {
		if cfg.conflictingFileNamesOnly {
			mergeTreeConflictError.ConflictingFileInfo[i].FileName = infoLine
		} else {
			infoAndFilename := strings.Split(infoLine, "\t")
			if len(infoAndFilename) != 2 {
				return structerr.NewInternal("parsing conflicting file info: %s", infoLine)
			}

			info := strings.Fields(infoAndFilename[0])
			if len(info) != 3 {
				return structerr.NewInternal("parsing conflicting file info: %s", infoLine)
			}

```
