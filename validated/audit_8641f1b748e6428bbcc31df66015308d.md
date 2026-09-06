### Title
Missing consensus-hash/signature verification when reusing a locally-cached block as the unconfirmed tenure-start block - (File: stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs, fn try_accept_tenure_info)

### Summary
In `try_accept_tenure_info`, when `chainstate.nakamoto_blocks_db().has_nakamoto_block_with_index_hash(&remote_tenure_tip.tenure_start_block_id)` returns true, the code immediately loads that block from the local DB and assigns it to `self.unconfirmed_tenure_start_block` without checking that the block's `consensus_hash` matches `remote_tenure_tip.consensus_hash` or that its signer signatures are valid for the claimed reward set. This is inconsistent with the sibling code path `try_accept_unconfirmed_tenure_start_block`, which performs exactly these checks (`verify_signer_signatures` and `consensus_hash` equality) before accepting a freshly-downloaded block.

### Finding Description
`remote_tenure_tip` is a fully attacker-controlled `RPCGetTenureInfo` response returned to an unauthenticated peer-to-peer/RPC download request (`/v3/tenure/info`). The function validates `remote_tenure_tip.consensus_hash` and `remote_tenure_tip.parent_tenure_start_block_id` against canonical sortition state [1](#0-0) , but it never validates `remote_tenure_tip.tenure_start_block_id` itself against any burnchain-committed value in the fast path. When `has_nakamoto_block_with_index_hash` finds a locally-stored block under that exact `StacksBlockId` (which happens whenever the ID equals any previously-processed/legitimate block the node already has, since `StacksBlockId` is a keyed cryptographic hash of `(consensus_hash, header)` and is not something the attacker needs to brute-force — merely reuse a publicly-known ID), the code fetches that block and assigns it as the ongoing tenure's start block without any equality check against `remote_tenure_tip.consensus_hash`: [2](#0-1) 

Contrast this with the explicit-fetch path, which enforces both the signer-signature check and the consensus-hash equality before acceptance: [3](#0-2) 

The earlier "highest_processed_block_id" fast-path shortcut at lines 259-288 has the identical gap — it also fetches `remote_tenure_tip.tenure_start_block_id` from the local DB and stores it unconditionally as `unconfirmed_tenure_start_block`: [4](#0-3) 

### Impact Explanation
The mis-set `self.unconfirmed_tenure_start_block` is subsequently consulted by `need_highest_complete_tenure`, which decides whether the state machine still needs to fetch/validate the highest-complete (confirmed) tenure by checking whether a header for that block ID already exists locally: [5](#0-4) 
Because the block came from an unrelated, already-stored tenure (not authenticated against the claimed `remote_tenure_tip.consensus_hash`), this check trivially returns "already have it," causing the node to skip issuing a `make_highest_complete_tenure_downloader` request for the real confirmed tenure it is missing. This is a sync-stalling / false-negative bug rather than a state-corruption or forged-propagation bug: the mismatched block is never written into chainstate under a new/wrong identity (its `StacksBlockId` doesn't change), and the eventual `NakamotoTenureDownloader` (when it is constructed) still independently re-validates any newly-fetched blocks against signer signatures and chain linkage. The concretely demonstrable effect is a malicious peer being able to make the unconfirmed-tenure downloader believe it has already synced the highest-complete tenure when it has not, stalling that peer's contribution to sync progress for the affected download attempt.

### Likelihood Explanation
The attacker only needs to run/control a single peer that answers `/v3/tenure/info` requests (no privileged role, no secret, remote-reachable by any P2P/RPC connection) and to reference, in the `tenure_start_block_id` field, any `StacksBlockId` that the victim node has already legitimately stored (which is knowable/public, not secret). The `consensus_hash` / `parent_consensus_hash` / `parent_tenure_start_block_id` fields can be set to real, currently-canonical values to pass the earlier checks at lines 176-257, while `tenure_start_block_id` is set independently to the reused ID. This is inexpensive and repeatable per request/session.

### Recommendation
In both fast-path branches (lines 273-286 and 338-357), after loading the block via `get_nakamoto_block(&remote_tenure_tip.tenure_start_block_id)`, add the same checks performed in `try_accept_unconfirmed_tenure_start_block`: verify `block.header.consensus_hash == remote_tenure_tip.consensus_hash` and, where applicable, `verify_signer_signatures` against the appropriate reward set, before assigning to `self.unconfirmed_tenure_start_block` / transitioning to `Done`/`GetUnconfirmedTenureBlocks`.

### Proof of Concept
1. In a `stackslib` net test harness, populate `chainstate` with a legitimately-signed `NakamotoBlock` `B` from tenure `A` (consensus_hash `CH_A`), so it is retrievable via `get_nakamoto_block`/`has_nakamoto_block_with_index_hash` under `block_id(B)`.
2. Construct a `RPCGetTenureInfo` `remote_tenure_tip` with real canonical `consensus_hash`/`parent_consensus_hash`/`parent_tenure_start_block_id` (satisfying checks at lines 176-257) but with `tenure_start_block_id = block_id(B)` (from unrelated tenure `A`).
3. Call `NakamotoUnconfirmedTenureDownloader::try_accept_tenure_info(...)` with this crafted tip.
4. Assert that `self.unconfirmed_tenure_start_block.unwrap().header.consensus_hash != remote_tenure_tip.consensus_hash` — proving the accepted block's tenure identity does not match the claimed ongoing tenure identity, i.e., the canonicity/equality invariant is broken at this call site (lines 338-357), unlike `try_accept_unconfirmed_tenure_start_block` which would reject this same mismatch (lines 440-445).

### Citations

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs (L248-257)
```rust
        // parent tenure start block ID must be the winning block hash for the ongoing tenure's
        // snapshot
        if local_tenure_sn.winning_stacks_block_hash.0
            != remote_tenure_tip.parent_tenure_start_block_id.0
        {
            debug!("Ongoing tenure does not commit to highest complete tenure's start block. Treating remote peer {} as stale.", &self.naddr;
                  "remote_tenure_tip.tenure_start_block_id" => %remote_tenure_tip.parent_tenure_start_block_id,
                  "local_tenure_sn.winning_stacks_block_hash" => %local_tenure_sn.winning_stacks_block_hash);
            return Err(NetError::StaleView);
        }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs (L273-286)
```rust
            if &remote_tenure_tip.tip_block_id == highest_processed_block_id
                || highest_processed_block_height > remote_tenure_tip.tip_height
            {
                // nothing to do -- we're at or ahead of the remote peer, so finish up.
                // If we don't have the tenure-start block for the confirmed tenure that the remote
                // peer claims to have, then the remote peer has sent us invalid data and we should
                // treat it as such.
                let unconfirmed_tenure_start_block = chainstate
                    .nakamoto_blocks_db()
                    .get_nakamoto_block(&remote_tenure_tip.tenure_start_block_id)?
                    .ok_or(NetError::InvalidMessage)?
                    .0;
                self.unconfirmed_tenure_start_block = Some(unconfirmed_tenure_start_block);
                self.state = NakamotoUnconfirmedDownloadState::Done;
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs (L338-357)
```rust
        if chainstate
            .nakamoto_blocks_db()
            .has_nakamoto_block_with_index_hash(&remote_tenure_tip.tenure_start_block_id.clone())?
        {
            // proceed to get unconfirmed blocks. We already have the tenure-start block.
            let unconfirmed_tenure_start_block = chainstate
                .nakamoto_blocks_db()
                .get_nakamoto_block(&remote_tenure_tip.tenure_start_block_id)?
                .ok_or_else(|| {
                    debug!(
                        "No such tenure-start Nakamoto block {}",
                        &remote_tenure_tip.tenure_start_block_id
                    );
                    NetError::DBError(DBError::NotFoundError)
                })?
                .0;
            self.unconfirmed_tenure_start_block = Some(unconfirmed_tenure_start_block);
            self.state = NakamotoUnconfirmedDownloadState::GetUnconfirmedTenureBlocks(
                remote_tenure_tip.tip_block_id.clone(),
            );
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs (L417-445)
```rust
        if let Err(e) = unconfirmed_tenure_start_block
            .header
            .verify_signer_signatures(unconfirmed_signer_keys, epoch_id)
        {
            warn!("Invalid tenure-start block: bad signer signature";
                  "tenure_start_block.header.consensus_hash" => %unconfirmed_tenure_start_block.header.consensus_hash,
                  "tenure_start_block.header.block_id" => %unconfirmed_tenure_start_block.header.block_id(),
                  "state" => %self.state,
                  "error" => %e);
            return Err(NetError::InvalidMessage);
        }

        // block has to match the expected hash
        if tenure_start_block_id != &unconfirmed_tenure_start_block.header.block_id() {
            warn!("Invalid tenure-start block";
                  "tenure_id_start_block" => %tenure_start_block_id,
                  "unconfirmed_tenure_start_block.header.consensus_hash" => %unconfirmed_tenure_start_block.header.consensus_hash,
                  "unconfirmed_tenure_start_block ID" => %unconfirmed_tenure_start_block.header.block_id(),
                  "state" => %self.state);
            return Err(NetError::InvalidMessage);
        }

        // furthermore, the block has to match the expected tenure ID
        if unconfirmed_tenure_start_block.header.consensus_hash != tenure_tip.consensus_hash {
            warn!("Invalid tenure-start block or tenure-tip: consensus hash mismatch";
                  "tenure_start_block.header.consensus_hash" => %unconfirmed_tenure_start_block.header.consensus_hash,
                  "tenure_tip.consensus_hash" => %tenure_tip.consensus_hash);
            return Err(NetError::InvalidMessage);
        }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs (L639-658)
```rust
    pub fn need_highest_complete_tenure(
        &self,
        chainstate: &StacksChainState,
    ) -> Result<bool, NetError> {
        if self.state != NakamotoUnconfirmedDownloadState::Done {
            return Err(NetError::InvalidState);
        }
        let Some(unconfirmed_tenure_start_block) = self.unconfirmed_tenure_start_block.as_ref()
        else {
            return Err(NetError::InvalidState);
        };

        // if we've processed the unconfirmed tenure-start block already, then we've necessarily
        // downloaded and processed the highest-complete tenure already.
        Ok(!NakamotoChainState::has_block_header(
            chainstate.db(),
            &unconfirmed_tenure_start_block.header.block_id(),
            false,
        )?)
    }
```
