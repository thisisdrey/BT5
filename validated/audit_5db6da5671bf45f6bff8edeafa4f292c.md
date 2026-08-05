Audit Report

## Title
Missing zero-address validation on ENA/PNA recipient in Snowbridge outbound XCM-to-Ethereum converter causes permanent fund loss - (File: `bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs`, `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`)

## Summary
The Snowbridge outbound-queue XCM converters extract the `recipient: H160` from the `DepositAsset { beneficiary, .. }` instruction and use it unchanged to build `Command::UnlockNativeToken`/`Command::MintForeignToken` without ever checking that it is non-zero. An unprivileged user can construct an XCM with `beneficiary: AccountKey20 { key: [0u8;20], network: None }`, which passes all converter checks and results in a command instructing the Ethereum Gateway contract to unlock/mint value at address `0x0`, permanently destroying the transferred assets.

## Finding Description
In `bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs`, both `make_unlock_native_token_command` and `make_mint_foreign_token_command` extract `recipient` via `match_expression!(beneficiary.unpack(), (0, [AccountKey20 { network, key }]) if self.network_matches(network), H160(*key))` at lines 254-260 and 371-377 respectively. The only validation performed is that the location is a single `AccountKey20` junction at depth 0 whose `network` matches the configured Ethereum network via `network_matches` (lines 327-333). There is no check that `key != [0u8; 20]`. The resulting `recipient` flows directly into `Command::UnlockNativeToken { agent_id, token, recipient, amount }` (lines 313-316) or `Command::MintForeignToken { token_id, recipient, amount }` (line 425).

The v2 converter in `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs` has the identical pattern at lines 266-272, feeding the unchecked `recipient` into both `extract_ethereum_native_assets` (line 151: `Command::UnlockNativeToken { token, recipient, amount }`) and `extract_polkadot_native_assets` (line 183: `Command::MintForeignToken { token_id, recipient, amount }`).

Existing guards in the surrounding code (`amount > 0` via `ZeroAssetTransfer`, asset-filter matching, fee-asset validation, network matching) address transfer amount and asset/network correctness but do not address recipient validity. This is the same class of omission the codebase treats seriously elsewhere: `CheckNonZeroSender` explicitly bans the zero account as a signed origin, and the assets `permit` precompile explicitly rejects a zero `owner`/`spender`, but no equivalent guard exists here for the Ethereum-side beneficiary.

## Impact Explanation
Any unprivileged user with access to `pallet_xcm::send`/`transfer_assets` from a parachain such as AssetHub can set the XCM `beneficiary` to `AccountKey20` with a zero key. This XCM will be accepted by `XcmConverter::convert` (both v1 and v2), queued via `OutboundQueue::validate`/`deliver`, committed to the outbound merkle root, and eventually relayed to the Ethereum Gateway contract as `UnlockNativeToken`/`MintForeignToken` with `recipient = 0x0`. Sending or minting value to the zero address on Ethereum is effectively an irrecoverable burn, causing permanent loss of the transferred user funds — this matches the "permanent user-fund or bridge-state lock" impact category for the Snowbridge bridge-processing scope.

## Likelihood Explanation
The exploit path requires only a standard, unprivileged XCM program construction and normal message submission from AssetHub (or any chain with access to the exporter) — no validator, relayer, or governance privilege is needed, and the malformed beneficiary passes every existing check in the converter (`network_matches`, `AccountKey20` shape, asset filters, fee checks). This is fully reachable and repeatable by any user who can submit XCM.

## Recommendation
Add an explicit zero-address check immediately after extracting `recipient` in `make_unlock_native_token_command`/`make_mint_foreign_token_command` (v1, `mod.rs:254-260` and `371-377`) and in `convert` (v2, `convert.rs:266-272`), e.g. `ensure!(recipient != H160::zero(), XcmConverterError::InvalidBeneficiary)` (or reuse `BeneficiaryResolutionFailed`), so that the conversion fails/returns `NotApplicable` rather than producing a `Command` addressed to `0x0`.

## Proof of Concept
1. From AssetHub, submit an XCM program (e.g., via `pallet_xcm::transfer_assets`) that reaches `EthereumBlobExporter::validate`, ending in `DepositAsset { assets: Wild(All), beneficiary: AccountKey20 { network: None, key: [0u8; 20] }.into() }`.
2. `XcmConverter::convert` in v1 (`mod.rs:253-260`) or v2 (`convert.rs:265-272`) successfully extracts `recipient = H160([0;20])` with no error raised, since only the `AccountKey20`/network match is checked.
3. The resulting `Command::UnlockNativeToken`/`MintForeignToken { recipient: H160::zero(), .. }` is accepted by `OutboundQueue::validate`/`deliver` and committed into the merkle root for relay to Ethereum.
4. Once relayed and executed by the Gateway contract, the transferred value is sent/minted to `0x0`, permanently lost to the user — reproducible as a unit test asserting `XcmConverter::convert` returns `Ok` (no `BeneficiaryResolutionFailed`/zero-check error) for a beneficiary of `AccountKey20 { key: [0;20], .. }`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L253-260)
```rust
		// assert that the beneficiary is AccountKey20.
		let recipient = match_expression!(
			beneficiary.unpack(),
			(0, [AccountKey20 { network, key }])
				if self.network_matches(network),
			H160(*key)
		)
		.ok_or(BeneficiaryResolutionFailed)?;
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L313-316)
```rust
		Ok((
			Command::UnlockNativeToken { agent_id: self.agent_id, token, recipient, amount },
			*topic_id,
		))
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L370-377)
```rust
		// assert that the beneficiary is AccountKey20.
		let recipient = match_expression!(
			beneficiary.unpack(),
			(0, [AccountKey20 { network, key }])
				if self.network_matches(network),
			H160(*key)
		)
		.ok_or(BeneficiaryResolutionFailed)?;
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L425-425)
```rust
		Ok((Command::MintForeignToken { token_id, recipient, amount }, *topic_id))
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L120-154)
```rust
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L157-186)
```rust
	fn extract_polkadot_native_assets(
		&mut self,
		pnas: &Assets,
		deposit_assets: &AssetFilter,
		recipient: H160,
	) -> Result<Vec<Command>, XcmConverterError> {
		let mut commands: Vec<Command> = Vec::new();
		ensure!(pnas.len() > 0, NoReserveAssets);
		for pna in pnas.clone().into_inner().into_iter() {
			if !deposit_assets.matches(&pna) {
				return Err(FilterDoesNotConsumeAllAssets);
			}

			// Only fungible is allowed
			let Asset { id: AssetId(asset_id), fun: Fungible(amount) } = pna else {
				return Err(AssetResolutionFailed);
			};

			// transfer amount must be greater than 0.
			ensure!(amount > 0, ZeroAssetTransfer);

			// Ensure PNA already registered
			let token_id = TokenIdOf::convert_location(&asset_id).ok_or(InvalidAsset)?;
			let expected_asset_id = ConvertAssetId::maybe_convert(token_id).ok_or(InvalidAsset)?;
			ensure!(asset_id == expected_asset_id, InvalidAsset);

			commands.push(Command::MintForeignToken { token_id, recipient, amount });
		}
		Ok(commands)
	}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L265-272)
```rust
		// assert that the beneficiary is AccountKey20.
		let recipient = match_expression!(
			beneficiary.unpack(),
			(0, [AccountKey20 { network, key }])
				if self.network_matches(network),
			H160(*key)
		)
		.ok_or(BeneficiaryResolutionFailed)?;
```
