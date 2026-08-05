Confirmed: `verify_messages_proof` in `bridges/modules/messages/src/proofs.rs` takes a `bridged_header_hash` chosen by the relayer at proof-construction time and looks it up via `HeaderChain::verify_storage_proof`, which resolves to `GrandpaChainHeaders::finalized_header_state_root` querying `ImportedHeaders::<T, I>::get(header_hash)` in `bridges/modules/grandpa/src/lib.rs`. This is the ring-buffer-backed analog of the Prover contract's `provenStates` lookup.

### Title
Bounded header ring-buffer in the bridge GRANDPA pallet lets an unprivileged submitter evict the header a pending message-proof relies on, denying message delivery - (File: `bridges/modules/grandpa/src/lib.rs`)

### Summary
The bridge GRANDPA light-client pallet stores only a fixed-size ring buffer (`ImportedHeaders`, bounded by `T::HeadersToKeep`) of previously finalized bridged-chain headers. `pallet-bridge-messages`' `verify_messages_proof`/`verify_messages_delivery_proof` (`bridges/modules/messages/src/proofs.rs`) bind a storage proof to a specific `bridged_header_hash` chosen when the relayer builds the proof off-chain. Between proof construction and on-chain inclusion, any unprivileged, unprivileged account can call the public `submit_finality_proof_ex` extrinsic with genuinely available (but not previously imported) finalized headers/justifications to advance the ring buffer and prune the exact header the pending message proof depends on, causing the message-proof transaction to fail with `HeaderChainError::UnknownHeader`. This mirrors the reported Prover contract issue: history is bounded, and a party racing to advance "latest" state invalidates in-flight proofs bound to now-stale (but still legitimately provable) state.

### Finding Description
`bridges/modules/grandpa/src/lib.rs` maintains:
- `BestFinalized` – latest imported header id.
- `ImportedHashes` / `ImportedHashesPointer` – a ring buffer of size `T::HeadersToKeep::get()`.
- `ImportedHeaders` – map from header hash to `StoredHeaderData`, pruned via `insert_header` (`lib.rs:704-720`) whenever the ring buffer wraps.

`insert_header` unconditionally removes the oldest entry in the buffer on every successful `submit_finality_proof_ex` call (`lib.rs:708-719`), regardless of whether some other in-flight transaction still needs that entry.

`submit_finality_proof_ex` (`lib.rs:284-363`) is callable by any `ensure_signed` origin, requires only that the header number is better than the on-chain "obsolete" check and a valid GRANDPA justification for the *already finalized* bridged chain — i.e., it does not require validator collusion, a compromised relayer, or any privileged role. Anyone who can fetch a valid justification for any not-yet-imported finalized header of the bridged chain (public information, obtainable from bridged-chain RPC) can submit it.

Meanwhile, `pallet-bridge-messages::verify_messages_proof` (`bridges/modules/messages/src/proofs.rs:46-102`) and `verify_messages_delivery_proof` (`proofs.rs:105-`) take a `bridged_header_hash` field embedded in the proof structure that the relayer selected when building the storage proof (i.e., some header that was the "best finalized" at construction time). This hash is passed to `HeaderChain::verify_storage_proof` (`bridges/primitives/header-chain/src/lib.rs:88-96`), which calls `finalized_header_state_root`, implemented for the GRANDPA pallet as `ImportedHeaders::<T, I>::get(header_hash)` (`grandpa/src/lib.rs:803-809`). If that specific hash has since been pruned from the bounded ring buffer, verification fails with `HeaderChainError::UnknownHeader`.

Existing guards do not prevent this:
- `HeadersToKeep` bounds storage growth but creates a small, deterministic eviction window.
- There is no mechanism ensuring a header referenced by an in-flight message proof stays available until that proof lands on-chain.
- `submit_finality_proof_ex` has no cool-down/rate limiting beyond `MaxFreeHeadersPerBlock` (which only affects fee refunds, not eviction).

An adversary can therefore pre-fetch a batch of legitimate, previously-unsubmitted finalized headers+justifications for the bridged chain (these accumulate naturally when nobody bothers importing every header, only enough to satisfy pending relays) and, upon observing a pending message-proof transaction in the mempool that references header H, rapidly submit `HeadersToKeep` many newer headers to push H out of the ring buffer before the message-proof transaction is included. The message-proof transaction then reverts, the relayer loses the transaction fee, and message delivery for that lane is delayed until a fresh proof (against a still-available header) is resubmitted — directly analogous to the reported "invalidate in-progress proof by submitting newer state" bug class.

### Impact Explanation
This degrades bridge message delivery: legitimate relayers can have their `receive_messages_proof`/delivery-proof transactions repeatedly invalidated, wasting fees and stalling processing of a lane's message queue — matching the "public underpriced work that degrades block production or stalls bridge processing" and message-queue-advancement guard in the impact gate. It does not lead to fund loss directly, but it enables a low-cost, unprivileged denial-of-service against bridge message throughput, and worst case (as noted in the original report) can cause message/nonce backlogs or force resubmission cycles.

### Likelihood Explanation
Likelihood is low-to-moderate: it requires (a) `HeadersToKeep` to be small enough relative to attacker capability to submit that many headers within one block/short window, and (b) the attacker to observe pending message-proof transactions (mempool visibility) and possess valid justifications for enough not-yet-imported headers. Both preconditions are realistic on public bridge deployments where headers are only imported "as needed" rather than continuously, leaving many legitimate finalized headers un-imported and available for the attacker to feed in rapid succession. No validator collusion, leaked keys, or privileged access is required — only an ordinary signed account and publicly available finality data, matching the "unprivileged attacker" requirement.

### Recommendation
- Do not prune ring-buffer entries purely by insertion order; instead track/allow references to any header still needed by known in-flight proofs, or increase `HeadersToKeep` significantly relative to expected header-submission cadence.
- Consider requiring message-proof submitters to be able to target "any header not older than X" rather than a single fixed hash, or let the messages pallet fall back to `BestFinalized` if the originally-referenced header was pruned, re-deriving/verifying against a currently available header.
- Add a minimum retention time (in addition to count) so a burst of legitimate header imports cannot evict recently used entries before pending consumers execute.
- Consider a "cool-down" between free/rapid submissions or scale `HeadersToKeep` based on realistic burst-import scenarios.

### Proof of Concept
1. Relayer A observes new messages on lane `L` and builds a storage proof against currently best-finalized header `H0` (hash `h0`), submitting `receive_messages_proof(proof{bridged_header_hash: h0, ...})` to the message pallet — this transaction sits in the mempool.
2. Attacker (any signed account) has pre-fetched `HeadersToKeep` (`T::HeadersToKeep::get()`) valid, not-yet-imported finalized headers `H1..Hn` (with valid GRANDPA justifications) for the bridged chain.
3. Attacker rapidly submits `submit_finality_proof_ex` for `H1, H2, ..., Hn` before A's transaction is included; each call invokes `insert_header` (`grandpa/src/lib.rs:704-720`), which evicts one old entry from `ImportedHeaders` per call.
4. Once `n >= HeadersToKeep`, `h0` is pruned from `ImportedHeaders`.
5. Relayer A's `receive_messages_proof` transaction now executes, calls `verify_messages_proof` → `HeaderChain::verify_storage_proof(h0, ...)` → `ImportedHeaders::get(h0)` returns `None` → `Err(HeaderChainError::UnknownHeader)` — matching the existing unit test `message_proof_is_rejected_if_header_is_missing_from_the_chain` (`bridges/modules/messages/src/proofs.rs:307-328`), which demonstrates this exact failure path is reachable and unguarded.
6. Relayer A's transaction fails, fee is spent, and message delivery for lane `L` is delayed pending a fresh proof — reproducing the "denial of service via invalidating in-progress proof by submitting newer state" pattern from the external report.