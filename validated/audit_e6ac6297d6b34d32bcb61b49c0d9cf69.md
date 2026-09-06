### Title
`need_unconfirmed_tenures` false-positive via `.any()` over `tenure_block_ids` lets a single malicious peer force premature Confirmed→Unconfirmed transition - ([File: stackslib/src/net/download/nakamoto/download_state_machine.rs])

### Summary
`NakamotoDownloadStateMachine::need_unconfirmed_tenures` computes `is_available_and_processed` with `tenure_block_ids.iter().any(...)`, where the closure returns `true` whenever a given peer's `AvailableTenures` map simply lacks an entry for the wanted tenure's consensus hash. Because this is an `.any()` over *every* tracked peer, a single malicious peer whose `TenureStartEnd` computation omits the targeted tenure (a normal outcome of `TenureStartEnd::from_inventory` when the bit-scan `break`s before locating a start/end pair) can force `is_available_and_processed = true` for that tenure even though no peer has actually delivered/processed it, overriding every honest peer's correct `processed:false` report.

### Finding Description
The relevant code: [1](#0-0) 

```
for wt in wanted_tenures.iter().chain(prev_wanted_tenures.iter()) {
    let is_available_and_processed = tenure_block_ids.iter().any(|(_, available)| {
        if let Some(tenure_start_end) = available.get(&wt.tenure_id_consensus_hash) {
            tenure_start_end.processed
        } else {
            true
        }
    });
```

`tenure_block_ids: HashMap<NeighborAddress, AvailableTenures>` is populated per-peer by `find_tenure_block_ids` / `TenureStartEnd::from_inventory` [2](#0-1) . `from_inventory` sets a tenure's bit as "available", but only inserts a `TenureStartEnd` entry into that peer's map if it can locate a subsequent tenure-start bit and a following tenure-end bit; otherwise it `break`s out of the loop without inserting the entry for that (and any later) tenure index [3](#0-2) .

Consequently, a remote peer can advertise a `NakamotoTenureInv` bitvector where the targeted tenure's bit is set but no further bits are set within the same/adjacent reward cycle window, causing `from_inventory` to `break` and silently omit that tenure from its own `AvailableTenures` map. When `need_unconfirmed_tenures` later iterates `tenure_block_ids` over **all** tracked peers, this single peer's missing entry causes the closure to evaluate `else { true }`, and since `.any()` only needs one `true`, this overrides all other (honest) peers' entries that correctly report `processed: false`. The equality the question describes — `is_available_and_processed == true` should only hold if some peer's `TenureStartEnd.processed == true` — is broken by design: an "unknown" entry is conflated with "processed."

This causes `need_unconfirmed_tenures` to return `true` for a tenure that was never fetched, which becomes `self.fetch_unconfirmed_tenures = true` in `run_downloads` [4](#0-3) . If `self.tenure_downloads` (the confirmed-tenure downloader set) happens to be empty — which can occur when the same missing-`TenureStartEnd` condition prevents `make_tenure_downloaders` from turning schedule entries into active downloaders — the state machine transitions from `Confirmed` to `Unconfirmed` prematurely [5](#0-4) , abandoning pending confirmed-tenure fetches in favor of unconfirmed/tip chasing.

### Impact Explanation
A remote, unprivileged peer that the victim node has merely connected to (no authentication or special role required) can poison the victim's tenure-completeness bookkeeping for a specific tenure by crafting its advertised `NakamotoTenureInv` bit pattern. This causes the node's IBD download state machine to falsely believe all confirmed tenures are available-and-processed, prematurely switching to `Unconfirmed` mode and abandoning the confirmed-tenure backlog, stalling the node's progress toward the canonical chain tip during IBD. This matches the High-severity category "steering a node off the tip via false inventory."

### Likelihood Explanation
The attacker only needs to run one ordinary P2P peer that the victim has an inventory exchange with; no secrets, no privileged role, and no majority of peers are required, since a single `true` from `.any()` overrides all honest peers' correct data. The precondition is a specific inventory bit shape (a bit set for the target tenure with no locatable subsequent start/end bits), which is achievable by a legitimate-looking, partially-synced inventory and thus easy and repeatable to construct. The attack is cheap (a handful of crafted inv messages) and repeatable across each `need_unconfirmed_tenures` check cycle.

### Recommendation
Change the logic so that an absent entry in a peer's `AvailableTenures` map is treated as "no information" rather than "processed." `is_available_and_processed` should be computed from the authoritative `available_tenures` map (whether the tenure is available at all) combined with checking that at least one peer's *present* `TenureStartEnd` entry for that specific tenure has `processed == true`, rather than defaulting missing entries to `true` inside an `.any()`. E.g., only count `available.get(&wt.tenure_id_consensus_hash)` results that exist, and require the aggregate check to be based on `.all()`/`.any()` semantics that don't let "no data" masquerade as "processed."

### Proof of Concept
Rust unit test (extends existing tests around `need_unconfirmed_tenures` in `stackslib/src/net/tests/download/nakamoto.rs`):
1. Construct `wanted_tenures` containing a `WantedTenure` `wt` with `processed: false` for consensus hash `CH`.
2. Construct `tenure_block_ids: HashMap<NeighborAddress, AvailableTenures>` with a single peer entry whose `AvailableTenures` map does **not** contain the key `CH` (simulating a peer whose `TenureStartEnd::from_inventory` broke before reaching `CH`, matching the real bit patterns that make `from_inventory` `break` early per `tenure.rs` lines 160-184).
3. Call `NakamotoDownloadStateMachine::need_unconfirmed_tenures(burnchain_height, &sort_tip, &wanted_tenures, &prev_wanted_tenures, &tenure_block_ids, &available_tenures)`.
4. Assert the function returns `true`, even though `CH` was never actually downloaded/processed by any peer — demonstrating the broken equality at `download_state_machine.rs:975-981`.

### Citations

**File:** stackslib/src/net/download/nakamoto/download_state_machine.rs (L615-639)
```rust
    pub(crate) fn find_tenure_block_ids<'a>(
        rc: u64,
        wanted_tenures: &[WantedTenure],
        next_wanted_tenures: Option<&[WantedTenure]>,
        pox_constants: &PoxConstants,
        first_burn_height: u64,
        mut inventory_iter: impl Iterator<Item = (&'a NeighborAddress, &'a NakamotoTenureInv)>,
    ) -> HashMap<NeighborAddress, AvailableTenures> {
        let mut tenure_block_ids = HashMap::new();
        while let Some((naddr, tenure_inv)) = inventory_iter.next() {
            let Some(peer_tenure_block_ids) = TenureStartEnd::from_inventory(
                rc,
                wanted_tenures,
                next_wanted_tenures,
                pox_constants,
                first_burn_height,
                tenure_inv,
            ) else {
                // this peer doesn't know about this reward cycle
                continue;
            };
            tenure_block_ids.insert(naddr.clone(), peer_tenure_block_ids);
        }
        tenure_block_ids
    }
```

**File:** stackslib/src/net/download/nakamoto/download_state_machine.rs (L973-981)
```rust
        for wt in wanted_tenures.iter().chain(prev_wanted_tenures.iter()) {
            debug!("Check unconfirmed tenures: check {:?}", &wt);
            let is_available_and_processed = tenure_block_ids.iter().any(|(_, available)| {
                if let Some(tenure_start_end) = available.get(&wt.tenure_id_consensus_hash) {
                    tenure_start_end.processed
                } else {
                    true
                }
            });
```

**File:** stackslib/src/net/download/nakamoto/download_state_machine.rs (L1469-1486)
```rust
        self.fetch_unconfirmed_tenures = if self
            .last_unconfirmed_download_check_ms
            .saturating_add(CHECK_UNCONFIRMED_TENURES_MS)
            > get_epoch_time_ms()
        {
            false
        } else {
            let do_fetch = Self::need_unconfirmed_tenures(
                burnchain_height,
                &network.burnchain_tip,
                &self.wanted_tenures,
                self.prev_wanted_tenures.as_ref().unwrap_or(&vec![]),
                &self.tenure_block_ids,
                &self.available_tenures,
            );
            self.last_unconfirmed_download_check_ms = get_epoch_time_ms();
            do_fetch
        };
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

**File:** stackslib/src/net/download/nakamoto/tenure.rs (L150-184)
```rust
            // advance to next tenure-start sortition
            let bit = u16::try_from(i).expect("FATAL: more sortitions than u16::MAX");
            if !invbits.get(bit).unwrap_or(false) {
                debug!("i={} bit not set", i);
                continue;
            }

            // the last tenure we'll consider
            last_tenure = i;

            let Some(wt_start_idx) = ((i + 1)..wanted_tenures.len()).find(|j| {
                let bit = u16::try_from(*j).expect("FATAL: more sortitions than u16::MAX");
                invbits.get(bit).unwrap_or(false)
            }) else {
                debug!("i={} out of wanted_tenures", i);
                break;
            };

            let Some(wt_start) = wanted_tenures.get(wt_start_idx) else {
                debug!("i={} no start wanted tenure", i);
                break;
            };

            let Some(wt_end_index) = ((wt_start_idx + 1)..wanted_tenures.len()).find(|j| {
                let bit = u16::try_from(*j).expect("FATAL: more sortitions than u16::MAX");
                invbits.get(bit).unwrap_or(false)
            }) else {
                debug!("i={} out of wanted_tenures", i);
                break;
            };

            let Some(wt_end) = wanted_tenures.get(wt_end_index) else {
                debug!("i={} no end wanted tenure", i);
                break;
            };
```
