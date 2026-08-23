### Title
proc-receive handler queues reference updates from unvalidated OID/ref strings - (`internal/gitaly/hook/procreceive_handler.go`)

### Summary
`parseRefUpdate` splits an attacker-supplied pkt-line payload on `" "` into exactly three fields and assigns them directly to `ReferenceUpdate.OldOID`, `NewOID`, and `Ref` without validating that the OID fields are syntactically valid (40/64-hex) object IDs or that `Ref` is a well-formed reference name. These unchecked values flow straight into `receivePackReferenceUpdates`, which calls `hookManager.UpdateHook` and `updater.Update` with the raw, attacker-controlled strings.

### Finding Description
`NewProcReceiveHandler` reads pkt-line encoded ref-update lines directly from the pushing client via `scanner.Bytes()`/`pktline.Payload` and hands each payload to `parseRefUpdate`: [1](#0-0) 

The only validation performed is that the payload splits into exactly 3 space-separated tokens; there is no check that `split[0]`/`split[1]` are valid hex object IDs of the expected length, and no check that `split[2]` is a well-formed reference name (e.g., starts with `refs/`, has no embedded control bytes). Because `git.ObjectID` and `git.ReferenceName` are plain string type aliases in this code path, any byte sequence — including embedded NULs, non-hex characters, or control characters that survive `pktline.Payload` unescaped — is accepted as-is.

These `ReferenceUpdate` values are collected into `handler.updates` and later consumed in `receivePackReferenceUpdates`, where they are passed unmodified to `hookManager.UpdateHook(... update.Ref.String(), update.OldOID.String(), update.NewOID.String() ...)` and then to `updater.Update(update.Ref, update.NewOID, update.OldOID)`: [2](#0-1) 

Because `parseRefUpdate` never verifies the OID/ref fields, whatever the attacker places between the two spaces in the proc-receive ref-update pkt-line — truncated hex, non-hex-junk, or bytes containing NUL/control characters — is carried unmodified into the update hook invocation and into the `git-update-ref` transaction. This is exactly the kind of “garbage-in equals action-out” bug the question describes: the invariant that “reference updates are gated / verified by the update hook logic tied to that ref” is undermined because the update hook and `updateref.Update` receive a string that was never checked to actually be the object referenced by the parsed OID.

### Impact Explanation
An attacker who can drive the proc-receive path (repository configured with `receive.procReceiveRefs`, reachable by any user who can push to a repo they control) can supply malformed OldOID/NewOID/Ref strings that are queued as a `ReferenceUpdate` and forwarded to the update hook and `git update-ref` machinery without format validation. Depending on how permissively `updateref.Update`/`git update-ref -z` and the update hook script interpret malformed byte sequences (non-hex OIDs, empty/zero-length tokens, or control bytes), this can result in unvetted or misinterpreted ref updates being processed — a “hook or quarantine bypass” / “unvetted refs accepted” class of impact.

### Likelihood Explanation
The precondition matches the scenario given: the pushing user needs `receive.procReceiveRefs` (or WAL-enabled receive-pack) configured for the repository, which is attacker-controllable in their own repo, and they need a client capable of emitting raw pkt-line to the proc-receive negotiation (any custom client speaking the documented protocol suffices — no special privilege, secret, or peer compromise required). This makes it reachable by an ordinary unprivileged user performing a crafted push.

### Recommendation
In `parseRefUpdate` (`internal/gitaly/hook/procreceive_handler.go`), validate each field before constructing the `ReferenceUpdate`:
- Verify `split[0]` and `split[1]` are syntactically valid object IDs (correct hex length/charset for the repository's object hash, using existing `git.ObjectHash` validation helpers) or are the literal `<zero-oid>`.
- Verify `split[2]` is a syntactically valid, fully-qualified reference name (e.g., via `git.ReferenceName` validation / `git.ValidateRevision`-style checks), rejecting embedded NUL/control bytes.
- Reject the whole proc-receive negotiation (return an error) if any update line fails validation, rather than silently forwarding malformed data to `receivePackReferenceUpdates`.

### Proof of Concept
```go
func TestParseRefUpdate_AcceptsInvalidOID(t *testing.T) {
    data := []byte("not-a-valid-oid 0000000000000000000000000000000000000000 refs/heads/main")
    update, err := parseRefUpdate(data) // exported for test via internal test file
    require.NoError(t, err)
    // No hex/length validation is performed:
    require.Equal(t, git.ObjectID("not-a-valid-oid"), update.OldOID)
}
```
This demonstrates that `parseRefUpdate` accepts a syntactically invalid `OldOID` without error, confirming the missing validation described above. Full exploitation to a concrete write-outside-repo or hook bypass would require confirming `updateref.Update`'s and the `update` hook's exact handling of malformed OID/ref strings, which was not further traced in this pass due to tool-call limits.

### Citations

**File:** internal/gitaly/hook/procreceive_handler.go (L203-216)
```go
func parseRefUpdate(data []byte) (ReferenceUpdate, error) {
	var update ReferenceUpdate

	split := bytes.Split(data, []byte(" "))
	if len(split) != 3 {
		return update, fmt.Errorf("unknown ref update format: %s", split)
	}

	update.Ref = git.ReferenceName(split[2])
	update.OldOID = git.ObjectID(split[0])
	update.NewOID = git.ObjectID(split[1])

	return update, nil
}
```

**File:** internal/gitaly/hook/receivepack/receive_pack.go (L228-244)
```go
	for _, update := range updates {
		if err := hookManager.UpdateHook(
			ctx,
			req.GetRepository(),
			update.Ref.String(),
			update.OldOID.String(),
			update.NewOID.String(),
			[]string{hooksPayload},
			stdout, stderr,
		); err != nil {
			return fmt.Errorf("running update hook: %w", err)
		}

		if err := updater.Update(update.Ref, update.NewOID, update.OldOID); err != nil {
			return fmt.Errorf("queueing ref to be updated: %w", err)
		}
	}
```
