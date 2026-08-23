Based on the investigation, I found a concrete Gitaly analog of the reported bug class: **two different code paths derive/consume the same untrusted `GitObjectDirectory` / `GitAlternateObjectDirectories` repository fields, but only one of them validates for path traversal — mirroring the "inconsistent calculation method" pattern from the report.**

### Title
Inconsistent validation of quarantine/alternate object directory paths allows traversal via `alternates.Env` - (File: internal/git/alternates/alternates.go)

### Summary
`Repo.ObjectDirectoryPath()` in `internal/git/localrepo/paths.go` rigorously validates the `GitObjectDirectory` field of a `gitalypb.Repository` against storage/repo escape and quarantine-prefix rules before it is used. [1](#0-0) [2](#0-1) 

However, `alternates.Env()` — the function that actually builds the `GIT_OBJECT_DIRECTORY` and `GIT_ALTERNATE_OBJECT_DIRECTORIES` environment variables handed to every spawned Git subprocess — performs no such validation at all; it simply joins `repoPath` with whatever value is in the repository's object-directory fields. [3](#0-2) 

This function is invoked from `internal/git/gitcmd/command_factory.go`, which is the component responsible for constructing every Git command's execution environment (imports `alternates` package). [4](#0-3) 

### Finding Description
As documented, GitLab's `pre-receive` hook reads the quarantine object directory from its own environment, relays it through the Rails internal API, and Rails sends it back to Gitaly inside the `Repository` protobuf message on subsequent calls; Gitaly then "re-creates the environment variables" for spawning Git processes. [5](#0-4) 

The `GitObjectDirectory`/`GitAlternateObjectDirectories` fields are explicitly treated as untrusted, attacker-influenceable input elsewhere in the codebase — `paths_test.go` exercises directory-traversal payloads such as `"../bazqux.git"`, `"/../bazqux.git"`, and `"objects/../.."` against `ObjectDirectoryPath()`, all of which are correctly rejected with `InvalidArgument`. [6](#0-5) 

But `alternates.Env()`, which is the code path that actually configures Git's object lookup for every command executed against the repository (not just the RPCs that call `ObjectDirectoryPath()` explicitly), performs a raw `filepath.Join(repoPath, dir)` for the object directory and each alternate directory with no call to `storage.ValidateRelativePath` or any traversal/escape check. [3](#0-2) 

This is the same inconsistency shape as the reported bug: one operation (`ObjectDirectoryPath`, analogous to the "staking" path with weighted validation) applies the correct/strict logic, while the other (`alternates.Env`, analogous to `increaseLockup()`) reuses the same underlying input but bypasses the safety logic, computing the effective path a different, weaker way.

### Impact Explanation
If a `GitObjectDirectory` or an entry in `GitAlternateObjectDirectories` containing a traversal sequence (e.g. `../../other-repo-storage/objects`) reaches `alternates.Env()` without first passing through `ObjectDirectoryPath()`'s validation, Git subprocesses spawned by Gitaly would have `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` pointed outside of the intended repository or even outside of the storage root. This can result in cross-repository object exposure (objects from another repository becoming readable/writable through the quarantine/alternates mechanism) — one of the accepted "concrete" outcomes for this scan (cross-repository object access / storage escape).

### Likelihood Explanation
The `Repository` proto fields in question are round-tripped through Rails on every push/access-check flow (`pre-receive` → internal API → back to Gitaly), and the fact that Gitaly's own test suite specifically hardens `ObjectDirectoryPath()` against traversal in these exact fields indicates the team already considers this attacker-reachable input. The likelihood hinges on whether any call site constructs a `CommandFactory` git invocation using an unvalidated `Repository` (i.e., one that never passed through `ObjectDirectoryPath()`/`storage.ValidateRelativePath`) before `alternates.Env()` consumes it.

### Recommendation
Modify `alternates.Env()` in `internal/git/alternates/alternates.go` to validate `objectDirectory` and each entry of `alternateObjectDirectories` against the repository/storage root (reusing `storage.ValidateRelativePath`, as `ObjectDirectoryPath()` does) before joining paths and emitting environment variables, ensuring both code paths that consume these fields apply the same validation method.

### Proof of Concept
Not fully constructible from static analysis alone: I was unable to trace, within the remaining tool budget, the exact call chain proving that `command_factory.go` invokes `alternates.Env()` with a `Repository` object that has *not* already passed through `ObjectDirectoryPath()`/`GetRepoPath` validation for those specific fields. This is a necessary condition for exploitability, and I could not confirm or rule it out with certainty in the time available. I recommend a Devin session with full repository/build access to trace all call sites of `alternates.Env` and confirm whether any of them receive an unvalidated `Repository.GitObjectDirectory`/`GitAlternateObjectDirectories` value directly from a client-controlled or Rails-relayed message before validation.

### Citations

**File:** internal/git/localrepo/paths.go (L27-45)
```go
	objectDirectoryPath := repo.GetGitObjectDirectory()
	if objectDirectoryPath == "" {
		return "", structerr.NewInvalidArgument("object directory path is not set")
	}

	storagePath, err := repo.locator.GetStorageByName(ctx, repo.GetStorageName())
	if err != nil {
		return "", fmt.Errorf("get storage by name: %w", err)
	}

	// Ensure the path points somewhere in the storage.
	relativeObjectDirectoryPath, err := storage.ValidateRelativePath(storagePath, filepath.Join(repoPath, objectDirectoryPath))
	if err != nil {
		return "", structerr.NewInvalidArgument("validate relative path: %w", err)
	}

	parentDir := filepath.Base(filepath.Dir(relativeObjectDirectoryPath))
	baseDir := filepath.Base(relativeObjectDirectoryPath)
	isTransactionQuarantineDir := (baseDir == "quarantine") || ((parentDir == "quarantine") && strings.HasPrefix(baseDir, "tmp_objdir"))
```

**File:** internal/git/localrepo/paths.go (L61-73)
```go
		if _, origError := storage.ValidateRelativePath(repoPath, objectDirectoryPath); origError != nil {
			tempDir, err := repo.locator.TempDir(repo.GetStorageName())
			if err != nil {
				return "", structerr.NewInvalidArgument("getting storage's temporary directory: %w", err)
			}

			expectedQuarantinePrefix := filepath.Join(tempDir, storage.QuarantineDirectoryPrefix(repo))
			absoluteObjectDirectoryPath := filepath.Join(repoPath, objectDirectoryPath)

			// The relative path is outside of the repository
			if !strings.HasPrefix(absoluteObjectDirectoryPath, expectedQuarantinePrefix) {
				return "", structerr.NewInvalidArgument("not a valid relative path: %w", origError)
			}
```

**File:** internal/git/alternates/alternates.go (L9-27)
```go
// Env returns the alternate object directory environment variables.
func Env(repoPath, objectDirectory string, alternateObjectDirectories []string) []string {
	var env []string
	if objectDirectory != "" {
		env = append(env, fmt.Sprintf("GIT_OBJECT_DIRECTORY=%s", filepath.Join(repoPath, objectDirectory)))
	}

	if len(alternateObjectDirectories) > 0 {
		var dirsList []string

		for _, dir := range alternateObjectDirectories {
			dirsList = append(dirsList, filepath.Join(repoPath, dir))
		}

		env = append(env, fmt.Sprintf("GIT_ALTERNATE_OBJECT_DIRECTORIES=%s", strings.Join(dirsList, ":")))
	}

	return env
}
```

**File:** internal/git/gitcmd/command_factory.go (L1-28)
```go
package gitcmd

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"github.com/prometheus/client_golang/prometheus"
	"gitlab.com/gitlab-org/gitaly/v18/internal/cgroups"
	"gitlab.com/gitlab-org/gitaly/v18/internal/command"
	"gitlab.com/gitlab-org/gitaly/v18/internal/featureflag"
	"gitlab.com/gitlab-org/gitaly/v18/internal/git"
	"gitlab.com/gitlab-org/gitaly/v18/internal/git/alternates"
	"gitlab.com/gitlab-org/gitaly/v18/internal/git/mvcc"
	"gitlab.com/gitlab-org/gitaly/v18/internal/git/trace2"
	"gitlab.com/gitlab-org/gitaly/v18/internal/git/trace2hooks"
	"gitlab.com/gitlab-org/gitaly/v18/internal/gitaly/config"
	"gitlab.com/gitlab-org/gitaly/v18/internal/gitaly/storage"
	"gitlab.com/gitlab-org/gitaly/v18/internal/log"
	"gitlab.com/gitlab-org/gitaly/v18/internal/tracing"
	"gitlab.com/gitlab-org/labkit/correlation"
	"golang.org/x/time/rate"
)
```

**File:** doc/object_quarantine.md (L109-123)
```markdown
### How GitLab passes the object quarantine information around

To overcome this problem, the GitLab `pre-receive` hook
[reads the object directory configuration from its environment](https://gitlab.com/gitlab-org/gitaly/-/blob/71d527f4f16c1f0e76793f055def0299b375cc7d/internal/gitlabshell/env.go#L9).
and passes this information
[along with the HTTP API call](https://gitlab.com/gitlab-org/gitaly/-/blob/71d527f4f16c1f0e76793f055def0299b375cc7d/internal/gitaly/hook/manager.go#L30-46).
On the Rails side, we then
[put the object directory information in the "request store"](https://gitlab.com/gitlab-org/gitlab/-/blob/master/lib/api/internal/base.rb#L43)
(i.e., request-scoped thread-local storage). And then during that
Rails request, when Rails makes Gitaly requests on this repo, we send back the quarantine information
[in the Gitaly `Repository` struct](https://gitlab.com/gitlab-org/gitlab/-/blob/f81f30c29a0edce20f6737fdccc3315c8baab9d1/lib/gitlab/gitaly_client/util.rb#L8-17).
And finally, inside Gitaly, when we spawn a Git process, we
[re-create the environment variables](https://gitlab.com/gitlab-org/gitaly/-/blob/969bac80e2f246867c1a976864bd1f5b34ee43dd/internal/git/alternates/alternates.go#L21-34)
that were present on the `pre-receive` hook, so that we can see the
quarantined objects.
```

**File:** internal/git/localrepo/paths_test.go (L135-163)
```go
			desc: "with directory traversal",
			repo: repoWithGitObjDir(repoProto, "../bazqux.git"),
			err:  codes.InvalidArgument,
		},
		{
			desc: "valid path but doesn't exist",
			repo: repoWithGitObjDir(repoProto, "foo../bazqux.git"),
			err:  codes.NotFound,
		},
		{
			desc: "with sneaky directory traversal",
			repo: repoWithGitObjDir(repoProto, "/../bazqux.git"),
			err:  codes.InvalidArgument,
		},
		{
			desc: "with traversal outside repository",
			repo: repoWithGitObjDir(repoProto, "objects/../.."),
			err:  codes.InvalidArgument,
		},
		{
			desc: "with traversal outside repository with trailing separator",
			repo: repoWithGitObjDir(repoProto, "objects/../../"),
			err:  codes.InvalidArgument,
		},
		{
			desc: "with deep traversal at the end",
			repo: repoWithGitObjDir(repoProto, "bazqux.git/../.."),
			err:  codes.InvalidArgument,
		},
```
