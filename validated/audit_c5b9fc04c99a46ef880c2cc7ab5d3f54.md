This confirms the finding: Gitaly has an explicit, dedicated mechanism (`git.InternalRefPrefixes` and `hiddenReceivePackRefPrefixes`) that hides/blocks writes to internal ref namespaces (`refs/merge-requests/`, `refs/pipelines/`, `refs/environments/`, `refs/keep-around/`, etc.) during `git-receive-pack` via `receive.hideRefs` configuration, verified by `TestPostReceivePack_hiddenRefs` which asserts a "deny updating a hidden ref" error for exactly these namespaces. `FetchBundle`, however, invokes `git-fetch` with the unconditional mirror refspec `+refs/*:refs/*` and does not apply any `receive.hideRefs`/`transfer.hideRefs`-equivalent restriction, so any ref name present in the attacker-supplied bundle is written to the destination repository verbatim. [1](#0-0) [2](#0-1) [3](#0-2) 

### Title
FetchBundle mirrors arbitrary ref namespaces (refs/keep-around, refs/merge-requests, refs/pipelines) bypassing the hideRefs restriction enforced on git-receive-pack - ([File: internal/git/localrepo/bundle.go])

### Summary
`Repo.FetchBundle` fetches an attacker-supplied bundle using the unconditional mirror refspec `git.MirrorRefSpec` (`+refs/*:refs/*`) without applying `receive.hideRefs`/`transfer.hideRefs`, the mechanism Gitaly otherwise uses to prevent writes to GitLab-internal ref namespaces during `git-receive-pack`. This allows any ref name embedded in the bundle — including `refs/keep-around/<sha>`, `refs/merge-requests/<n>/head`, `refs/pipelines/<n>`, `refs/environments/<n>` — to be written into the target repository.

### Finding Description
`FetchBundle` (`internal/gitaly/service/repository/fetch_bundle.go:44`) calls `repo.FetchBundle`, which configures an ad-hoc `inmemory` remote with `remote.inmemory.fetch = +refs/*:refs/*` (`internal/git/localrepo/bundle.go:166-169`) and runs `git fetch` via `FetchRemote`. Unlike `git-receive-pack`, which is invoked with `hiddenReceivePackRefPrefixes` to set `receive.hideRefs` for every prefix in `git.InternalRefPrefixes` (`internal/git/gitcmd/command_description.go:453-468`), the fetch path used by `FetchBundle`/`FetchRemote` applies no equivalent `transfer.hideRefs`/`fetch.hideRefs` guard. Because the bundle content (ref names and target OIDs) is fully attacker-controlled input assembled client-side before being streamed to the RPC, any ref name — including internal bookkeeping refs — is accepted and mirrored by `git-fetch` into the destination bare repository, overwriting existing internal refs or creating spoofed ones. `TestPostReceivePack_hiddenRefs` demonstrates the invariant that legitimate push paths reject exactly these namespaces (`refs/environments/1`, `refs/merge-requests/1/head`, `refs/merge-requests/1/merge`, `refs/pipelines/1`) with "deny updating a hidden ref" — a check `FetchBundle` never performs.

### Impact Explanation
If an attacker can invoke `FetchBundle` against a repository (or drive it indirectly through a feature that consumes attacker-influenced bundle content), they can overwrite or delete `refs/keep-around/*` refs that prevent premature garbage collection of merge-request diff base objects, causing data loss, or create/overwrite `refs/merge-requests/<n>/head` / `refs/pipelines/<n>` to spoof GitLab-internal state read by Rails via `ListRefs`/ref lookups. This matches a data-integrity/spoofing impact class (write-gating bypass, REPO_ISOLATION violation).

### Likelihood Explanation
Exploitability is entirely gated on whether `FetchBundle` is reachable with attacker-controlled bundle bytes. In this codebase, `FetchBundle` is used exclusively as an internal primitive for backup/restore (`internal/backup/repository.go`), not as a general user-facing mutator RPC invoked from ordinary push/fetch/import flows exposed to unprivileged GitLab users — I could not find any Rails-reachable, unprivileged-user-triggered call path into this RPC within the Gitaly repository (GitLab Rails source is out of scope/not indexed here). Absent confirmation that an unprivileged user can reach `FetchBundle` with a bundle they fully control, the precondition stated in the question is not established by the code available; the underlying refspec behavior is otherwise consistent with, and shared by, the already-existing `FetchRemote`/mirror-pull code paths (`getRefspecs` defaults to `refs/*:refs/*` as well) and `CloneBundle`, both of which have the same characteristic and are treated as intentional "mirror everything" semantics rather than a Gitaly-specific defect.

### Recommendation
If `FetchBundle` (or `FetchRemote`'s default `all_refs` mirroring) can ever be reached with bundle/remote content influenced by a non-trusted party, apply the same `hiddenReceivePackRefPrefixes`-equivalent guard (a `transfer.hideRefs`/`fetch.hideRefs` config built from `git.InternalRefPrefixes`) to the `git-fetch` invocation used in `internal/git/localrepo/bundle.go`'s `FetchBundle`, or filter/reject bundle ref names under `git.InternalRefPrefixes` before invoking `git-fetch`, mirroring the protection already present for `git-receive-pack`.

### Proof of Concept
Not provided — cannot be validated end-to-end because the calling context (whether/how an unprivileged GitLab user can reach `FetchBundle` with attacker-controlled bundle bytes) could not be confirmed from the Gitaly repository alone. A concrete PoC would extend `internal/gitaly/service/repository/fetch_bundle_test.go` by crafting a bundle (via `git bundle create` with `refs/keep-around/<oid>` and `refs/merge-requests/1/head` refs) and asserting that, unlike `TestPostReceivePack_hiddenRefs`, `FetchBundle` accepts and writes these refs unfiltered — but this only demonstrates the mechanism gap, not confirmed exploitability without an established unprivileged-reachable call path.

### Citations

**File:** internal/git/gitcmd/command_description.go (L453-468)
```go
func hiddenReceivePackRefPrefixes(ctx context.Context) []GlobalOption {
	config := make([]GlobalOption, 0, len(git.InternalRefPrefixes))

	for refPrefix, refType := range git.InternalRefPrefixes {
		switch refType {
		case git.InternalReferenceTypeReadonly, git.InternalReferenceTypeHidden:
			// We want to hide both read-only and hidden refs in git-receive-pack(1) so
			// that we make neither of them writeable.
			config = append(config, ConfigPair{Key: "receive.hideRefs", Value: refPrefix})
		default:
			panic(fmt.Sprintf("unhandled internal reference type: %v", refType))
		}
	}

	return config
}
```

**File:** internal/gitaly/service/smarthttp/receive_pack_test.go (L222-259)
```go
func TestPostReceivePack_hiddenRefs(t *testing.T) {
	t.Parallel()

	ctx := testhelper.Context(t)
	cfg := testcfg.Build(t)
	cfg.SocketPath = runSmartHTTPServer(t, cfg)
	testcfg.BuildGitalyHooks(t, cfg)

	client := newSmartHTTPClient(t, cfg.SocketPath, cfg.Auth.Token)

	for _, ref := range []string{
		"refs/environments/1",
		"refs/merge-requests/1/head",
		"refs/merge-requests/1/merge",
		"refs/pipelines/1",
	} {
		t.Run(ref, func(t *testing.T) {
			t.Parallel()

			repoProto, repoPath := gittest.CreateRepository(t, ctx, cfg)
			repoProto.GlProjectPath = "project/path"

			gittest.WriteCommit(t, cfg, repoPath, gittest.WithReference(ref))
			push := setupSimplePush(t, ctx, cfg, repoPath, git.ReferenceName(ref))

			stream, err := client.PostReceivePack(ctx)
			require.NoError(t, err)

			response := push.perform(t, stream, &gitalypb.PostReceivePackRequest{
				Repository:   repoProto,
				GlUsername:   "user",
				GlId:         "123",
				GlRepository: "project-456",
			})

			require.Contains(t, response, fmt.Sprintf("%s deny updating a hidden ref", ref))
		})
	}
```

**File:** internal/git/localrepo/bundle.go (L153-193)
```go
// FetchBundle fetches references from a bundle. Refs will be mirrored to the
// repository with the refspec "+refs/*:refs/*".
func (repo *Repo) FetchBundle(ctx context.Context, txManager transaction.Manager, reader io.Reader, opts *FetchBundleOpts) error {
	if opts == nil {
		opts = &FetchBundleOpts{}
	}

	bundlePath, cleanup, err := repo.createTempBundle(ctx, reader)
	if err != nil {
		return fmt.Errorf("fetch bundle: %w", err)
	}
	defer cleanup()

	fetchConfig := []gitcmd.ConfigPair{
		{Key: "remote.inmemory.url", Value: bundlePath},
		{Key: "remote.inmemory.fetch", Value: git.MirrorRefSpec},
	}
	fetchOpts := FetchOpts{
		CommandOptions: []gitcmd.CmdOpt{
			gitcmd.WithConfigEnv(fetchConfig...),
			// Starting in Git version 2.46.0, executing git-fetch(1) on a bundle performs fsck
			// checks when `transfer.fsckObjects` is enabled. Prior to this, this configuration was
			// always ignored and fsck checks were not run. Unfortunately, fsck message severity
			// configuration is ignored by Git only for bundle fetches. Until this is supported by
			// Git, disable `transfer.fsckObjects` so bundles containing fsck errors can continue to
			// be fetched. This matches behavior prior to Git version 2.46.0.
			gitcmd.WithConfig(gitcmd.ConfigPair{Key: "transfer.fsckObjects", Value: "false"}),
		},
	}
	if err := repo.FetchRemote(ctx, "inmemory", fetchOpts); err != nil {
		return fmt.Errorf("fetch bundle: %w", err)
	}

	if opts.UpdateHead {
		if err := repo.updateHeadFromBundle(ctx, txManager, bundlePath); err != nil {
			return fmt.Errorf("fetch bundle: %w", err)
		}
	}

	return nil
}
```
