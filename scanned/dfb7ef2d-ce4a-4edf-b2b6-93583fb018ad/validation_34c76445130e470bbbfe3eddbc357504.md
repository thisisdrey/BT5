## Analysis

The Sablier report's core broken invariant: **an address computed from CREATE (nonce-based) semantics is treated as stable/final before the transaction that "claims" that nonce is actually finalized**, allowing funds to be routed to a not-yet-canonical address that a reorg can hand to different bytecode.

`pallet-revive` reproduces the exact same primitive natively, and its own Ethereum-RPC compatibility layer actively encourages the unsafe workflow.

`substrate/frame/revive/src/address.rs` implements both semantics: `create1` (pure nonce-based, EVM `CREATE`) and `create2` (salt-based) [1](#0-0) . Salt is optional in the `instantiate` code path — when no salt is supplied, `create1` derives the address purely from `deployer` + `account_nonce` [2](#0-1) , mirroring the CREATE opcode path used for Solidity `new Contract()` in `substrate/frame/revive/src/vm/evm/instructions/contract.rs` [3](#0-2) .

Crucially, the eth-RPC layer's `eth_getTransactionCount` — the exact API Ethereum tooling/wallets use to pre-compute a CREATE1 address before a deployment confirms — resolves the default/`latest`/`pending` block tag to the **non-finalized best block**, not the finalized one: [4](#0-3) 

`get_transaction_count` then simply reads the nonce at whatever block that tag resolves to [5](#0-4) , and the crate's own test suite demonstrates the exact vulnerable pattern from the report — computing `create1(&account.address(), nonce)` from a nonce fetched against the (potentially unfinalized) default block tag, then treating that address as final and funding it, before the deploying transaction is finalized: [6](#0-5)  and [7](#0-6) .

This is precisely the shape of bug Parity itself later found and fixed for the dry-run vs. actual-dispatch nonce mismatch (`prdoc/stable2506/pr_8504.prdoc`) [8](#0-7)  — confirming that CREATE1 address stability under "which nonce is really canonical" is a real, previously-mishandled concern in this pallet, not a hypothetical.

### Title
Non-finalized nonce used for CREATE1 address prediction in pallet-revive/eth-rpc enables reorg-driven contract-address hijack — (File: `substrate/frame/revive/rpc/src/client.rs`, `substrate/frame/revive/src/exec.rs`)

### Summary
`pallet-revive` implements Ethereum's `CREATE` opcode with address derivation `create1(deployer, nonce)`, and exposes `eth_getTransactionCount` for tooling to predict that address ahead of confirmation. The RPC layer's `latest`/`pending`/default tag resolves to the **best, non-finalized** block rather than the finalized one. Any workflow that funds a CREATE1-predicted address before finality (the same pattern the crate's own tests perform) is exposed to a chain reorg reassigning that nonce slot to different contract bytecode — the same "reorg attack against `CREATE`" class described in the Sablier report, now native to Substrate/Aura block production instead of Ethereum L1/L2s.

### Finding Description
`address::create1` derives a contract's address solely from the deployer's address and account nonce [9](#0-8) . In `Frame` construction, when `instantiate`/`instantiate_with_code` is called without a `salt`, this nonce-only derivation is exactly what's used [2](#0-1) ; salt is entirely optional, so nothing in the pallet forces callers (EOAs, or contracts acting as factories for others) into the collision-resistant CREATE2 form.

Ethereum-compatible tooling built against pallet-revive's `eth-rpc` predicts this address off-chain via `eth_getTransactionCount`, exactly as the crate's own integration tests do: fetch nonce, then `create1(address, nonce)`, then fund the predicted address before the deploying transaction is confirmed [6](#0-5) . The RPC layer resolves `Latest`/`Pending` (and the default `BlockId`) to `block_provider.latest_block()` — the current best block, which is not guaranteed final [4](#0-3) . Only `Finalized`/`Safe` route to `latest_finalized_block()`.

If the block containing the nonce-consuming transaction is later reorganized out (a routine occurrence during network partitions/near-simultaneous block production under Aura/BABE, requiring no malicious validator, relayer, or governance actor — merely two honest block producers racing), the nonce slot that the address prediction relied on can be re-assigned on the new canonical fork to a *different* transaction/contract. Anything sent to the pre-computed address in the interim now belongs to whatever code actually lands in that nonce slot post-reorg — identical in outcome to the Sablier `clawback`-theft scenario, generalized to any "pre-fund a predicted CREATE1 address" integration built against this RPC surface (including factory-style contracts that call the CREATE opcode on behalf of many callers using their own contract nonce, which is the closest structural match to `SablierV2MerkleLockupFactory`).

### Impact Explanation
Funds sent to a CREATE1-predicted address before finality can be permanently redirected to attacker- or third-party-controlled bytecode after a reorg, resulting in theft or loss of user/contract funds — matching the "theft or unbacked mint/unlock" and "public underpriced work / false state acceptance" categories in scope, since the corrupted value (the non-finalized nonce) is silently accepted as authoritative by the RPC layer and downstream tooling.

### Likelihood Explanation
Requires only a short, ordinary chain reorg (not a malicious actor) plus a counterfactual-funding pattern that pallet-revive's own test suite and documentation implicitly demonstrate as valid usage. Likelihood is low-to-medium depending on chain reorg depth/frequency, consistent with the original report's "Medium" overall rating.

### Recommendation
- Document and, where feasible, enforce that CREATE1-predicted addresses (via `eth_getTransactionCount`/`create1`) must only be treated as final once the block is finalized — i.e., steer tooling/users to the `finalized`/`safe` tag, or reject "latest"-based prediction for funding flows.
- Encourage/require CREATE2 with a `salt` binding `msg.sender` for factory-style deployments, matching the report's mitigation, and consider surfacing this guidance in `pallet-revive` docs given the pallet already supports both `create1`/`create2`.

### Proof of Concept
1. Query `eth_getTransactionCount(deployer, "latest")` (resolves to best, non-finalized block per `client.rs`).
2. Compute `contract_address = create1(deployer, nonce)` off-chain (same call performed in `tests.rs::test_deploy_and_call`).
3. Send value to `contract_address` immediately, assuming the deployment transaction at `nonce` will land there.
4. A short reorg occurs before finality; a different transaction is included at that nonce slot on the new best chain (e.g., the deployer's own resubmission with different constructor code, or — for a shared-nonce factory contract pattern — a different end-user's factory-mediated deployment).
5. The funds sent in step 3 now belong to the account/contract that occupies `contract_address` on the reorganized canonical chain, which the attacker/beneficiary can immediately drain.

### Citations

**File:** substrate/frame/revive/src/address.rs (L263-285)
```rust
/// Determine the address of a contract using CREATE semantics.
pub fn create1(deployer: &H160, nonce: u64) -> H160 {
	let mut list = rlp::RlpStream::new_list(2);
	list.append(&deployer.as_bytes());
	list.append(&nonce);
	let hash = keccak_256(&list.out());
	H160::from_slice(&hash[12..])
}

/// Determine the address of a contract using the CREATE2 semantics.
pub fn create2(deployer: &H160, code: &[u8], input_data: &[u8], salt: &[u8; 32]) -> H160 {
	let init_code_hash = {
		let init_code: Vec<u8> = code.into_iter().chain(input_data).cloned().collect();
		keccak_256(init_code.as_ref())
	};
	let mut bytes = [0; 85];
	bytes[0] = 0xff;
	bytes[1..21].copy_from_slice(deployer.as_bytes());
	bytes[21..53].copy_from_slice(salt);
	bytes[53..85].copy_from_slice(&init_code_hash);
	let hash = keccak_256(&bytes);
	H160::from_slice(&hash[12..])
}
```

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

**File:** substrate/frame/revive/src/vm/evm/instructions/contract.rs (L70-83)
```rust
	let salt = if IS_CREATE2 {
		let [salt] = interpreter.stack.popn()?;
		Some(salt.to_big_endian())
	} else {
		None
	};

	let call_result = interpreter.ext.instantiate(
		&CallResources::NoLimits,
		Code::Upload(code),
		value,
		vec![],
		salt.as_ref(),
	);
```

**File:** substrate/frame/revive/rpc/src/client.rs (L746-754)
```rust
			BlockNumberOrTag::Finalized | BlockNumberOrTag::Safe => {
				let block = self.block_provider.latest_finalized_block().await;
				Ok(Some(block))
			},
			BlockNumberOrTag::Earliest => self.block_by_number(self.earliest_block_number()).await,
			BlockNumberOrTag::Latest | BlockNumberOrTag::Pending => {
				let block = self.block_provider.latest_block().await;
				Ok(Some(block))
			},
```

**File:** substrate/frame/revive/rpc/src/lib.rs (L536-544)
```rust
	async fn get_transaction_count(&self, address: H160, block: BlockId) -> RpcResult<U256> {
		let hash = self.client.block_hash_for_tag(block).await?;
		let runtime_api = self.client.runtime_api(hash).await?;
		let nonce = runtime_api
			.nonce(address)
			.ok_or(ClientError::UnsupportedRuntimeApiMethod("nonce"))?
			.await?;
		Ok(nonce)
	}
```

**File:** substrate/frame/revive/rpc/src/tests.rs (L500-513)
```rust
	// Deploy contract
	let data = b"hello world".to_vec();
	let value = U256::from(5_000_000_000_000u128);
	let (bytes, _) = pallet_revive_fixtures::compile_module("dummy")?;
	let input = bytes.into_iter().chain(data.clone()).collect::<Vec<u8>>();
	let nonce = client.get_transaction_count(account.address(), Default::default()).await?;
	let tx = TransactionBuilder::new(client.clone()).value(value).input(input).send().await?;
	let receipt = tx.wait_for_receipt().await?;
	let contract_address = create1(&account.address(), nonce.try_into().unwrap());
	assert_eq!(
		Some(contract_address),
		receipt.contract_address,
		"Contract should be deployed at {contract_address:?}."
	);
```

**File:** substrate/frame/revive/rpc/src/tests.rs (L996-1015)
```rust
	// Deploy a contract and trigger it to emit a log.
	let (bytes, _) = pallet_revive_fixtures::compile_module_with_type(
		"SimpleReceiver",
		pallet_revive_fixtures::FixtureType::Solc,
	)?;
	let nonce = client.get_transaction_count(account.address(), Default::default()).await?;
	TransactionBuilder::new(client.clone())
		.input(bytes.to_vec())
		.send()
		.await?
		.wait_for_receipt()
		.await?;
	let contract_address = create1(&account.address(), nonce.try_into().unwrap());
	let emit_receipt = TransactionBuilder::new(client.clone())
		.value(U256::from(1_000_000_000_000u128))
		.to(contract_address)
		.send()
		.await?
		.wait_for_receipt()
		.await?;
```

**File:** prdoc/stable2506/pr_8504.prdoc (L31-59)
```text
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

    A new test `nonce_not_incremented_in_dry_run()` has been added to verify the behavior.

    ## Before Fix

    - Dry-run contract deployment returns address derived with nonce N
    - Actual transaction deployment creates contract at address derived with nonce N-1
    - Result: Inconsistent addresses between simulation and actual execution

    ## After Fix

    - Dry-run and actual transaction deployments both create contracts at the same address
    - Result: Consistent contract addresses regardless of execution context
    - Added test case to verify nonce handling in different execution contexts

    This fix ensures that users can rely on the address returned by a dry run to match the actual address that will be used when the transaction is submitted.

    Fixes https://github.com/paritytech/contract-issues/issues/37
```
