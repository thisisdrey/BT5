Based on my investigation, this repository still contains the pre-fix code that matches the bug described in `prdoc/stable2506/pr_8504.prdoc`: the `origin_is_caller` nonce-adjustment logic in `new_frame` at `substrate/frame/revive/src/exec.rs` has no `ExecContext`/dry-run check, meaning `CREATE1` address derivation still diverges between RPC dry-run and actual on-chain execution in this snapshot.

### Title
CREATE1 contract address derivation diverges between dry-run and actual transaction, breaking deterministic address prediction - (`substrate/frame/revive/src/exec.rs`)

### Summary
`pallet-revive`'s `Stack::new_frame` computes the address of a to-be-instantiated contract via `address::create1(deployer, nonce)` when no salt is supplied. The nonce used for this derivation is adjusted by subtracting 1 whenever `origin_is_caller` is true, to compensate for the pre-dispatch nonce increment that happens during real transaction execution. This repo's code applies that `-1` adjustment unconditionally based on `origin_is_caller` alone, with no distinction between a dry-run/RPC call context and an actual signed extrinsic, even though the prdoc for PR #8504 documents that this exact discrepancy was identified and a fix (checking `ExecContext::Transaction`) was proposed.

### Finding Description
In `substrate/frame/revive/src/exec.rs`, function `new_frame`: [1](#0-0) 

computes the deployed contract's address as:
```rust
address::create1(
    &deployer,
    if origin_is_caller {
        account_nonce.saturating_sub(1u32.into()).saturated_into()
    } else {
        account_nonce.saturated_into()
    },
)
```
This nonce-decrement branch is only correct when the frame is being built as part of an actual dispatched extrinsic (where `System::account_nonce` has already been bumped pre-dispatch). When the same code path is exercised through a runtime API dry-run (e.g. via RPC `call`/simulate endpoints used to preview deployments before submission), the nonce has *not* been incremented, so subtracting 1 yields a **different nonce than will actually be used on-chain**, and therefore `create1` computes a **different address** than the one that will actually receive the deployed contract.

`prdoc/stable2506/pr_8504.prdoc` documents that Parity identified precisely this bug and its intended fix, i.e. gating the `-1` subtraction on `matches!(exec_context, ExecContext::Transaction)` in addition to `origin_is_caller`: [2](#0-1) 

However, the actual `exec.rs` in this repository snapshot still contains only the `origin_is_caller` check with no `ExecContext` gating — the corrective condition described in the prdoc is absent from the code. Nothing in `contract_address`/`create1` itself, nor any caller of `new_frame`, re-validates that the nonce used matches the actual dispatch nonce; the only guard that was supposed to close this gap is the `ExecContext::Transaction` check, which is missing.

This is a direct structural analog of the Maverick M-02 bug class: the effective "deployment coordinate" (there: CREATE2 salt+constructor args; here: CREATE1 nonce) used to predict a contract's address is *not* guaranteed to be identical between the value observed by an external caller performing address prediction (dry-run) and the value actually used at execution time (transaction), so the predicted address and the deployed address diverge.

### Impact Explanation
Users, wallets, or other contracts (e.g. counterfactual funding schemes, cross-contract deploy-then-fund flows, off-chain tooling that pre-computes contract addresses for `pallet-revive`/EVM-compatibility deployments) that rely on the RPC/dry-run address to pre-fund, whitelist, or reference a contract before submitting the real deployment transaction can end up sending value or state references to an address that will never host the deployed contract. This matches the "public underpriced work / wrong beneficiary or address" impact class: value transferred to (or logic bound to) the predicted address is permanently stranded, since the actual code is instantiated at a different address than the one computed off-chain. This is a chain-visible correctness bug in `pallet-revive`, not an infrastructure or admin-abuse issue, and requires no privileged actor — any ordinary user performing a standard dry-run-then-submit deployment flow can trigger the mismatch.

### Likelihood Explanation
Likelihood is high for any workflow that uses a runtime-API/dry-run call to predict a `CREATE1` (no-salt) contract address before submitting the actual `instantiate`/`instantiate_with_code` extrinsic, since `origin_is_caller` alone does not distinguish "real extrinsic, nonce pre-incremented" from "dry run, nonce not incremented." This is the default and commonly documented usage pattern for previewing contract addresses in Substrate/EVM-compatible tooling, so the divergent-address condition is easily and unintentionally triggered without any adversarial behavior.

### Recommendation
Reinstate/land the fix described in `prdoc/stable2506/pr_8504.prdoc`: thread an `ExecContext` (or equivalent dry-run/transaction discriminator) into `new_frame`, and only apply the `saturating_sub(1)` nonce adjustment when `origin_is_caller && matches!(exec_context, ExecContext::Transaction)`. Add/verify a regression test analogous to `nonce_not_incremented_in_dry_run()` asserting that dry-run-computed and transaction-computed `create1` addresses are identical for the same deployer/nonce state.

### Proof of Concept
1. Fund account `ALICE` and note `System::account_nonce(&ALICE)`.
2. Call the runtime API dry-run path for `instantiate_with_code` (no salt) as `ALICE`; capture the returned predicted address — this goes through `new_frame` with `origin_is_caller = true` and the *pre-dispatch* (not yet incremented) nonce, but the code still subtracts 1 from it.
3. Submit the actual signed `instantiate_with_code` extrinsic as `ALICE`. Because nonce is incremented pre-dispatch for real extrinsics, the `-1` correctly compensates here, giving the true address `create1(ALICE_ADDR, nonce_before)`.
4. Since step 2 already used `nonce_before - 1` on an already-pre-increment nonce (because dry run never incremented it), the predicted address from the dry run equals `create1(ALICE_ADDR, nonce_before - 1)`, which differs from the true deployed address `create1(ALICE_ADDR, nonce_before)` computed in step 3 — reproducing the address mismatch documented in `pr_8504.prdoc`.

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1141-1158)
```rust
			FrameArgs::Instantiate { sender, executable, salt, input_data } => {
				let deployer = T::AddressMapper::to_address(&sender);
				let account_nonce = <System<T>>::account_nonce(&sender);
				let address = if let Some(salt) = salt {
					address::create2(&deployer, executable.code(), input_data, salt)
				} else {
					use sp_runtime::Saturating;
					address::create1(
						&deployer,
						// the Nonce from the origin has been incremented pre-dispatch, so we
						// need to subtract 1 to get the nonce at the time of the call.
						if origin_is_caller {
							account_nonce.saturating_sub(1u32.into()).saturated_into()
						} else {
							account_nonce.saturated_into()
						},
					)
				};
```

**File:** prdoc/stable2506/pr_8504.prdoc (L9-41)
```text
    The issue stems from the `create1` address derivation logic in `exec.rs`:

    ```rust
    address::create1(
        &deployer,
        // the Nonce from the origin has been incremented pre-dispatch, so we
        // need to subtract 1 to get the nonce at the time of the call.
        if origin_is_caller {
            account_nonce.saturating_sub(1u32.into()).saturated_into()
        } else {
            account_nonce.saturated_into()
        },
    )
    ```

    The code correctly subtracts 1 from the account nonce during a transaction execution (because the nonce is incremented pre-dispatch), but doesn't account for execution context - whether it's a real transaction or a dry run through the RPC.

    ## Review Notes

    This PR adds a new condition to check for the `ExecContext` when calculating the nonce for address derivation:

    ```rust
    address::create1(
        &deployer,
        // the Nonce from the origin has been incremented pre-dispatch, so we
        // need to subtract 1 to get the nonce at the time of the call.
        if origin_is_caller && matches!(exec_context, ExecContext::Transaction) {
            account_nonce.saturating_sub(1u32.into()).saturated_into()
        } else {
            account_nonce.saturated_into()
        },
    )
    ```
```
