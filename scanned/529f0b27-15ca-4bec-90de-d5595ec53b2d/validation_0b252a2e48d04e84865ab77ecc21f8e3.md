## Title
Feeless `transfer` to a fresh purse key permanently blocks it via `pallet-scarcity`'s one-NFT-per-account invariant — analog of the zero-value position spam - (File: `substrate/frame/scarcity/src/lib.rs`, `substrate/frame/scarcity/src/extension.rs`)

## Summary
`pallet-scarcity` enforces a strict "one NFT per purse key" invariant (`ensure!(!NftsByOwner::<T>::contains_key(&to), Error::<T>::AddressOccupied)` in `do_mint_inner`) exactly analogous to the audited protocol's mutual-exclusion check (`shortPositions == 0` / `holdings == 0`). Any existing NFT holder can use the fee-less `AsScarcity` transaction extension to `transfer` their instance to an arbitrary, previously-empty account, permanently occupying that account's single "slot" without the recipient's consent and at zero cost to the sender beyond the original mint.

## Finding Description
The pallet's own module documentation admits the exact bug class from the report: "Any collection owner can mint into — or force-transfer an instance to — any empty purse key, and because each key holds at most one NFT, an unsolicited instance blocks that key from receiving anything else until its holder burns it or transfers it away" (`substrate/frame/scarcity/src/lib.rs:29-35`).

The `transfer` path is reachable by an ordinary NFT holder (not just the collection owner) via the `AsScarcity` transaction extension: `substrate/frame/scarcity/src/extension.rs:206-252` validates a transfer, checking only `TransferToSelf` and `DestinationOccupied` — there is no consent check from `to`, and this pipeline is documented to be **feeless** ("Transfers are feeless when authorized through the `AsScarcity` extension", `lib.rs:65`). This mirrors the report's core primitive: an attacker performs a state-changing action that costs them nothing meaningful and unilaterally consumes a scarce resource (the `NftsByOwner` slot) belonging to a victim who never opted in — exactly like opening a 0-value/0-collateral short and then transferring it to a victim to block their `openTrade(isLong=true)` call.

The mutual-exclusion guard hit by the victim is `do_mint_inner`'s:
```
ensure!(!NftsByOwner::<T>::contains_key(&to), Error::<T>::AddressOccupied);
```
(`substrate/frame/scarcity/src/lib.rs:1290`), which is structurally identical to the audited contract's `holdings == 0` / `shortPositions == 0` guards — a balance/possession check used to gate an action, which can be forced into a "blocked" state by an unsolicited transfer from a third party.

## Impact Explanation
Because `NftsByOwner` is keyed one-to-one per account and the transfer is fee-less and unilateral, any account (a fresh key, a victim's collection-owner account, a system/service account expecting to mint into a specific address) can be permanently prevented from ever holding (or minting) an NFT again until it notices the unsolicited instance and burns or moves it — i.e., a permanent, attacker-imposed denial-of-service on a specific account's ability to use the pallet, at the attacker's discretion and at negligible cost (feeless transfer). This matches the "permanent user-fund or bridge-state lock" / unauthorized state-mutation impact class: state is force-mutated against an account without its consent, altering that account's future capabilities exactly as in the audited report (blocking the counter-position from being opened).

## Likelihood Explanation
High: exploitation requires only (a) owning one instance (which can be obtained via any ordinary paid mint, or, if runtime pallets wire up `MintWithoutDeposit`, entirely free) and (b) calling `transfer` under `AsScarcity`, which is explicitly designed to be usable by any purse-key holder without special privilege and to be feeless. No governance, admin, relayer, or validator collusion is needed — this is a pure unprivileged public-entrypoint action, matching the "public-entrypoint path... unauthorized execution... fund loss or lock" acceptance criteria.

## Recommendation
Require destination consent (or at minimum, only allow depositing into `to` if `to` has pre-registered/opted into receiving, or provide a rejection/queue mechanism) before mutating `NftsByOwner` for a foreign account, rather than unconditionally occupying any empty slot. Alternatively, decouple "one instance per account" enforcement from unsolicited third-party transfers by allowing a receiving account to explicitly `accept`/`claim` an incoming transfer, and by making blocked accounts able to force-reject/burn incoming spam without cost, symmetric to the sender.

## Proof of Concept
1. Attacker mints (or free-mints via `MintWithoutDeposit`, if wired into a runtime pallet) an NFT instance `I` into their own purse key `A`.
2. Attacker signs a `transfer { to: V }` call under the `AsScarcity` extension where `V` is the victim's account (currently holding no NFT). Per `extension.rs::validate`, the only checks are `TransferToSelf` and `DestinationOccupied` — neither blocks transferring to an unwilling `V`.
3. The transfer executes fee-lessly (documented behavior), and `NftsByOwner::<T>::insert(V, nft)` succeeds inside `do_mint`/transfer's underlying storage mutation, matching `do_mint_inner`'s check at `lib.rs:1290`.
4. Any subsequent attempt by `V` to receive a legitimate mint or transfer now fails with `Error::<T>::AddressOccupied`, exactly paralleling the report's PoC where spamming 0-value short positions and transferring one to `user_2` caused `user_2`'s legitimate `openTrade` to be blocked (`shortPositions != 0`).

Note: I was not able to fully trace whether any currently-wired runtime in this repository exposes `MintWithoutDeposit` to an unprivileged caller (this would make step 1 also free); this remains unconfirmed from the available code and would require further review of the runtime pallet(s) that implement `crate::MintWithoutDeposit` consumers. The `transfer`-based griefing path itself, however, is confirmed directly from `pallet-scarcity`'s own code and documentation.