[1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** stackslib/src/net/download/nakamoto/download_state_machine.rs (L1461-1466)
```rust
        self.update_available_tenures(
            &invs.inventories,
            &sortdb.pox_constants,
            sortdb.first_block_height,
            ibd,
        );
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

**File:** stackslib/src/net/download/nakamoto/download_state_machine.rs (L1497-1510)
```rust
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
```
