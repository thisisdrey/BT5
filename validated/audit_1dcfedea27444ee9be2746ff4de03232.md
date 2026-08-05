Based on my analysis, this claim is confirmed valid.

Audit Report

## Title
Unvalidated `value` field in `Transact`/`ContractCall::V1` allows draining an Ethereum agent's Ether balance beyond what is reserved in the message - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`)

## Summary
`XcmConverter::convert` builds a `Command::CallContract { target, calldata, gas, value }` from a user-supplied `Transact`/`ContractCall::V1` XCM instruction without validating that `value` is bounded by the ether actually reserved/withdrawn in that same message, and the error variant clearly created for this purpose is never used. Because the agent is keyed off `AgentIdOf::convert_location(origin_location)` — which can resolve to a shared, parachain/account-level sovereign identity used across many bridging operations over time — accumulated residual Ether in that agent (fees, dust, prior partial transfers) can be swept out to an attacker-controlled `target` in a single unrelated message.

## Finding Description
`XcmConverter::convert` in `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs:217-325` extracts `fee_amount` via `extract_remote_fee()` (line 219) and any ENA/PNA amounts via `extract_ethereum_native_assets`/`extract_polkadot_native_assets` (lines 276-292), then independently decodes the optional `Transact` payload into `ContractCall::V1 { target, calldata, gas, value }` and unconditionally pushes `Command::CallContract { value, .. }` (lines 294-305) — with no comparison of `value` against `fee_amount` or the reserved ENA/PNA amounts. [1](#0-0) 

The dead error variant `CallContractValueInsufficient` (declared at line 46) is never referenced anywhere in `convert()`, confirming the guard was intended but not wired in. [2](#0-1) 

On Ethereum, `Command::CallContract`'s `value` is explicitly documented as "Include ether held by agent contract," and `ContractCall::V1.value` documentation states "Include ether held by the agent contract" — both confirming this field draws from the agent's accumulated balance rather than being scoped to the current message's own transferred amount. [3](#0-2) 

The agent identity is derived from `origin_location` via `AgentIdOf::convert_location`, gated only by `AllowedAliasOrigin::contains(origin_location)` (lines 246-256). Per the repo's own edge-case test, this location can resolve to a shared, coarse-grained identity (e.g., a whole parachain's sovereign location, `Location::new(1, [Parachain(...)])`), meaning multiple distinct signed accounts routing through the same parachain path can end up sharing one Ethereum agent and its accumulated balance. [4](#0-3) [5](#0-4) 

The repository's own test explicitly flags this as a missing validation: `// value should be less than the transfer amount, require validation on BH Exporter`, confirming this check was expected but never implemented. [6](#0-5) 

## Impact Explanation
This breaks the invariant that a message's authorized ether movement should be bound to what that message itself reserves/withdraws. An agent contract accumulates Ether over multiple bridging operations (fees, partial transfers, retries); since `CallContract.value` is unbounded relative to the current message, any account able to route a valid `AliasOrigin`/`Transact` through the exporter (an ordinary, unprivileged operation) can direct the Gateway to forward the agent's full accumulated balance to an arbitrary contract. This matches the "theft or unbacked mint/unlock" category of the impact gate — direct, unauthorized transfer of bridge-custodied Ether.

## Likelihood Explanation
High. Constructing the exploit XCM requires no privileged capability beyond having a valid agent — exactly the pattern already exercised by the repository's own `transact_with_agent_from_asset_hub` test [7](#0-6) . The dead `CallContractValueInsufficient` variant and the explicit test-code comment about missing "validation on BH Exporter" are strong, direct evidence this is a genuine gap rather than an intentional design choice, and the happy-path tests currently pass without ever exercising this bound.

## Recommendation
In `XcmConverter::convert`, after computing `fee_amount` and any ENA amount transferred to the ether (`Here`) location, enforce that `ContractCall::V1.value` does not exceed the ether amount actually reserved/withdrawn for this specific message, returning the existing `XcmConverterError::CallContractValueInsufficient` otherwise. Alternatively, redesign the Ethereum-side Gateway/agent semantics to escrow per-message rather than allowing `CallContract` to draw from the agent's total historical balance.

## Proof of Concept
1. An attacker with an established agent (via `AliasOrigin`, e.g. a signed AssetHub account or a shared parachain sovereign origin) submits an XCM via `pallet_xcm::execute`, following the shape of `transact_with_agent_from_asset_hub`: `WithdrawAsset`/`PayFees` (small fee), `InitiateTransfer` with `remote_xcm` containing `AliasOrigin`, `DepositAsset`, and `Transact { call: ContractCall::V1{ target: attacker_contract, value: AGENT_ETHER_BALANCE, .. } }`.
2. `XcmConverter::convert` at `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs:294-305` decodes this and emits `Command::CallContract { value: AGENT_ETHER_BALANCE, .. }` unconditionally, without ever consulting `fee_amount` or ENA/PNA reserved amounts.
3. On delivery, the Gateway forwards `AGENT_ETHER_BALANCE` wei from the agent to `attacker_contract`, draining ether that was never part of this message's own reserved/withdrawn assets.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L44-47)
```rust
	TransactParamsDecodeFailed,
	FeeAssetResolutionFailed,
	CallContractValueInsufficient,
	NoCommands,
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L246-256)
```rust
		// Check AliasOrigin.
		let origin_location = match_expression!(self.next()?, AliasOrigin(origin), origin)
			.ok_or(AliasOriginExpected)?;

		// Validate the AliasOrigin using the configured AllowedAliasOrigin filter.
		// This provides a mechanism for the runtime to restrict which origins
		// are permitted to alias, providing defense-in-depth against
		// unprivileged alias attempts.
		ensure!(AllowedAliasOrigin::contains(origin_location), InvalidOrigin);

		let origin = AgentIdOf::convert_location(origin_location).ok_or(InvalidOrigin)?;
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L182-192)
```rust
	/// Call Contract on Ethereum
	CallContract {
		/// Target contract address
		target: H160,
		/// ABI-encoded calldata
		calldata: Vec<u8>,
		/// Maximum gas to forward to target contract
		gas: u64,
		/// Include ether held by agent contract
		value: u128,
	},
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound_edge_case.rs (L441-446)
```rust
	let forged_assethub_origin = Location::new(1, [Parachain(AssetHubWestend::para_id().into())]);
	let expected_assethub_agent = AgentIdOf::convert_location(&forged_assethub_origin).unwrap();
	assert_eq!(
		expected_assethub_agent,
		hex!("81c5ab2571199e3188135178f3c2c8e2d268be1313d029b30f534fa579b69b79").into()
	);
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
