## Finding

### Title
Unvalidated `value` field in Snowbridge V2 `CallContract` Transact command allows draining an Agent's Ether balance beyond the declared transfer amount - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`)

### Summary
The Snowbridge outbound-queue v2 XCM→Ethereum message converter accepts an opaque, off-chain/user-supplied `Transact` payload (`ContractCall::V1`) that carries its own `value` field describing how much Ether the target Agent contract on Ethereum should forward to an arbitrary `target` address. This `value` is never cross-checked against the assets actually withdrawn/reserved (`WithdrawAsset`/`ReserveAssetDeposited`) in the same XCM message, even though the code defines a dedicated error variant (`CallContractValueInsufficient`) for exactly this purpose that is never constructed or returned anywhere.

### Finding Description
`XcmConverter::convert` in `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs` parses an XCM program that may contain an explicit asset transfer (ENA via `WithdrawAsset`, or PNA via `ReserveAssetDeposited`) *and* an optional `Transact` instruction. The `Transact` call bytes are decoded into a `ContractCall::V1 { target, calldata, gas, value }` struct and blindly converted into `Command::CallContract { target, calldata, gas, value }`: [1](#0-0) 

The `value` field is fully attacker-controlled inside the `Transact` payload and is never validated against the `amount` extracted from the message's own `WithdrawAsset`/`ReserveAssetDeposited` instructions (handled separately in `extract_ethereum_native_assets`/`extract_polkadot_native_assets`): [2](#0-1) 

The error type explicitly anticipates this check but it is dead code — `CallContractValueInsufficient` is declared but never used: [3](#0-2) 

An integration test even documents the intended invariant in a comment, confirming this is a known/expected safeguard that is not actually implemented in the converter: [4](#0-3) 

`Command::CallContract` on the Ethereum side is dispatched by the origin's Agent contract, which acts as a proxy holding Ether (accumulated from prior fee payments/top-ups for that origin). Because `value` is not bound to what this specific message declares as being transferred, any origin permitted to route through `InitiateTransfer`/`Transact` (via the `AliasOrigin` check against `AllowedAliasOrigin`) can request the Agent to forward an amount of Ether to an arbitrary `target` far larger than what was withdrawn/reserved in the message — up to the Agent's entire accumulated balance.

### Impact Explanation
This is the on-chain analog of the reported bug class: the contract logic accepts an opaque, externally-produced payload (`ContractCall::V1` bytes inside `Transact`) whose embedded parameters (`value`, `target`) are not reconciled with the explicit, user-declared transfer amount in the same message. A mismatch here directly causes unbacked/incorrect movement of funds held by the Agent contract on Ethereum — i.e., theft/drain of bridge-held funds to an attacker-chosen address, which falls under the in-scope "theft or unbacked mint or unlock" and "runtime bugs that compromise intended behavior" categories for the Snowbridge/BridgeHub program.

### Likelihood Explanation
Exploitation requires only a permitted XCM sender able to satisfy the `AliasOrigin`/`AllowedAliasOrigin` check (e.g., a system/sibling parachain with alias rights to its own Agent) — no malicious relayer, validator, or governance action is needed. The sender fully controls the `Transact` call bytes it submits (it is their own local XCM execution via `pallet_xcm::execute` or `InitiateTransfer`), so they can freely set `value` independent of the amount actually withdrawn/reserved in the same program. The absence of any check (confirmed by the unused `CallContractValueInsufficient` variant) makes this directly reachable through the normal, unprivileged message-conversion path.

### Recommendation
In `XcmConverter::convert`, after extracting ENA/PNA transfer amounts and before pushing `Command::CallContract`, enforce that `value <= total amount already reserved/withdrawn` in the same message (or require an explicit separate reservation for the `value` used in `Transact`), returning `XcmConverterError::CallContractValueInsufficient` when the invariant is violated — matching the intent already documented in the test suite comment.

### Proof of Concept
1. An origin permitted by `AllowedAliasOrigin` constructs an XCM containing:
   - `WithdrawAsset`/`PayFees` for a small fee amount (e.g. `REMOTE_FEE_AMOUNT_IN_ETHER`).
   - No (or minimal) `WithdrawAsset`/`ReserveAssetDeposited` for actual transfer assets.
   - `AliasOrigin(<their own agent's origin>)`.
   - `DepositAsset` (can target `Wild(AllCounted(0))` if no real transfer assets, as shown in `transact_with_agent_from_asset_hub_without_any_asset_transfer`).
   - `Transact { call: ContractCall::V1 { target: <attacker address>, calldata: [], gas: 40000, value: <Agent's full Ether balance> }.encode() }`.
   - `SetTopic`.
2. `XcmConverter::convert` decodes the `Transact` payload and emits `Command::CallContract { target, calldata, gas, value }` with the attacker-chosen `value`, with no check against the (empty/near-zero) declared transfer amount: [1](#0-0) 
3. This is precisely the scenario already exercised (minus the missing validation) in `transact_with_agent_from_asset_hub`, where the test comment notes `value` should be checked against the transfer amount but no such check exists in `convert.rs`: [5](#0-4) 
4. When relayed and executed on Ethereum, the Agent contract forwards `value` Ether to `target`, draining funds beyond what the message declared as being transferred.

Note: I was unable to inspect the Solidity Gateway/Agent contract execution logic (out of the Rust-indexed scope) to confirm there is no equivalent check on the Ethereum side; if such a check exists there, it would mitigate this specific path, but no on-chain (Substrate-side) validation exists as shown above, and the dedicated error variant confirms this was intended to be enforced in the converter itself.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L44-47)
```rust
	TransactParamsDecodeFailed,
	FeeAssetResolutionFailed,
	CallContractValueInsufficient,
	NoCommands,
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L119-154)
```rust
	/// Extract ethereum native assets
	fn extract_ethereum_native_assets(
		&mut self,
		enas: &Assets,
		deposit_assets: &AssetFilter,
		recipient: H160,
	) -> Result<Vec<Command>, XcmConverterError> {
		let mut commands: Vec<Command> = Vec::new();
		for ena in enas.clone().into_inner().into_iter() {
			// Check the the deposit asset filter matches what was reserved.
			if !deposit_assets.matches(&ena) {
				return Err(FilterDoesNotConsumeAllAssets);
			}

			// only fungible asset is allowed
			let (token, amount) = match ena {
				Asset { id: AssetId(inner_location), fun: Fungible(amount) } => {
					match inner_location.unpack() {
						(0, [AccountKey20 { network, key }]) if self.network_matches(network) => {
							Ok((H160(*key), amount))
						},
						// To allow ether
						(0, []) => Ok((H160([0; 20]), amount)),
						_ => Err(AssetResolutionFailed),
					}
				},
				_ => Err(AssetResolutionFailed),
			}?;

			// transfer amount must be greater than 0.
			ensure!(amount > 0, ZeroAssetTransfer);

			commands.push(Command::UnlockNativeToken { token, recipient, amount });
		}
		Ok(commands)
	}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L294-305)
```rust
		// Transact commands
		let transact_call = match_expression!(self.peek(), Ok(Transact { call, .. }), call);
		if let Some(transact_call) = transact_call {
			let _ = self.next();
			let transact =
				ContractCall::decode_all(&mut transact_call.clone().into_encoded().as_slice())
					.map_err(|_| TransactDecodeFailed)?;
			match transact {
				ContractCall::V1 { target, calldata, gas, value } => commands
					.push(Command::CallContract { target: target.into(), calldata, gas, value }),
			}
		}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L516-580)
```rust
#[test]
fn transact_with_agent_from_asset_hub() {
	let weth_asset_location: Location = weth_location();

	fund_on_bh();

	register_assets_on_ah();

	fund_on_ah();

	AssetHubWestend::execute_with(|| {
		type RuntimeOrigin = <AssetHubWestend as Chain>::RuntimeOrigin;

		let local_fee_asset =
			Asset { id: AssetId(Location::parent()), fun: Fungible(LOCAL_FEE_AMOUNT_IN_DOT) };

		let remote_fee_asset =
			Asset { id: AssetId(ethereum()), fun: Fungible(REMOTE_FEE_AMOUNT_IN_ETHER) };

		let reserve_asset =
			Asset { id: AssetId(weth_asset_location.clone()), fun: Fungible(TOKEN_AMOUNT) };

		let assets = vec![reserve_asset.clone(), local_fee_asset.clone(), remote_fee_asset.clone()];

		let beneficiary =
			Location::new(0, [AccountKey20 { network: None, key: AGENT_ADDRESS.into() }]);

		let transact_info = ContractCall::V1 {
			target: Default::default(),
			calldata: vec![],
			gas: 40000,
			// value should be less than the transfer amount, require validation on BH Exporter
			value: 4 * (TOKEN_AMOUNT - REMOTE_FEE_AMOUNT_IN_ETHER) / 5,
		};

		let xcms = VersionedXcm::from(Xcm(vec![
			WithdrawAsset(assets.clone().into()),
			PayFees { asset: local_fee_asset.clone() },
			InitiateTransfer {
				destination: ethereum(),
				remote_fees: Some(AssetTransferFilter::ReserveWithdraw(Definite(
					remote_fee_asset.clone().into(),
				))),
				preserve_origin: true,
				assets: BoundedVec::truncate_from(vec![AssetTransferFilter::ReserveWithdraw(
					Definite(reserve_asset.clone().into()),
				)]),
				remote_xcm: Xcm(vec![
					DepositAsset { assets: Wild(AllCounted(2)), beneficiary },
					Transact {
						origin_kind: OriginKind::SovereignAccount,
						fallback_max_weight: None,
						call: transact_info.encode().into(),
					},
				]),
			},
		]));

		<AssetHubWestend as AssetHubWestendPallet>::PolkadotXcm::execute(
			RuntimeOrigin::signed(AssetHubWestendSender::get()),
			bx!(xcms),
			Weight::from(EXECUTION_WEIGHT),
		)
		.unwrap();
	});
```
