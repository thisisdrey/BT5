## Finding confirmed

Both my analysis and the existing test suite corroborate the hypothesis in the question. The critical code is `TenureStartEnd::from_inventory`'s boundary-straddling loop: [1](#0-0) 

### Title
`TenureStartEnd::from_inventory` mislabels `start_reward_cycle`/`end_reward_cycle` for boundary tenures, causing tenure-block signature verification against the wrong reward-cycle's signer set - ([File: stackslib/src/net/download/nakamoto/tenure.rs])

### Summary
When a tenure's start (or end) block actually falls in the *next* reward cycle relative to the reward cycle being processed, `TenureStartEnd::from_inventory`'s boundary-handling loop hardcodes `start_reward_cycle` to the current `rc` (line 304) instead of the reward cycle actually containing `wt_start`, and separately computes `end_reward_cycle` from `wt_start.burn_height` (line 306) rather than `wt_end.burn_height`. This mislabeled reward cycle is later used by `NakamotoTenureDownloaderSet::make_tenure_downloaders` to fetch the signer reward set from `current_reward_cycles`, which is then handed to `NakamotoTenureDownloader::new` as `start_signer_keys`/`end_signer_keys` and used in `try_accept_tenure_start_block`/`try_accept_tenure_end_block` to verify block signer signatures.

### Finding Description
In the boundary loop of `TenureStartEnd::from_inventory`, `wt_start` is searched for first in `wanted_tenures` (current `rc`) and, failing that, in `next_wanted_tenures` (`rc+1`), tracked via the `in_next` flag: [2](#0-1) 

`wt_end` is *guaranteed* to be found in `next_wanted_tenures` (i.e., cycle `rc+1`), per the code's own comment: [3](#0-2) 

But the resulting `TenureStartEnd` is constructed with:
- `start_reward_cycle = rc` unconditionally (ignoring `in_next`)
- `end_reward_cycle = pox_constants.block_height_to_reward_cycle(first_burn_height, wt_start.burn_height)` (using `wt_start`'s height, not `wt_end`'s) [4](#0-3) 

Two mismatches result:
- When `in_next == true` (the real start block is in `rc+1`), `start_reward_cycle` is wrongly recorded as `rc` instead of `rc+1`.
- When `in_next == false` (start block still in `rc`, but end block guaranteed in `rc+1`), `end_reward_cycle` is wrongly computed as `rc` instead of `rc+1`.

Downstream, `NakamotoTenureDownloaderSet::make_tenure_downloaders` uses these mislabeled fields verbatim to look up the reward set used for signature verification: [5](#0-4) 

That reward set becomes `start_signer_keys`/`end_signer_keys` passed into `NakamotoTenureDownloader::new` (line 494-505), and is later used to verify the actual on-wire block: [6](#0-5) [7](#0-6) 

The existing unit test `test_tenure_start_end_from_inventory` exercises exactly this boundary-straddling scenario but only asserts `start_block_id`/`end_block_id` correctness, never `start_reward_cycle`/`end_reward_cycle` correctness against `PoxConstants::block_height_to_reward_cycle`, so this defect is not caught: [8](#0-7) 

### Impact Explanation
Because the reward set used for signature verification is fetched by the (possibly wrong) `start_reward_cycle`/`end_reward_cycle` key rather than the reward cycle that actually signed the block at that height, if the two adjacent cycles' reward/signer sets differ in membership, `verify_signer_signatures` will be checked against the wrong signer set. The realistic, unprivileged-attacker-reachable direction is that a legitimately-signed, canonical tenure-start/end block fails verification and is rejected (`NetError::InvalidMessage`), stalling that peer's tenure download at the reward-cycle boundary. The "forged block accepted" direction described in the question would additionally require the attacker to possess signing keys from the wrongly-selected reward cycle, which is outside the unprivileged threat model. This is best characterized as an availability/sync-correctness defect at reward-cycle boundaries rather than a forged-data-acceptance vulnerability, since actually causing acceptance of a forged/non-canonical block requires signer-key material the attacker does not have.

### Likelihood Explanation
The selection of `wt_start`/`wt_end` (and hence whether `in_next` is true or false) is driven by which inventory bits (from `NakamotoTenureInv`, a peer-supplied, unauthenticated gossip/inv structure) are set for the reward cycle straddling the boundary — an unprivileged remote peer can freely advertise inventory bit patterns to steer this selection. However, the underlying `wanted_tenures`/`next_wanted_tenures` entries themselves come from the local, real sortition history, so the actual blocks referenced are genuine chain data; the attacker's role is limited to influencing indices, not forging block content or signer sets.

### Recommendation
In `TenureStartEnd::from_inventory`'s boundary loop, compute `start_reward_cycle` from `wt_start.burn_height` (respecting `in_next`) and `end_reward_cycle` from `wt_end.burn_height`, both via `pox_constants.block_height_to_reward_cycle(first_burn_height, ...)`, instead of hardcoding `rc` for the start and reusing `wt_start.burn_height` for the end. Add a regression test asserting that `TenureStartEnd::start_reward_cycle`/`end_reward_cycle` match `PoxConstants::block_height_to_reward_cycle` applied to the actual `start_block_id`/`end_block_id` burn heights for boundary-straddling tenures.

### Proof of Concept
Extend `test_tenure_start_end_from_inventory` (stackslib/src/net/tests/download/nakamoto.rs) for the boundary case (rc, rc+1) loop: for each `tenure_start_end` returned when `next_wanted_tenures` is supplied, look up the actual burn height of the wanted tenure matching `tenure_start_end.start_block_id` (search `all_tenures`) and assert:
```rust
let expected_start_rc = pox_constants
    .block_height_to_reward_cycle(first_burn_height, matching_wt.burn_height)
    .unwrap();
assert_eq!(tenure_start_end.start_reward_cycle, expected_start_rc);
```
This assertion fails whenever `in_next == true` for that tenure, demonstrating `start_reward_cycle` is mislabeled as `rc` instead of `rc+1`.

### Citations

**File:** stackslib/src/net/download/nakamoto/tenure.rs (L251-315)
```rust
            let Some((in_next, wt_start_idx, wt_start)) = ((i + iter_start + 1)
                ..wanted_tenures.len())
                .find_map(|j| {
                    // search `wanted_tenures`
                    let bit = u16::try_from(j).expect("FATAL: more sortitions than u16::MAX");
                    if invbits.get(bit).unwrap_or(false) {
                        wanted_tenures.get(j).map(|tenure| (false, j, tenure))
                    } else {
                        None
                    }
                })
                .or_else(|| {
                    // search `next_wanted_tenures`
                    (0..next_wanted_tenures.len()).find_map(|n| {
                        let bit = u16::try_from(n).expect("FATAL: more sortitions than u16::MAX");
                        if next_invbits.get(bit).unwrap_or(false) {
                            next_wanted_tenures.get(n).map(|tenure| (true, n, tenure))
                        } else {
                            None
                        }
                    })
                })
            else {
                debug!(
                    "i={} out of wanted_tenures and next_wanted_tenures",
                    iter_start + i
                );
                break;
            };

            // search after the wanted tenure we just found to get the tenure-end wanted tenure. It
            // is guaranteed to be in `next_wanted_tenures`, since otherwise we would have already
            // found it
            let next_start = if in_next { wt_start_idx + 1 } else { 0 };
            let Some(wt_end) = (next_start..next_wanted_tenures.len()).find_map(|k| {
                let bit = u16::try_from(k).expect("FATAL: more sortitions than u16::MAX");
                if next_invbits.get(bit).unwrap_or(false) {
                    next_wanted_tenures.get(k)
                } else {
                    None
                }
            }) else {
                debug!("i={} out of next_wanted_tenures", iter_start + i);
                break;
            };

            let mut tenure_start_end = TenureStartEnd::new(
                wt.tenure_id_consensus_hash.clone(),
                wt.burn_height,
                wt_start.tenure_id_consensus_hash.clone(),
                wt_start.winning_block_id.clone(),
                wt_end.tenure_id_consensus_hash.clone(),
                wt_end.winning_block_id.clone(),
                rc,
                pox_constants
                    .block_height_to_reward_cycle(first_burn_height, wt_start.burn_height)
                    .unwrap_or_else(|| {
                        panic!(
                            "FATAL: tenure from before system start ({} <= {first_burn_height})",
                            wt_start.burn_height
                        )
                    }),
                wt.processed,
            );
            tenure_start_end.fetch_end_block = true;
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L442-463)
```rust
            let Some(Some(start_reward_set)) = current_reward_cycles
                .get(&tenure_info.start_reward_cycle)
                .map(|cycle_info| cycle_info.reward_set())
            else {
                debug!(
                    "Cannot fetch tenure-start block due to no known start reward set for cycle {}: {tenure_info:?}",
                    tenure_info.start_reward_cycle,
                );
                schedule.pop_front();
                continue;
            };
            let Some(Some(end_reward_set)) = current_reward_cycles
                .get(&tenure_info.end_reward_cycle)
                .map(|cycle_info| cycle_info.reward_set())
            else {
                debug!(
                    "Cannot fetch tenure-end block due to no known end reward set for cycle {}: {tenure_info:?}",
                    tenure_info.end_reward_cycle,
                );
                schedule.pop_front();
                continue;
            };
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L199-210)
```rust
        if let Err(e) = tenure_start_block
            .header
            .verify_signer_signatures(&self.start_signer_keys, self.epoch_id)
        {
            // signature verification failed
            warn!("Invalid tenure-start block: bad signer signature";
                   "tenure_id" => %self.tenure_id_consensus_hash,
                   "block.header.block_id" => %tenure_start_block.header.block_id(),
                   "state" => %self.state,
                   "error" => %e);
            return Err(NetError::InvalidMessage);
        }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L272-283)
```rust
        if let Err(e) = tenure_end_block
            .header
            .verify_signer_signatures(&self.end_signer_keys, self.epoch_id)
        {
            // bad signature
            warn!("Invalid tenure-end block: bad signer signature";
                  "tenure_id" => %self.tenure_id_consensus_hash,
                  "block.header.block_id" => %tenure_end_block.header.block_id(),
                  "state" => %self.state,
                  "error" => %e);
            return Err(NetError::InvalidMessage);
        }
```

**File:** stackslib/src/net/tests/download/nakamoto.rs (L1237-1319)
```rust
    // check the case where we have at least two Nakamoto rewrad cycles.
    // the available tenures should straddle the reward cycle boundary.
    for rc in 0..(num_rcs - 1) {
        debug!("rc = {}", rc);
        let available = TenureStartEnd::from_inventory(
            rc,
            &wanted_tenures,
            Some(&next_wanted_tenures),
            &pox_constants,
            first_burn_height,
            &invs,
        )
        .unwrap();

        // need to check across two reward cycles
        let bits_cur_rc = invs.tenures_inv.get(&rc).unwrap();
        let bits_next_rc = invs.tenures_inv.get(&(rc + 1)).unwrap();
        let mut bits = BitVec::<2100>::zeros(rc_len * 2).unwrap();
        for i in 0..rc_len {
            if bits_cur_rc.get(i).unwrap() {
                bits.set(i, true).unwrap();
            }
            if bits_next_rc.get(i).unwrap() {
                bits.set(i + rc_len, true).unwrap();
            }
        }

        for (i, wt) in wanted_tenures.iter().enumerate() {
            let tenure_start_end_opt = available.get(&wt.tenure_id_consensus_hash);
            if bits
                .get(i as u16)
                .unwrap_or_else(|| panic!("failed to get bit {i}: {wt:?}"))
            {
                // this sortition had a tenure
                let mut j = (i + 1) as u16;
                let mut tenure_start_index = None;
                let mut tenure_end_index = None;

                while j < bits.len() {
                    if bits.get(j).unwrap() {
                        tenure_start_index = Some(j);
                        j += 1;
                        break;
                    }
                    j += 1;
                }

                while j < bits.len() {
                    if bits.get(j).unwrap() {
                        tenure_end_index = Some(j);
                        break;
                    }
                    j += 1;
                }

                if tenure_start_index.is_some() && tenure_end_index.is_some() {
                    debug!(
                        "rc = {rc}, i = {i}, tenure_start_index = {tenure_start_index:?}, tenure_end_index = {tenure_end_index:?}"
                    );
                    let tenure_start_end = tenure_start_end_opt.unwrap_or_else(|| {
                        panic!("failed to get tenure_start_end_opt: i = {i}, wt = {wt:?}")
                    });
                    assert_eq!(
                        all_tenures[tenure_start_index.unwrap() as usize].winning_block_id,
                        tenure_start_end.start_block_id
                    );
                    assert_eq!(
                        all_tenures[tenure_end_index.unwrap() as usize].winning_block_id,
                        tenure_start_end.end_block_id
                    );
                } else {
                    assert!(tenure_start_end_opt.is_none());
                }
            } else {
                // no tenure here
                assert!(
                    tenure_start_end_opt.is_none(),
                    "tenure_start_end = {tenure_start_end_opt:?}, rc = {rc}, i = {i}, wt = {wt:?}"
                );
            }
        }
    }
}
```
