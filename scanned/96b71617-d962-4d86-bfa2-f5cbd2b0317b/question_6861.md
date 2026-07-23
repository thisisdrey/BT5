# Q6861: Version Snapshot Slip in all_checkpoint_versions

## Question
Can `storage/storage-interface/src/state_store/state_update_refs.rs::all_checkpoint_versions` mix versions, snapshots, or pruned history in a way that makes reads or proofs observe one state while commits or checks assume another?

## Target
- File/function: storage/storage-interface/src/state_store/state_update_refs.rs::all_checkpoint_versions
- Entrypoint: Submit crafted transactions or package publishes that force execution or commit paths to reach `all_checkpoint_versions` with attacker-shaped state changes or proofs.
- Attacker controls: transaction write-set shape, state keys, resource layouts, proof nodes, sibling hashes, event sequences, versions, and prunable state history
- Exploit idea: Exploit mismatched handling of current, historical, or pruned data in one logical storage operation.
- Invariant to test: One logical read, proof, or commit operation must bind to one exact version and one exact snapshot boundary.
- Expected Immunefi impact: Critical. Irreversible fund lock, permanently unspendable balances, or non-recoverable loss of access to user or protocol value caused by broken execution, storage, object, staking, vesting, multisig, or resource-account flows.
- Fast validation: Build regression cases around snapshot boundaries, pruned ranges, and historical reads, then assert no mixed-version result is accepted.
