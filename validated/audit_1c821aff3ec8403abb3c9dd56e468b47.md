### Title
Cross-repository object exfiltration via unchecked `RemoteRepository` field in `UserRebaseConfirmable` - (File: internal/gitaly/service/operations/rebase_confirmable.go)

### Summary
`UserRebaseConfirmable` accepts a caller-supplied `RemoteRepository`/`RemoteBranch` pair that is used purely as a fetch source without any check that the requester is authorized to read from that repository. Objects fetched from the arbitrary source repository are merged into the quarantine of the caller's own (authorized) target repository and can be surfaced back to the caller through the RPC response and, upon `apply=true`, permanently persisted into the target repository's history.

### Finding Description
`UserRebaseConfirmable` only validates the presence of fields, not their relationship to the authenticated caller: `validateUserRebaseConfirmableHeader` checks that `RemoteRepository`/`RemoteBranch` are non-empty but never checks that the caller has any permission on `RemoteRepository`. [1](#0-0) 

The handler builds a `rebaseRemoteFetch` wrapper directly from client-supplied fields and passes it to `s.fetchStartRevision`, which fetches the referenced branch from `RemoteRepository` into the quarantine of the target repository: [2](#0-1) [3](#0-2) 

Only `header.Repository` (the local/target repository) is annotated `(target_repository)=true` in the proto definition and thus subject to Gitaly/Praefect routing and quarantine gating; `remote_repository` carries no such annotation and is treated as an arbitrary, unauthenticated pointer to any repository reachable by the Gitaly node: [4](#0-3) 

The commit produced by the rebase (containing objects merged in from the "remote" repository) is returned directly in the first stream response before any authorization hook runs, and if the second `apply=true` message is sent, `updateReferenceWithHooks` runs the standard hook chain only against the *target* repository — it never validates that the caller is entitled to read `RemoteRepository`: [5](#0-4) 

Because Gitaly's own authentication is a single shared secret between the API layer (e.g. GitLab Rails/Workhorse) and the Gitaly node rather than a per-repository ACL, any authenticated caller that can reach this RPC directly and control the `RemoteRepository` field can pull commit content, file trees, and metadata (author identity, commit messages, file diffs surfaced in conflict details) from a repository it has no access rights to, by rebasing its own branch onto that repository's branch and reading back the response / final commit in its own repo.

### Impact Explanation
An attacker who can invoke `UserRebaseConfirmable` with control of the `remote_repository`/`remote_branch` fields can:
- Exfiltrate arbitrary commit history, file contents, and metadata from a private repository they do not have read access to, by pointing `RemoteRepository` at it and rebasing.
- Persist the stolen objects into a repository under their own control by completing the two-phase RPC (`apply=true`), making the exfiltrated data durably retrievable via ordinary clone/fetch afterward.
- Learn sensitive information even without completing the apply, since conflicting-file names and the resulting rebase SHA/tree are echoed back in the first response before any target-repository access control is enforced.

This is a direct violation of repository isolation — the analog of the `_from`-controlled asset theft in the reported ERC-721 bug: a caller-controlled "source" identifier is used to pull assets (Git objects) without verifying the caller's right to that source.

### Likelihood Explanation
Reachability requires the caller to invoke the `OperationService/UserRebaseConfirmable` gRPC directly with a `Repository` it does control (satisfying the standard target-repository access check that Rails performs prior to normal usage) but an arbitrary `RemoteRepository` value. Since this field carries no additional authorization annotation and is not validated against the caller's permissions inside Gitaly itself, exploitation is possible by any party capable of making gRPC calls to Gitaly with a valid repository under their control (e.g. a compromised/malicious internal caller, or if input validation for this field is ever relaxed on the Rails side). It requires no exploitation of memory-safety bugs, credential theft, or MITM — it is a straightforward missing authorization check on a caller-supplied field.

### Recommendation
Before fetching from `RemoteRepository`, verify that the authenticated request context/user is authorized to read that repository (equivalent to running the same "allowed" access check that is performed for the target repository, or by ensuring `RemoteRepository` is constrained server-side to a value the caller is already known to have access to, e.g. only allowing rebase sources that are forks/related in a way already validated by the upstream authorization layer). At minimum, Gitaly should not treat `remote_repository` as an implicitly trusted parameter; it should require the same repository-scoped authorization gate applied to `target_repository` fields.

### Proof of Concept
1. Attacker controls `repo-attacker` (a repository they have legitimate push/rebase permission on) and knows the storage name/relative path of `repo-victim` (a private repository they cannot otherwise read).
2. Attacker calls `UserRebaseConfirmable` with:
   - `header.repository = repo-attacker`
   - `header.branch = "attacker-branch"`, `header.branch_sha = <current tip>`
   - `header.remote_repository = repo-victim`
   - `header.remote_branch = "main"` (or any branch name guessed/known to exist)
3. Gitaly quarantines `repo-attacker`, then fetches `refs/heads/main` from `repo-victim` into that quarantine via `fetchStartRevision`/`rebaseRemoteFetch`, and computes a rebase of `attacker-branch` onto the fetched revision.
4. The first response returns `rebase_sha`; conflict details (if any) reveal file paths from `repo-victim`. Reading the object via `CatFile`/`ReadCommit` in the quarantine before the second message is sent already leaks tree/commit content.
5. Sending `apply = true` merges the objects into `repo-attacker`'s branch, permanently copying the victim repository's commit content into a repository the attacker fully controls and can subsequently clone.

Note: Full confirmation that this RPC is invocable with attacker-controlled `RemoteRepository` in a production GitLab deployment (i.e., that GitLab Rails does not itself constrain the field before issuing the call) would require reviewing the GitLab Rails call sites, which are outside this repository's index; this analysis is based solely on the Gitaly-side implementation shown above, where no such constraint exists in Gitaly itself.

### Citations

**File:** internal/gitaly/service/operations/rebase_confirmable.go (L34-56)
```go
	quarantineDir, quarantineRepo, cleanup, err := s.quarantinedRepo(ctx, header.GetRepository())
	if err != nil {
		return structerr.NewInternal("creating repo quarantine: %w", err)
	}
	defer cleanup()

	objectHash, err := quarantineRepo.ObjectHash(ctx)
	if err != nil {
		return fmt.Errorf("detecting object hash: %w", err)
	}

	branch := git.NewReferenceNameFromBranchName(string(header.GetBranch()))
	oldrev, err := objectHash.FromHex(header.GetBranchSha())
	if err != nil {
		return structerr.NewNotFound("%w", err)
	}

	remoteFetch := rebaseRemoteFetch{header: header}
	startRevision, err := s.fetchStartRevision(ctx, quarantineRepo, remoteFetch)
	if err != nil {
		return structerr.NewInternal("%w", err)
	}

```

**File:** internal/gitaly/service/operations/rebase_confirmable.go (L94-119)
```go
	if err := stream.Send(&gitalypb.UserRebaseConfirmableResponse{
		UserRebaseConfirmableResponsePayload: &gitalypb.UserRebaseConfirmableResponse_RebaseSha{
			RebaseSha: newrev.String(),
		},
	}); err != nil {
		return structerr.NewInternal("send rebase sha: %w", err)
	}

	secondRequest, err := stream.Recv()
	if err != nil {
		return structerr.NewInternal("recv: %w", err)
	}

	if !secondRequest.GetApply() {
		return structerr.NewFailedPrecondition("rebase aborted by client")
	}

	if err := s.updateReferenceWithHooks(
		ctx,
		header.GetRepository(),
		header.GetUser(),
		quarantineDir,
		branch,
		newrev,
		oldrev,
		header.GetGitPushOptions()...,
```

**File:** internal/gitaly/service/operations/rebase_confirmable.go (L149-178)
```go
func validateUserRebaseConfirmableHeader(ctx context.Context, locator storage.Locator, header *gitalypb.UserRebaseConfirmableRequest_Header) error {
	if err := locator.ValidateRepository(ctx, header.GetRepository()); err != nil {
		return err
	}

	if header.GetUser() == nil {
		return errors.New("empty User")
	}

	if header.GetBranch() == nil {
		return errors.New("empty Branch")
	}

	if header.GetBranchSha() == "" {
		return errors.New("empty BranchSha")
	}

	if header.GetRemoteRepository() == nil {
		return errors.New("empty RemoteRepository")
	}

	if header.GetRemoteBranch() == nil {
		return errors.New("empty RemoteBranch")
	}

	if err := git.ValidateRevision(header.GetRemoteBranch()); err != nil {
		return ErrInvalidBranch
	}

	return nil
```

**File:** internal/gitaly/service/operations/rebase_confirmable.go (L181-202)
```go
// rebaseRemoteFetch is an intermediate type that implements the
// `requestFetchingStartRevision` interface. This allows us to use
// `fetchStartRevision` to get the revision to rebase onto.
type rebaseRemoteFetch struct {
	header *gitalypb.UserRebaseConfirmableRequest_Header
}

func (r rebaseRemoteFetch) GetRepository() *gitalypb.Repository {
	return r.header.GetRepository()
}

func (r rebaseRemoteFetch) GetBranchName() []byte {
	return r.header.GetBranch()
}

func (r rebaseRemoteFetch) GetStartRepository() *gitalypb.Repository {
	return r.header.GetRemoteRepository()
}

func (r rebaseRemoteFetch) GetStartBranchName() []byte {
	return r.header.GetRemoteBranch()
}
```

**File:** proto/operations.proto (L936-967)
```text
// UserRebaseConfirmableRequest is a request for the UserRebaseConfirmable RPC.
message UserRebaseConfirmableRequest {
  // Header contains information to compute the rebase and must be sent as
  // first message.
  message Header {
    // repository is the repository in which the rebase will be computed and
    // applied.
    Repository repository = 1 [(target_repository)=true];
    // user to execute the action as. Also used to perform authentication and
    // authorization via an external endpoint.
    User user = 2;
    // rebase_id does nothing anymore.
    string rebase_id = 3 [deprecated=true];
    // branch is the branch onto which the rebase shall happen.
    bytes branch = 4;
    // branch_sha is the expected object ID which branch currently points to.
    // This is used as a safety guard to avoid races when branch has been
    // updated meanwhile.
    string branch_sha = 5;
    // remote_repository is the repository which contains the branch which
    // shall be rebased onto the local branch.
    Repository remote_repository = 6;
    // remote_branch contains the branch name which shall re rebased onto the
    // local branch.
    bytes remote_branch = 7;
    // git_push_options contain options which shall be passed to the git hooks
    // when the local branch gets updated.
    repeated string git_push_options = 8;
    // timestamp is the optional timestamp to use for the rebased commits as
    // committer date. If it's not set, the current time will be used.
    google.protobuf.Timestamp timestamp = 9;
  }
```
