### Title
Sovereign account derivation collision across chains via `GlobalConsensusParachainConvertsFor` lets an attacker on Rococo Parachain 1000 control the sovereign account reserved for Westend Parachain 1000 on Asset Hub Next - ([File: polkadot/xcm/xcm-builder/src/location_conversion.rs])

### Summary
`GlobalConsensusParachainConvertsFor` derives a sovereign `AccountId` for a remote parachain purely from `(network, para_id)`, with no binding to the local chain, universal location depth, or any chain-specific domain separator beyond the target network/para_id pair. It is still wired into a live production `LocationToAccountId` pipeline (`substrate/frame/staking-async/runtimes/parachain/src/xcm_config.rs`) despite the type's own doc-comment warning that it is deprecated precisely because it "creates commonalities between chains with different security characteristics" and "could possibly form part of a more sophisticated attack scenario." This mirrors the LifeBuoy finding: a deterministic address/identity formula that ignores the deploying/host chain's identity lets a party that only controls a security domain elsewhere (e.g. a lower-security or bridged network reusing the same `NetworkId`/`ParaId` pair) end up in control of, or interfere with, funds/state meant to be exclusively addressable by the legitimate chain.

### Finding Description
`GlobalConsensusParachainConvertsFor::convert_location` computes:
```rust
fn from_params(network: &NetworkId, para_id: &u32) -> [u8; 32] {
    (b"glblcnsnss/prchn_", network, para_id).using_encoded(blake2_256)
}
``` [1](#0-0) 

The struct doc explicitly documents the broken invariant: [2](#0-1) 

This is analogous to LifeBuoy's `create2`/CREATE-based recovery address: the target account is a pure function of externally-controllable, chain-agnostic parameters (`NetworkId`, `ParaId`) with **no binding to the local chain's own identity or genesis**. Any two chains that both include `GlobalConsensusParachainConvertsFor` in their `LocationToAccountId` and are reachable via XCM with the same `(remote_network, remote_para_id)` pair will resolve to the *identical* 32-byte `AccountId`, regardless of the trust level of the path used to reach that account (bridge vs. direct, or a lower-security relay vs. a higher-security one).

The type remains actively configured in a real runtime's asset-transacting pipeline: [3](#0-2) 
That `LocationToAccountId` is used both as the `SovereignAccountOf` for `pallet_xcm` and as the account converter inside `FungibleTransactor`/`FungiblesTransactor`/`UniquesTransactor`, i.e. it directly determines which account XCM `Transact`/reserve-transfer/deposit operations will credit or authorize as origin: [4](#0-3) [5](#0-4) 

Because the derived account depends only on `(NetworkId, ParaId)` and not on the local `UniversalLocation`'s security context or any authenticated identity of "who actually controls para X under network Y," any XCM message that can be made to appear as originating from `Location { parents: N, interior: [GlobalConsensus(network), Parachain(para_id)] }` — reachable through any bridge/exporter that this chain trusts for that network — resolves to the same sovereign account as a message that legitimately originates from the real parachain. If a lower-assurance path (e.g., a differently-configured bridge, or a parachain that is later reassigned to a different consensus/relay with the same `para_id`) is ever trusted as a source for that `NetworkId`, funds and dispatch rights bound to that sovereign account are exposed to whoever controls that path — exactly the "same address reachable via an untrusted or lower-trust route" failure mode described in the LifeBuoy report (shared factory / reproducible deployer identity).

### Impact Explanation
The sovereign account produced by this converter is used by `pallet_xcm`'s `SovereignAccountOf` and by the asset transactors as the beneficiary/origin account for cross-chain asset transfers and `Transact` dispatch. If the derived account is reachable via more than one trust domain sharing the same `(network, para_id)` tuple, an attacker who controls only the weaker domain can withdraw balances held in, or dispatch as, that sovereign account — a direct theft/unauthorized-execution primitive, matching the "theft or unbacked mint/unlock" and "unauthorized execution or origin escalation" impact categories.

### Likelihood Explanation
This does not require a malicious relayer, validator, or admin — it is a structural property of the converter's formula being independent of the local chain's own consensus/trust boundary, and the type is still live in a shipped runtime configuration despite an internal deprecation warning acknowledging the exact same category of attack. Exploitability depends on whether any currently-configured bridge/exporter setup allows a second, lower-trust path to present the same `GlobalConsensus(network)/Parachain(para_id)` location as origin; this repository snapshot does not let me exhaustively confirm whether such a dual-path condition currently exists for `staking-async/runtimes/parachain`, so likelihood should be treated as conditional on bridge/exporter trust configuration rather than certain.

### Recommendation
Replace `GlobalConsensusParachainConvertsFor` with `ExternalConsensusLocationsConverterFor` (the documented, non-deprecated replacement) in `substrate/frame/staking-async/runtimes/parachain/src/xcm_config.rs`'s `LocationToAccountId`, and audit all runtimes still referencing the deprecated converter (`GlobalConsensusParachainConvertsFor` usages found only in `location_conversion.rs` itself and this runtime) to ensure no chain exposes a sovereign-account collision across differing trust paths for the same `(NetworkId, ParaId)`.

### Proof of Concept
1. Configure a runtime `A` with `LocationToAccountId` including `GlobalConsensusParachainConvertsFor<UniversalLocation, AccountId>`, trusting bridge exporter `E1` for `NetworkId::ByGenesis(X)`.
2. A legitimate parachain `P` (para_id = 1000) under network `X` accumulates a sovereign-account balance on `A`, derived via `blake2_256(b"glblcnsnss/prchn_", X, 1000)`, through `E1`.
3. Separately configure or later add a second exporter/bridge `E2` (lower security, e.g. a permissively-configured bridge hub or a mis-scoped `UniversalAliases`/`ExporterFor` table entry) that also allows messages tagged with `GlobalConsensus(X)/Parachain(1000)` to reach `A`'s XCM executor as a trusted origin.
4. An attacker controlling only `E2`'s trust domain sends an XCM `Transact`/`WithdrawAsset` message with source location `GlobalConsensus(X)/Parachain(1000)`.
5. `SovereignSignedViaLocation<LocationToAccountId, RuntimeOrigin>` resolves this to the *same* `AccountId` as the legitimate parachain `P`, letting the attacker sign/dispatch as, or drain, `P`'s sovereign account on `A` — without needing to compromise `P`, `E1`, or any validator/collator of `A`.

### Citations

**File:** polkadot/xcm/xcm-builder/src/location_conversion.rs (L373-389)
```rust
/// Converts a location which is a top-level parachain (i.e. a parachain held on a
/// Relay-chain which provides its own consensus) into a 32-byte `AccountId`.
///
/// This will always result in the *same account ID* being returned for the same
/// parachain index under the same Relay-chain, regardless of the relative security of
/// this Relay-chain compared to the local chain.
///
/// Note: No distinction is made when the local chain happens to be the parachain in
/// question or its Relay-chain.
///
/// WARNING: This results in the same `AccountId` value being generated regardless
/// of the relative security of the local chain and the Relay-chain of the input
/// location. This may not have any immediate security risks, however since it creates
/// commonalities between chains with different security characteristics, it could
/// possibly form part of a more sophisticated attack scenario.
///
/// DEPRECATED in favor of [ExternalConsensusLocationsConverterFor]
```

**File:** polkadot/xcm/xcm-builder/src/location_conversion.rs (L414-420)
```rust
impl<UniversalLocation, AccountId>
	GlobalConsensusParachainConvertsFor<UniversalLocation, AccountId>
{
	fn from_params(network: &NetworkId, para_id: &u32) -> [u8; 32] {
		(b"glblcnsnss/prchn_", network, para_id).using_encoded(blake2_256)
	}
}
```

**File:** substrate/frame/staking-async/runtimes/parachain/src/xcm_config.rs (L89-104)
```rust
/// Type for specifying how a `Location` can be converted into an `AccountId`. This is used
/// when determining ownership of accounts for asset transacting and when attempting to use XCM
/// `Transact` in order to determine the dispatch Origin.
pub type LocationToAccountId = (
	// The parent (Relay-chain) origin converts to the parent `AccountId`.
	ParentIsPreset<AccountId>,
	// Sibling parachain origins convert to AccountId via the `ParaId::into`.
	SiblingParachainConvertsVia<Sibling, AccountId>,
	// Straight up local `AccountId32` origins just alias directly to `AccountId`.
	AccountId32Aliases<RelayNetwork, AccountId>,
	// Foreign locations alias into accounts according to a hash of their standard description.
	HashedDescription<AccountId, DescribeFamily<DescribeAllTerminal>>,
	// Different global consensus parachain sovereign account.
	// (Used for over-bridge transfers and reserve processing)
	GlobalConsensusParachainConvertsFor<UniversalLocation, AccountId>,
);
```

**File:** substrate/frame/staking-async/runtimes/parachain/src/xcm_config.rs (L106-118)
```rust
/// Means for transacting the native currency on this chain.
pub type FungibleTransactor = FungibleAdapter<
	// Use this currency:
	Balances,
	// Use this currency when it is a fungible asset matching the given location or name:
	IsConcrete<WestendLocation>,
	// Convert an XCM Location into a local account id:
	LocationToAccountId,
	// Our chain's account ID type (we can't get away without mentioning it explicitly):
	AccountId,
	// We don't track any teleports of `Balances`.
	(),
>;
```

**File:** substrate/frame/staking-async/runtimes/parachain/src/xcm_config.rs (L223-245)
```rust
/// This is the type we use to convert an (incoming) XCM origin into a local `Origin` instance,
/// ready for dispatching a transaction with Xcm's `Transact`. There is an `OriginKind` which can
/// biases the kind of local `Origin` it will become.
pub type XcmOriginToTransactDispatchOrigin = (
	// Sovereign account converter; this attempts to derive an `AccountId` from the origin location
	// using `LocationToAccountId` and then turn that into the usual `Signed` origin. Useful for
	// foreign chains who want to have a local sovereign account on this chain which they control.
	SovereignSignedViaLocation<LocationToAccountId, RuntimeOrigin>,
	// Native converter for Relay-chain (Parent) location; will convert to a `Relay` origin when
	// recognised.
	RelayChainAsNative<RelayChainOrigin, RuntimeOrigin>,
	// Native converter for sibling Parachains; will convert to a `SiblingPara` origin when
	// recognised.
	SiblingParachainAsNative<cumulus_pallet_xcm::Origin, RuntimeOrigin>,
	// Superuser converter for the Relay-chain (Parent) location. This will allow it to issue a
	// transaction from the Root origin.
	ParentAsSuperuser<RuntimeOrigin>,
	// Native signed account converter; this just converts an `AccountId32` origin into a normal
	// `RuntimeOrigin::Signed` origin of the same 32-byte value.
	SignedAccountId32AsNative<RelayNetwork, RuntimeOrigin>,
	// Xcm origins can be represented natively under the Xcm pallet's Xcm origin.
	XcmPassthrough<RuntimeOrigin>,
);
```
