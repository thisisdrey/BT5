### Title
Missing-inventory entries treated as "already processed" in `need_unconfirmed_tenures`, letting a single lying inv peer force premature `Confirmed → Unconfirmed` transition and skip a canonical tenure - (File: `stackslib/src/net/download/nakamoto/download_state_machine.rs`)

### Summary
`NakamotoDownloadStateMachine::run_downloads` gates the `Confirmed → Unconfirmed` transition on `self.tenure_downloads.is_empty() && self.fetch_unconfirmed_tenures`, where `fetch_unconfirmed_tenures` comes from `need_unconfirmed_tenures`. That function's per-tenure availability check treats "no neighbor reported anything about this tenure" as equivalent to "already processed," so a single (only) inventory-providing peer that simply omits the highest canonical tenure from its bitvector can make the state machine believe every wanted tenure is satisfied even though that tenure was never scheduled or downloaded.

### Finding Description
`self.wanted_tenures` is built from the canonical sortition DB via `load_wanted_tenures_at_tip`/`load_wanted_tenures`, so it correctly lists every canonical tenure. However, whether a wanted tenure gets a download attempt at all depends entirely on peer-supplied inventory:

- `update_available_tenures` (lines 717-864) computes `self.available_tenures` and `self.tenure_block_ids` purely from `inventories` (the `NakamotoTenureInv` bitvectors reported by peers). `make_ibd_download_schedule`/`make_rarest_first_download_schedule` (lines 641-689) silently `continue` (skip) any `wt` for which `!available.contains_key(&wt.tenure_id_consensus_hash)` — i.e. a tenure that no peer's inventory claims to have is simply never put in `tenure_download_schedule`, and therefore `NakamotoTenureDownloaderSet::make_tenure_downloaders` never creates a downloader for it. `self.tenure_downloads` (the actual `NakamotoTenureDownloaderSet`) can thus be empty even though a canonical tenure remains unfetched.

- The gate that is supposed to prevent transitioning to `Unconfirmed` before all canonical confirmed tenures are staged is `need_unconfirmed_tenures` (lines 929-1013). It loops over `wanted_tenures.iter().chain(prev_wanted_tenures.iter())` and computes:
```rust
let is_available_and_processed = tenure_block_ids.iter().any(|(_, available)| {
    if let Some(tenure_start_end) = available.get(&wt.tenure_id_consensus_hash) {
        tenure_start_end.processed
    } else {
        true
    }
});
```
If the only neighbor in `tenure_block_ids` has no entry at all for `wt.tenure_id_consensus_hash` (because its inventory never advertised that tenure), the closure returns `true` via the `else` branch, and `.any()` is satisfied. This makes the withheld tenure look "processed" even though it was never downloaded, staged, or even scheduled.

- Consequently, in `run_downloads` (Confirmed branch, lines 1488-1513), `self.tenure_downloads.is_empty()` is true (no downloader was ever created for the withheld tenure) and `self.fetch_unconfirmed_tenures` is true (per the flawed check above), so the state transitions from `Confirmed` to `Unconfirmed`.

- Once in `Unconfirmed` mode, `run_unconfirmed_downloaders`/`NakamotoUnconfirmedTenureDownloader` only fetches the ongoing tenure and its immediate parent (`tenure_tip.parent_consensus_hash`, see `make_highest_complete_tenure_downloader`, `tenure_downloader_unconfirmed.rs` lines 710-757). A tenure withheld from inventory that is *not* the immediate parent of the tip (e.g., one reward cycle behind, per the question's scenario) is never picked up by this path either. The node is permanently left without that canonical tenure's blocks.

No existing guard catches this: `try_accept_tenure_info` and the unconfirmed/confirmed downloaders do validate consensus hashes against the local sortition DB once a peer actually serves data, but nothing forces the state machine to demand at least one advertisement for *every* canonical wanted tenure before concluding it's "processed."

### Impact Explanation
This lets a single malicious or misconfigured peer that is the node's sole inventory source silently steer the victim node off the canonical tip: the node commits to `Unconfirmed`/steady-state operation while a canonical confirmed tenure is permanently skipped, stalling block processing one tenure behind the true chain tip. This matches the High-severity category "steering a node off the tip via false inventory" — it is a false/omitted inventory claim (not requiring any signature or auth bypass) that causes the node's local state to diverge from canonical chain history. It is repeatable on every IBD/steady-state cycle as long as the attacker continues to omit the tenure from its advertised inventory.

### Likelihood Explanation
- Preconditions: the attacker peer must be the node's only inbound/outbound neighbor supplying Nakamoto tenure inventories (`tenure_block_ids`/`inventories` populated solely from this peer) — a realistic scenario for a node with few peers or one recently (re)joining the network.
- Attacker cost: trivial — just omit one bit from the tenure inventory bitvector it gossips; no cryptographic forgery, no privileged role, no secret required.
- Remote reachability: inventories are exchanged over the ordinary P2P/inv-sync protocol reachable by any connecting peer.
- Repeatable indefinitely as long as the attacker remains the dominant/only inv source.

### Recommendation
In `need_unconfirmed_tenures`, do not treat "no data from this neighbor for this tenure" as "processed." Instead, require positive confirmation from the canonical sortition DB / local chainstate (e.g., check `wt.processed`, which is populated from local processing state, rather than defaulting to `true` when a neighbor's `tenure_block_ids` lacks an entry) that the tenure has actually been staged/processed before it can be excluded from the "still need this" check. Likewise, before transitioning `Confirmed → Unconfirmed`, cross-check that `tenure_downloads.is_empty()` is only trusted when every entry in `self.wanted_tenures`/`self.prev_wanted_tenures` is either marked `processed` in local chainstate or has been observed as available from more than a single, possibly-adversarial inventory source (e.g. require corroboration from multiple peers, or require the peer set advertising availability to be non-trivial before concluding a canonical tenure is unobtainable).

### Proof of Concept
Rust test plan (net test, e.g. added to `stackslib/src/net/tests/download/nakamoto.rs`):
1. Build a test sortition DB/peer with N canonical Nakamoto tenures (`make_nakamoto_peer_from_invs`), producing `wanted_tenures` of length N via `NakamotoDownloadStateMachine::load_wanted_tenures_at_tip`.
2. Construct a single neighbor's `NakamotoTenureInv` bitvector that has all bits set except the bit for the highest-indexed (most recent) canonical tenure (simulating the "one reward cycle behind" adversarial inventory).
3. Feed this into `NakamotoDownloadStateMachine::update_available_tenures` with only this one neighbor's inventory in `inventories`, then call `need_unconfirmed_tenures` directly with the resulting `tenure_block_ids`/`available_tenures`.
4. Assert `need_unconfirmed_tenures(...) == true` even though the highest tenure's `ConsensusHash` never appears as a key in `tenure_block_ids`'s per-neighbor `AvailableTenures` map (i.e., it was never advertised/downloaded).
5. Drive `NakamotoDownloadStateMachine::run_downloads`/`run` end-to-end and assert `self.state == NakamotoDownloadState::Unconfirmed` while asserting that the withheld tenure's `ConsensusHash` never appears in any completed download (`tenure_downloads.completed_tenures` or returned `HashMap<ConsensusHash, Vec<NakamotoBlock>>`), confirming the canonical tenure was silently skipped. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** stackslib/src/net/download/nakamoto/download_state_machine.rs (L641-689)
```rust
    /// Produce a download schedule for IBD mode.  Tenures will be downloaded in sortition order.
    /// The first item will be fetched first.
    pub(crate) fn make_ibd_download_schedule(
        nakamoto_start: u64,
        wanted_tenures: &[WantedTenure],
        available: &HashMap<ConsensusHash, Vec<NeighborAddress>>,
    ) -> VecDeque<ConsensusHash> {
        let mut schedule = VecDeque::new();
        for wt in wanted_tenures.iter() {
            if wt.processed {
                continue;
            }
            if wt.burn_height < nakamoto_start {
                continue;
            }
            if !available.contains_key(&wt.tenure_id_consensus_hash) {
                continue;
            }
            schedule.push_back(wt.tenure_id_consensus_hash.clone());
        }
        schedule
    }

    /// Produce a download schedule for steady-state mode.  Tenures will be downloaded in
    /// rarest-first order.
    /// The first item will be fetched first.
    pub(crate) fn make_rarest_first_download_schedule(
        nakamoto_start: u64,
        wanted_tenures: &[WantedTenure],
        available: &HashMap<ConsensusHash, Vec<NeighborAddress>>,
    ) -> VecDeque<ConsensusHash> {
        let mut schedule = Vec::with_capacity(available.len());
        for wt in wanted_tenures.iter() {
            if wt.processed {
                continue;
            }
            if wt.burn_height < nakamoto_start {
                continue;
            }
            let Some(neighbors) = available.get(&wt.tenure_id_consensus_hash) else {
                continue;
            };
            schedule.push((neighbors.len(), wt.tenure_id_consensus_hash.clone()));
        }

        // order by fewest neighbors first
        schedule.sort_by(|a, b| a.0.cmp(&b.0));
        schedule.into_iter().map(|(_count, ch)| ch).collect()
    }
```

**File:** stackslib/src/net/download/nakamoto/download_state_machine.rs (L962-1012)
```rust
        let (unconfirmed_tenure_opt, confirmed_tenure_opt) = Self::find_unconfirmed_tenure_ids(
            wanted_tenures,
            prev_wanted_tenures,
            available_tenures,
        );
        debug!(
            "Check unconfirmed tenures: highest two available tenures are {:?}, {:?}",
            &unconfirmed_tenure_opt, &confirmed_tenure_opt
        );

        // see if we need any tenures still
        for wt in wanted_tenures.iter().chain(prev_wanted_tenures.iter()) {
            debug!("Check unconfirmed tenures: check {:?}", &wt);
            let is_available_and_processed = tenure_block_ids.iter().any(|(_, available)| {
                if let Some(tenure_start_end) = available.get(&wt.tenure_id_consensus_hash) {
                    tenure_start_end.processed
                } else {
                    true
                }
            });

            if !is_available_and_processed {
                let is_unconfirmed = unconfirmed_tenure_opt
                    .as_ref()
                    .map(|ch| *ch == wt.tenure_id_consensus_hash)
                    .unwrap_or(false)
                    || confirmed_tenure_opt
                        .as_ref()
                        .map(|ch| *ch == wt.tenure_id_consensus_hash)
                        .unwrap_or(false);

                if is_unconfirmed {
                    debug!(
                        "Tenure {} is only available via the unconfirmed tenure downloader",
                        &wt.tenure_id_consensus_hash
                    );
                    continue;
                }

                // a tenure is available but not yet processed, so we can't yet transition to
                // fetching unconfirmed tenures (we'd have no way to validate them).
                // TODO: also check that this cannot be fetched by confirmed downloader
                debug!(
                    "Tenure {} is available but not yet processed",
                    &wt.tenure_id_consensus_hash
                );
                return false;
            }
        }

        true
```

**File:** stackslib/src/net/download/nakamoto/download_state_machine.rs (L1488-1513)
```rust
        match self.state {
            NakamotoDownloadState::Confirmed => {
                let new_blocks = self.download_confirmed_tenures(
                    network,
                    chainstate,
                    usize::try_from(network.get_connection_opts().max_inflight_blocks)
                        .expect("FATAL: max_inflight_blocks exceeds usize::MAX"),
                );

                if self.tenure_downloads.is_empty() && self.fetch_unconfirmed_tenures {
                    debug!(
                        "Transition from {} to {}",
                        &self.state,
                        NakamotoDownloadState::Unconfirmed
                    );

                    self.unconfirmed_tenure_download_schedule =
                        Self::make_unconfirmed_tenure_download_schedule(
                            &network.chain_view,
                            network.iter_peer_convos(),
                        );
                    self.state = NakamotoDownloadState::Unconfirmed;
                }

                return new_blocks;
            }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs (L710-757)
```rust
    /// Create a NakamotoTenureDownloader for the highest complete tenure.  We already have the
    /// tenure-end block (which will be supplied to the downloader), but we'll still want to go get
    /// its tenure-start block.
    ///
    /// Returns Ok(downloader) on success
    /// Returns Err(..) if we call this function out of sequence.
    pub fn make_highest_complete_tenure_downloader(
        &self,
    ) -> Result<NakamotoTenureDownloader, NetError> {
        if self.state != NakamotoUnconfirmedDownloadState::Done {
            return Err(NetError::InvalidState);
        }
        let Some(tenure_tip) = &self.tenure_tip else {
            return Err(NetError::InvalidState);
        };
        let Some(confirmed_signer_keys) = self.confirmed_signer_keys.as_ref() else {
            return Err(NetError::InvalidState);
        };
        let Some(unconfirmed_signer_keys) = self.unconfirmed_signer_keys.as_ref() else {
            return Err(NetError::InvalidState);
        };

        info!(
            "Create highest confirmed downloader from unconfirmed";
            "confirmed_tenure" => %tenure_tip.parent_consensus_hash,
            "neighbor" => %self.naddr,
        );

        // The highest-complete tenure downloader validates the parent
        // (confirmed) tenure's blocks, so it uses that tenure's epoch. Fall back
        // to the lenient pre-4.0 rule if unset, which can never drop a valid block.
        let epoch_id = self.confirmed_epoch_id.unwrap_or(StacksEpochId::Epoch34);

        let ntd = NakamotoTenureDownloader::new(
            tenure_tip.parent_consensus_hash.clone(),
            tenure_tip.consensus_hash.clone(),
            tenure_tip.parent_tenure_start_block_id.clone(),
            tenure_tip.consensus_hash.clone(),
            tenure_tip.tenure_start_block_id.clone(),
            self.naddr.clone(),
            confirmed_signer_keys.clone(),
            unconfirmed_signer_keys.clone(),
            epoch_id,
            true,
        );

        Ok(ntd)
    }
```
