### Title
Client-controlled `GitAlternateObjectDirectories`/`GitObjectDirectory` fields bypass transaction-based repository isolation, enabling cross-repository object access - (File: internal/gitaly/storage/storagemgr/middleware.go)

### Summary
Gitaly's transaction middleware treats any incoming repository-scoped RPC request whose `Repository` message already has `git_object_directory` or `git_alternate_object_directories` populated as a "loop-back" request from GitLab Rails' access-check flow (which normally re-supplies quarantine information), and deliberately skips starting a normal transaction/snapshot for it. Because these are ordinary, client-settable protobuf fields on every `Repository` message, a caller can set them directly on a request instead of relying on Gitaly to populate them from a real quarantine, which routes the request onto a non-transactional path that later feeds the (arbitrary) directory strings directly into `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` for the spawned Git process without validating that the resolved paths stay within the target repository or even the same storage.

### Finding Description
This mirrors the report's underlying bug class: a security-relevant invariant (bypassing the normal transactional/quarantine check) is enforced based on a piece of state that the caller itself controls and can freely set, rather than a control Gitaly derives itself. In the LSSVMPair report, `onlyOwner` was checked before/after a batched multicall, but an untrusted intermediate could transiently spoof ownership to smuggle a privileged action through. In Gitaly, the middleware infers "this is a Rails access-check loop-back with a legitimate quarantine" purely from whether `GetGitObjectDirectory()`/`GetGitAlternateObjectDirectories()` are non-empty on the request: [1](#0-0) 

The code comment explicitly acknowledges the field is spoofable and that doing so lets a caller "circumvent the transaction management by configuring either of the object directories," and states this is "left unaddressed for now": [2](#0-1) 

Both fields are ordinary, always-present fields on the `Repository` proto that ships with every RPC, with no server-side provenance/signature attached distinguishing a Gitaly-generated quarantine path from an attacker-supplied one: [3](#0-2) 

Once a request takes this path, execution eventually reaches the Git command factory, which joins the repository path with the caller-supplied alternate directory strings and injects them as environment variables for the spawned `git` process, with no boundary/traversal check performed at this stage: [4](#0-3) [5](#0-4) 

While mutating RPCs (`OpMutator`) are explicitly rejected when these fields are set (`ErrQuarantineConfiguredOnMutator`), accessor RPCs are not, and are instead routed through `restoreSnapshotRelativePath` onto a non-transactional request path, meaning normal transaction/snapshot boundaries and their associated repository-scoping guarantees are skipped for such calls: [6](#0-5) 

The `git/dirs.go` traversal check (`alternateOutsideStorageError`) that Gitaly normally uses when parsing a repository's own on-disk `objects/info/alternates` file is not applied to this client-supplied RPC field/environment-variable path, so the usual defense-in-depth for alternates does not cover this route: [7](#0-6) 

### Impact Explanation
If a caller can invoke an accessor RPC (e.g. any read RPC that accepts a `Repository`) while directly populating `git_object_directory`/`git_alternate_object_directories` with a relative path that escapes the target repository (e.g. `../other-repo/objects`), Gitaly will configure `GIT_ALTERNATE_OBJECT_DIRECTORIES` to point at another repository's object store and skip the normal transactional isolation/validation that would otherwise apply. This can result in cross-repository object disclosure — reading blob/commit/tree content that the caller is not authorized to access via the repository they nominally have access to — which is one of the accepted high-impact categories (cross-repository object access via a crafted RPC field).

### Likelihood Explanation
The precondition is narrow: an actor must be able to submit a Gitaly RPC with these two fields set to attacker-chosen values, which in production is typically mediated by GitLab Rails/Workhorse populating them only from a legitimate Gitaly-issued quarantine. However, the fields are ordinary, unauthenticated proto fields with no cryptographic binding to a real quarantine, and the code comment itself documents that this can be exploited to "circumvent the transaction management." Any component or caller that can reach Gitaly's gRPC surface with a valid repository-scoped Accessor RPC and control over the `Repository` message (e.g., a compromised or overly-trusted intermediate, or any client library that forwards Repository protos without stripping these fields) can trigger this without needing to compromise or impersonate the primary/owner.

### Recommendation
Do not infer "legitimate quarantine" purely from the presence of `git_object_directory`/`git_alternate_object_directories` on an inbound request. Instead, bind quarantine information to server-generated, unforgeable state (e.g., an opaque transaction/quarantine token minted by Gitaly and validated on loop-back, rather than accepting attacker-suppliable directory strings verbatim). Additionally, validate that any resolved `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` paths remain within the expected repository/storage root before injecting them as Git subprocess environment variables, mirroring the existing `alternateOutsideStorageError` boundary check used for on-disk alternates files.

### Proof of Concept
1. As a caller able to submit a Gitaly Accessor RPC (e.g. `GetObjectDirectorySize`, or similar) that carries a `Repository` message, request access to a repository `A` that the caller can legitimately reach.
2. Populate the request's `Repository.git_alternate_object_directories` with a value such as `../../<storage>/<victim-repo>/objects` (a relative path escaping repository `A`).
3. Because `beginTransactionForRepository` detects the non-empty alternate-directory field and treats the request as an already-quarantined loop-back, it skips creating a fresh transaction/snapshot and does not re-validate the resulting directories, per [1](#0-0) .
4. The eventual Git subprocess is spawned with `GIT_ALTERNATE_OBJECT_DIRECTORIES` set to the attacker-chosen path via [4](#0-3)  and [8](#0-7) , allowing the RPC to read/report on objects belonging to the victim repository instead of (or in addition to) repository `A`.

### Citations

**File:** internal/gitaly/storage/storagemgr/middleware.go (L271-297)
```go
	if targetRepo.GetGitObjectDirectory() != "" || len(targetRepo.GetGitAlternateObjectDirectories()) > 0 {
		// The object directories should only be configured on a repository coming from a request that
		// was already configured with a quarantine directory and is being looped back to Gitaly from Rails'
		// authorization checks. If that's the case, the request should already be running in scope of a
		// transaction and the repository rewritten to point to the snapshot repository. We thus don't start
		// a new transaction if we encounter this.
		//
		// This property is violated in tests which manually configure the object directory or the alternate
		// object directory. This allows for circumventing the transaction management by configuring the either
		// of the object directories. We'll leave this unaddressed for now and later address this by removing
		// the options to configure object directories and alternates in a request.

		if methodInfo.Operation == protoregistry.OpMutator {
			// Accessor requests may come with quarantine configured from Rails' access checks. Since the
			// RPC that triggered these access checks would already run in a transaction and target a
			// snapshot, we won't start another one. Mutators however are rejected to prevent writes
			// unintentionally targeting the main repository.
			return transactionalizedRequest{}, ErrQuarantineConfiguredOnMutator
		}

		rewrittenReq, err := restoreSnapshotRelativePath(ctx, methodInfo, req)
		if err != nil {
			return transactionalizedRequest{}, fmt.Errorf("restore snapshot relative path: %w", err)
		}

		return nonTransactionalRequest(ctx, rewrittenReq), nil
	}
```

**File:** proto/shared.proto (L56-64)
```text
  // relative_path ...
  string relative_path = 3;
  // git_object_directory sets the GIT_OBJECT_DIRECTORY envvar on git commands to the value of this field.
  // It influences the object storage directory the SHA1 directories are created underneath.
  string git_object_directory = 4;
  // git_alternate_object_directories sets the GIT_ALTERNATE_OBJECT_DIRECTORIES envvar on git commands to
  // the values of this field. It influences the list of Git object directories which can be used to search
  // for Git objects.
  repeated string git_alternate_object_directories = 5;
```

**File:** internal/git/gitcmd/command_factory.go (L509-520)
```go
	}

	var repoPath string
	if repo != nil {
		var err error
		repoPath, err = cf.locator.GetRepoPath(ctx, repo)
		if err != nil {
			return nil, err
		}

		env = append(alternates.Env(repoPath, repo.GetGitObjectDirectory(), repo.GetGitAlternateObjectDirectories()), env...)
	}
```

**File:** internal/git/alternates/alternates.go (L1-27)
```go
package alternates

import (
	"fmt"
	"path/filepath"
	"strings"
)

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

**File:** internal/git/dirs.go (L74-86)
```go
	for _, newDir := range strings.Split(string(alternates), "\n") {
		if len(newDir) == 0 || newDir[0] == '#' {
			continue
		}

		if !filepath.IsAbs(newDir) {
			newDir = filepath.Join(objDir, newDir)
		}

		if !strings.HasPrefix(newDir, storagePrefix) {
			return nil, alternateOutsideStorageError(newDir)
		}

```
