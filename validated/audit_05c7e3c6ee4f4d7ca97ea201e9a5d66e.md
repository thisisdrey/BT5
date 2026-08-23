### Title
FetchRemote bypasses receive-pack's hidden-ref protection, allowing mirror fetches to write into internal ref namespaces (refs/keep-around, refs/merge-requests, refs/environments, refs/pipelines, refs/remotes, refs/tmp) - ([File: internal/gitaly/service/repository/fetch_remote.go])

### Summary
`fetchRemoteAtomic` applies reference updates returned by `git fetch --dry-run --porcelain` directly via a raw `updateref.Updater`, without ever invoking `git-receive-pack` or its hidden-ref-prefix protection, and with hooks explicitly disabled. Because `getRefspecs`/`buildCommandOpts` let the caller set `remote.inmemory.fetch` to `refs/*:refs/*` via `MirrorRefmaps: ["all_refs"]`, an attacker-controlled remote can serve refs under GitLab-internal namespaces (e.g. `refs/keep-around/<oid>`, `refs/merge-requests/<n>/head`) that get written straight into the target repository's ref database.

### Finding Description
`buildCommandOpts` (`internal/gitaly/service/repository/fetch_remote.go:260-304`) builds `remote.inmemory.fetch` config from `getRefspecs(req.GetRemoteParams().GetMirrorRefmaps())`. With `MirrorRefmaps: ["all_refs"]` (or no refmaps at all), the refspec is `refs/*:refs/*` [1](#0-0) , meaning every ref the attacker's remote advertises — with no prefix restriction — is fetched.

`fetchRemoteAtomic` then runs the fetch with `DryRun`/`Porcelain` and parses the resulting reference updates from `gitcmd.NewFetchPorcelainScanner`, queuing every non-failed/unchanged status directly onto a raw `updateref.Updater` created via `updateref.New(ctx, quarantineRepo)` [2](#0-1)  and applying it with `refUpdater.Update(git.ReferenceName(status.Reference), status.NewOID, status.OldOID)` [3](#0-2) , followed directly by `Commit()` [4](#0-3) .

This path is materially different from a normal push through `git-receive-pack`, where `hiddenReceivePackRefPrefixes` injects Git config that makes receive-pack reject updates to hidden ref prefixes — verified by `TestPostReceivePack_hiddenRefs`, which asserts `"%s deny updating a hidden ref"` for `refs/environments/1`, `refs/merge-requests/1/head`, `refs/merge-requests/1/merge`, and `refs/pipelines/1` [5](#0-4) . The fetch/update-ref path used by `FetchRemote` never invokes `git-receive-pack`, so this protection never applies. Additionally, hooks are fully disabled for the underlying fetch (`DisableTransactions: true` documented as intentionally skipping the reference-transaction hook until `git-update-ref` runs) [6](#0-5) , and the raw `updateref.Updater` used here does not run pre-receive/update hooks at all (unlike `UpdaterWithHooks.UpdateReference`, which explicitly gates each update through pre-receive/update/reference-transaction hooks) [7](#0-6) .

Gitaly's own `InternalRefPrefixes` map documents `refs/keep-around/`, `refs/remotes/`, `refs/tmp/` as hidden and `refs/merge-requests/`, `refs/environments/`, `refs/pipelines/` as internal/readonly [8](#0-7) , confirming these are meant to be write-protected from ordinary ref update paths — a protection that `FetchRemote`'s raw ref-update path does not enforce.

### Impact Explanation
An attacker who can invoke `FetchRemote` against a repository they control (e.g. configuring a pull mirror, or any Rails flow that calls Gitaly's `FetchRemote` on their own project) can point `RemoteParams.Url` at a Git server they control and set `MirrorRefmaps: ["all_refs"]`. The crafted remote can serve refs like `refs/keep-around/<oid>`, `refs/merge-requests/<n>/head`, `refs/merge-requests/<n>/merge`, `refs/environments/<n>`, `refs/pipelines/<n>`, `refs/remotes/*`, or `refs/tmp/*` pointing to attacker-chosen commits. These get written into the target repository unfiltered, bypassing the same hidden-ref denial that a normal `git push` would hit. This can corrupt GitLab-managed metadata refs (used by GitLab Rails to resolve merge request diffs, keep-around retention, environment/pipeline provenance, or internal remote-tracking state), potentially causing GitLab Rails to trust attacker-forged content as if it were internally generated, and can pollute or spoof `refs/keep-around` retention refs used to prevent GC of specific objects. This matches "hook/ACL bypass allowing unvetted refs" impact class.

### Likelihood Explanation
The precondition is limited: the attacker must be able to invoke `FetchRemote` (typically via configuring a pull mirror on a repository they administer) and control the remote URL/content and `MirrorRefmaps`. No special role, secret, or Gitaly/Praefect compromise is required — this matches a standard unprivileged GitLab user workflow (setting up a repository mirror pointed at a URL they control). The exploit is fully reproducible and deterministic: `getRefspecs` will always emit `refs/*:refs/*` for `all_refs`, and the raw `updateref.Updater` path always skips hidden-ref/receive-pack config and hooks.

### Recommendation
In `fetchRemoteAtomic`, before queuing each `status.Reference` onto `refUpdater`/`prunedUpdater`, filter/reject reference names that fall under `git.InternalRefPrefixes` (or otherwise route the reference-update application for `FetchRemote` through the same hidden-ref-prefix denial/hook-gated path used by `git-receive-pack`/`UpdaterWithHooks`), so that fetched remote refs cannot land in GitLab-internal namespaces regardless of the RPC entry point.

### Proof of Concept
```go
func TestFetchRemote_CanWriteHiddenRefs(t *testing.T) {
    ctx := testhelper.Context(t)
    cfg, client := setupRepositoryServiceWithoutRepo(t) // existing test harness

    // Attacker-controlled remote repo exposing internal-namespace refs.
    _, remoteRepoPath := gittest.CreateRepository(t, ctx, cfg)
    commitID := gittest.WriteCommit(t, cfg, remoteRepoPath, gittest.WithBranch("main"))
    gittest.Exec(t, cfg, "-C", remoteRepoPath, "update-ref",
        "refs/keep-around/"+commitID.String(), commitID.String())
    gittest.Exec(t, cfg, "-C", remoteRepoPath, "update-ref",
        "refs/merge-requests/999/head", commitID.String())

    // Victim/attacker-owned target repo.
    repoProto, repoPath := gittest.CreateRepository(t, ctx, cfg)

    _, err := client.FetchRemote(ctx, &gitalypb.FetchRemoteRequest{
        Repository: repoProto,
        RemoteParams: &gitalypb.Remote{
            Url:           "file://" + remoteRepoPath,
            MirrorRefmaps: []string{"all_refs"},
        },
    })
    require.NoError(t, err)

    // Expect this to fail (hidden ref should be denied) - currently it will
    // succeed, demonstrating the bypass.
    out := gittest.Exec(t, cfg, "-C", repoPath, "for-each-ref", "refs/keep-around", "refs/merge-requests")
    require.Empty(t, out, "hidden/internal refs should not be writable via FetchRemote")
}
```
Expected (fixed) behavior: the internal-namespace refs are rejected/filtered and never appear in the target repository. Current behavior: `refs/keep-around/<oid>` and `refs/merge-requests/999/head` are created in the target repository, contrasted with `TestPostReceivePack_hiddenRefs` which shows the same ref prefixes are denied when pushed via `git-receive-pack`.

### Citations

**File:** internal/gitaly/service/repository/fetch_remote.go (L60-68)
```go
		// Transactions are disabled during fetch operation because no references are updated when
		// the dry-run option is enabled. Instead, the reference-transaction hook is performed
		// during the subsequent execution of `git-update-ref(1)`.
		DisableTransactions: true,
		// When the `dry-run` option is used with `git-fetch(1)`, Git objects are received without
		// performing reference updates. This is used to quarantine objects on the initial fetch and
		// migration to occur only during reference update.
		DryRun: true,
		// The `porcelain` option outputs reference update information from `git-fetch(1) to stdout.
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L146-154)
```go
	refUpdater, err := updateref.New(ctx, quarantineRepo)
	if err != nil {
		return false, false, fmt.Errorf("spawning ref updater: %w", err)
	}
	defer func() {
		if err := refUpdater.Close(); err != nil && returnedErr == nil {
			returnedErr = fmt.Errorf("cancel ref updater: %w", err)
		}
	}()
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L190-206)
```go
		// Queue all other reference updates in the same transaction.
		default:
			if err := refUpdater.Update(git.ReferenceName(status.Reference), status.NewOID, status.OldOID); err != nil {
				return false, false, fmt.Errorf("queueing ref to be updated: %w", err)
			}
			referencesUpdated = true

			// While scanning reference updates, check if any tags changed.
			if wereTagsChanged(status) {
				tagsChanged = true
			}

			// While scanning reference updates, check if repo was changed.
			if changeTypes[status.Type] {
				repoChanged = true
			}
		}
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L233-236)
```go
	// Commit the remaining queued reference updates so the changes get applied.
	if err := refUpdater.Commit(); err != nil {
		return false, false, fmt.Errorf("committing reference update: %w", err)
	}
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L322-334)
```go
func getRefspecs(refmaps []string) []string {
	if len(refmaps) == 0 {
		return []string{"refs/*:refs/*"}
	}

	refspecs := make([]string, 0, len(refmaps))

	for _, refmap := range refmaps {
		switch refmap {
		case "all_refs":
			// with `all_refs`, the repository is equivalent to the result of `git clone --mirror`
			refspecs = append(refspecs, "refs/*:refs/*")
		case "heads":
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

**File:** internal/gitaly/hook/updateref/update_with_hooks.go (L224-247)
```go
	if err := u.hookManager.PreReceiveHook(ctx, quarantinedRepo, pushOptions, []string{hooksPayload}, strings.NewReader(changes), &stdout, &stderr); err != nil {
		return fmt.Errorf("running pre-receive hooks: %w", wrapHookError(err, gitcmd.PreReceiveHook, stdout.String(), stderr.String()))
	}

	// Now that Rails has told us that the change is okay via the pre-receive hook, we can
	// migrate any potentially quarantined objects into the main repository. This must happen
	// before we start updating the refs because git-update-ref(1) will verify that it got all
	// referenced objects available.
	if quarantineDir != nil {
		if err := quarantineDir.Migrate(ctx); err != nil {
			return fmt.Errorf("migrating quarantined objects: %w", err)
		}

		// We only need to update the hooks payload to the unquarantined repo in case we
		// had a quarantine environment. Otherwise, the initial hooks payload is for the
		// real repository anyway.
		hooksPayload, err = gitcmd.NewHooksPayload(ctx, u.cfg, repoProto, objectHash, transaction, &receiveHooksPayload, gitcmd.ReceivePackHooks, featureflag.FromContext(ctx), storage.ExtractTransactionID(ctx)).Env()
		if err != nil {
			return fmt.Errorf("constructing quarantined hooks payload: %w", err)
		}
	}

	if err := u.hookManager.UpdateHook(ctx, quarantinedRepo, reference.String(), oldrev.String(), newrev.String(), []string{hooksPayload}, &stdout, &stderr); err != nil {
		return fmt.Errorf("running update hooks: %w", wrapHookError(err, gitcmd.UpdateHook, stdout.String(), stderr.String()))
```

**File:** internal/git/reference.go (L45-70)
```go
// InternalRefPrefixes is an array of all reference prefixes which are used internally by GitLab.
// These need special treatment in some cases, e.g. to restrict writing to them.
var InternalRefPrefixes = map[string]InternalReferenceType{
	// Environments may be interesting to the user in case they want to figure out what exact
	// reference an environment has been constructed from.
	"refs/environments/": InternalReferenceTypeReadonly,

	// Keep-around references are only used internally to keep alive objects, and thus they
	// shouldn't be exposed to the user.
	"refs/keep-around/": InternalReferenceTypeHidden,

	// Merge request references should be readable by the user so that they can still fetch the
	// changes of specific merge requests.
	"refs/merge-requests/": InternalReferenceTypeReadonly,

	// Pipelines may be interesting to the user in case they want to figure out what exact
	// reference a specific pipeline has been running with.
	"refs/pipelines/": InternalReferenceTypeReadonly,

	// Remote references shouldn't typically exist in repositories nowadays anymore, and there
	// is no reason to expose them to the user.
	"refs/remotes/": InternalReferenceTypeHidden,

	// Temporary references are used internally by Rails for various operations and should not
	// be exposed to the user.
	"refs/tmp/": InternalReferenceTypeHidden,
```
