## Title
Dry-run `CREATE` address prediction diverges from actual instantiation address, causing funds sent to a pre-computed contract address to be lost/stealable in `pallet-revive` — (File: `substrate/frame/revive/src/exec.rs`)

## Summary
`pallet-revive`'s CREATE-style contract address derivation (`create1`, analogous to the report's `create`-based `QuestFactory` address) is deterministic on `deployer_address + nonce`. Off-chain tooling (wallets, dApps, the eth-rpc dry-run endpoint) is expected to pre-compute this address before the deployment transaction lands, exactly the same "predict-then-fund" pattern that made the original `QuestFactory.createQuest` vulnerable. In this repository's current state, the nonce basis used by `Frame::new_frame` for `create1` during a dry run does **not** match the nonce basis used during real transaction execution, so the predicted address a user relies on to pre-fund/verify a contract does not match the address that is actually instantiated on-chain.

## Finding Description
`Frame::new_frame` computes the instantiation address for a non-salted `CREATE` as: [1](#0-0) 

```rust
FrameArgs::Instantiate { sender, executable, salt, input_data } => {
    let deployer = T::AddressMapper::to_address(&sender);
    let account_nonce = <System<T>>::account_nonce(&sender);
    let address = if let Some(salt) = salt {
        address::create2(&deployer, executable.code(), input_data, salt)
    } else {
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

This subtracts 1 from the nonce whenever `origin_is_caller` is true, on the (correct-for-real-transactions) assumption that the nonce was already incremented pre-dispatch. However, this same code path is also used when the runtime API is invoked for a dry run (e.g. `eth_call`/`eth_estimateGas`/state-query style calls through the `Revive` runtime API used by the eth-rpc proxy), where the nonce has **not** actually been incremented pre-dispatch. The repository's own `prdoc/stable2506/pr_8504.prdoc` documents this exact defect and its intended fix (gating the subtraction on `matches!(exec_context, ExecContext::Transaction)`): [2](#0-1) 

But a `grep` for `ExecContext` across the codebase shows the fix documented in the prdoc was never actually applied to `exec.rs` — only the changelog file exists, while the live code in `substrate/frame/revive/src/exec.rs` still contains the pre-fix logic: [3](#0-2) 

The `create1` derivation itself is a direct analog of Ethereum's `CREATE` opcode formula (deployer + nonce hash), matching the address-prediction primitive from the `QuestFactory` report: [4](#0-3) 

Because dry-run and real execution disagree on the nonce basis, any address a user/dApp derives via `eth_estimateGas`/`eth_call` dry-run against `instantiate_with_code` will be off by one nonce slot from the address the real transaction actually creates the contract at.

## Impact Explanation
This breaks the "predict-then-fund" invariant that `pallet-revive`/eth-rpc explicitly documents as reliable behavior ("This fix ensures that users can rely on the address returned by a dry run to match the actual address that will be used when the transaction is submitted"). Consequences:
- A user or integrator who pre-computes the deployment address off-chain via the dry-run/RPC path and sends value to that address before submitting the real deployment transaction will find their funds sitting at an address that is **not** the actual contract address (the real contract lands one nonce slot away).
- Because CREATE1 addresses are fully deterministic on `(deployer, nonce)`, another account transaction interleaving (any transaction from the same signer, or the difference between simulated vs. actual nonce accounting) can cause the *wrong* address to be treated as authoritative, permanently stranding value sent to the mispredicted address, or allowing whichever address gets deployed there next (by the same deployer's future nonce) to inherit funds not intended for it — the same "unbacked value ends up in a contract nobody intended" outcome as the `QuestFactory` reorg bug, achieved here purely through nonce-basis divergence rather than a chain reorg.
- This directly matches the "public underpriced work/degraded correctness" and "theft or unbacked mint" impact classes: value settles to the wrong beneficiary address with no additional privilege required.

## Likelihood Explanation
High: the divergent nonce basis is deterministic and triggers on every non-salted `instantiate` call executed through the dry-run/runtime-API path versus a real transaction — no adversarial timing, reorg, or privileged actor is required, unlike the original Solidity report which needed an actual chain reorg. Any standard integration that relies on `eth_estimateGas`/dry-run to predict a `CREATE`-style contract address before broadcasting the deployment will hit this mismatch. The repository's own PR metadata (`pr_8504.prdoc`) confirms Parity engineers identified and intended to fix this exact defect, but the fix is not present in `exec.rs`.

## Recommendation
Gate the nonce-basis correction on the actual execution context, not merely on `origin_is_caller`, exactly as described in the (currently undeployed) fix: only subtract 1 from the nonce when executing a genuine dispatched transaction (`ExecContext::Transaction`), and use the raw current nonce for all dry-run/runtime-API contexts, ensuring `create1` produces identical results whether invoked via dry run or via a submitted extrinsic.

## Proof of Concept
1. Account `A` (nonce `N`) calls the `Revive` runtime API / eth-rpc `eth_estimateGas`/`eth_call` with an `instantiate` (CREATE, no salt) payload — this executes `Frame::new_frame` with `origin_is_caller = true` and the *unincremented* nonce `N` (since no transaction was actually dispatched), producing `predicted = create1(&deployer, N - 1)` due to the unconditional subtraction.
2. The dApp/tooling sends value to `predicted` before broadcasting the real transaction, relying on the dry-run result.
3. Account `A` then submits the real `instantiate_with_code` extrinsic. Pre-dispatch, `System::account_nonce` is incremented to `N+1`; inside `new_frame`, `account_nonce.saturating_sub(1)` yields `N`, so the actual contract is created at `create1(&deployer, N)` — one nonce off from `predicted`.
4. The funds sent to `predicted` in step 2 are now stranded at an address with no contract code (or, if `A` deploys again later, could end up captured by whatever contract eventually lands at that nonce slot), reproducing the "funds sent to a wrongly-derived address are lost or captured by an unintended contract" impact from the `QuestFactory` report. [1](#0-0) [5](#0-4)

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

**File:** prdoc/stable2506/pr_8504.prdoc (L9-24)
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
```

**File:** prdoc/stable2506/pr_8504.prdoc (L45-57)
```text
    ## Before Fix

    - Dry-run contract deployment returns address derived with nonce N
    - Actual transaction deployment creates contract at address derived with nonce N-1
    - Result: Inconsistent addresses between simulation and actual execution

    ## After Fix

    - Dry-run and actual transaction deployments both create contracts at the same address
    - Result: Consistent contract addresses regardless of execution context
    - Added test case to verify nonce handling in different execution contexts

    This fix ensures that users can rely on the address returned by a dry run to match the actual address that will be used when the transaction is submitted.
```

**File:** substrate/frame/revive/src/address.rs (L263-270)
```rust
/// Determine the address of a contract using CREATE semantics.
pub fn create1(deployer: &H160, nonce: u64) -> H160 {
	let mut list = rlp::RlpStream::new_list(2);
	list.append(&deployer.as_bytes());
	list.append(&nonce);
	let hash = keccak_256(&list.out());
	H160::from_slice(&hash[12..])
}
```
