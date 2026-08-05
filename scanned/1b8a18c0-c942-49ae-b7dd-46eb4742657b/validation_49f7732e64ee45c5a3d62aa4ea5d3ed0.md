### Title
Content-only duplicate check in `HopDataPool::insert` lets an attacker permanently squat a hash and deny legitimate hand-offs - (File: `substrate/client/hop/src/pool.rs`)

### Summary
`sc-hop`'s data pool deduplicates submissions purely by `blake2_256(data)`, with no binding to sender, recipients, or any other distinguishing field. This is the same root-cause pattern as the reported beacon-kit bug: a duplicate check keyed only on *content* rather than on the tuple of (content, distinguishing context) that actually identifies a unique, legitimate submission. In the original report, two distinct sidecars with equal KZG commitments (equal blob content) were wrongly treated as illegal duplicates and caused a chain halt. Here, the same mis-keyed dedup logic allows one submitter to permanently occupy a content hash, causing every other honest user's semantically distinct submission (different sender, different recipients) with the same bytes to be rejected as `DuplicateEntry`.

### Finding Description
`HopDataPool::insert` computes `let hash = H256(blake2_256(&data));` and then rejects the call outright if that hash already exists in the in-memory index, regardless of who is submitting or who the intended recipients are: [1](#0-0) 

The design is explicitly documented as content-addressed with duplicates rejected at submit time: [2](#0-1) 

and surfaced to callers as a stable, expected error code: [3](#0-2) 

There is no per-sender or per-recipient component mixed into the hash, and the pool's per-user quota / rate limiting is charged and then unwound on the duplicate path, but the *entry itself* is never allowed to coexist for two different (sender, recipients) pairs that happen to produce the same bytes. Any RPC caller can call `hop_submit` with attacker-chosen, predictable, or low-entropy payloads (e.g. all-zero padding, common template bytes, or any data that a legitimate user is expected to later submit — a signed message envelope, a well-known constant, a re-used payload) and permanently occupy that hash for the duration of `--hop-retention-secs` (24h by default). Every subsequent legitimate submitter whose real hand-off payload happens to collide with the squatted bytes is rejected with `DuplicateEntry`, even though their `signer`, `recipients`, and intent are completely different from the original submitter's. The attacker can also re-submit right after each expiry window to keep the squat alive indefinitely.

This is structurally identical to the external bug: a `map[content_hash] -> struct{}` (or here, `HashMap<HopHash, HopEntryMeta>`) is used as the sole gate for "is this a duplicate," while the actual invariant that should be enforced is "is this the *same intended submission*," which requires binding additional context (sender/recipient in this case, sidecar index in the BeaconKit case).

### Impact Explanation
An unprivileged, unauthenticated (or minimally authenticated — only a valid signature over arbitrary chosen data is required) actor can deny the `hop_submit` service to any other user whose payload collides with an attacker-chosen hash. Because the hash space is fully attacker-controlled (they pick their own `data`), this is not a probabilistic collision attack — it's a direct griefing primitive: pre-submit the exact bytes you expect a target/legitimate user to submit (predictable templates, zero-padding, common constants, or replayed public data) and their real submission is unconditionally rejected for up to the retention period. This degrades the "hand-off" data-availability path a collator offers to users and can be sustained indefinitely by re-squatting after each expiry, i.e. a persistent public denial-of-service against a specific data-availability feature of the node with no protocol-level way for the victim to recover other than choosing different bytes (not always possible if the payload format is fixed/predictable).

### Likelihood Explanation
High. The attack requires no privileged role, no validator/collator/relayer position, and no leaked keys — just an RPC connection and the ability to sign a `HOP_SUBMIT_CONTEXT` payload over self-chosen data, which is exactly what `hop_submit` is designed to accept from any caller. The only friction is the per-account rate limiter, but that limits the *attacker's own* submission rate, not the exposure window (24h retention by default) during which the squatted hash blocks everyone else.

### Recommendation
Do not use the raw content hash alone as the pool's uniqueness key. Bind the entry key (or the duplicate-detection tuple) to include the submitting `sender_id`/`signer` (and/or the recipient set), analogous to the report's fix of accepting duplicate KZG commitments once differentiated by sidecar index. Concretely, key the index by `(blake2_256(data), sender_id)` — or, if content-addressing must remain global for storage dedup, keep the on-disk blob store content-addressed but track *submissions* (sender, recipients, timestamp) as a separate, non-colliding record so distinct legitimate hand-offs can coexist even when their payload bytes match.

### Proof of Concept
1. Attacker computes/guesses bytes `D` that a victim is expected to submit later (e.g. a fixed template message, an all-zero-padded envelope, or any publicly known constant payload used by the hand-off application).
2. Attacker calls `hop_submit(D, attacker_recipients, attacker_signature, attacker_signer, now)` — this succeeds and inserts `hash = blake2_256(D)` into `HopDataPool`'s index.
3. Victim later calls `hop_submit(D, victim_recipients, victim_signature, victim_signer, now)` with the *same bytes* but entirely different `recipients`/`signer`.
4. `HopDataPool::insert` at `substrate/client/hop/src/pool.rs:393-398` finds `hash` already present and returns `HopError::DuplicateEntry`, unconditionally rejecting the victim's legitimate, distinct hand-off — even though its sender and recipients have nothing to do with the attacker's entry.
5. Attacker repeats step 2 immediately after each expiry (`--hop-retention-secs`) to keep the squat alive indefinitely, permanently denying the victim's use of that payload on this collator.

### Citations

**File:** substrate/client/hop/src/pool.rs (L389-399)
```rust
		let hash = H256(blake2_256(&data));

		// First duplicate check (read lock only).
		{
			let index = self.index.lock();
			if index.contains_key(&hash) {
				self.release_user_quota(&sender_id, accounted);
				self.current_size.fetch_sub(accounted, Ordering::Relaxed);
				return Err(HopError::DuplicateEntry);
			}
		}
```

**File:** substrate/client/hop/README.md (L19-20)
```markdown
- **Content-addressed** — entries are keyed by `blake2_256(data)`; duplicates
  are rejected at submit time.
```

**File:** substrate/client/hop/README.md (L199-199)
```markdown
| 1003 | `DuplicateEntry` | A blob with this hash is already in the pool |
```
