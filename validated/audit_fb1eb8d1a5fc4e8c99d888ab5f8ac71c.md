### Title
Compromised/malicious controller can front-run `SHAREHOLDER_ROLE` revocation to claim or cancel pending vault requests before being blocked - (File: `contracts/PortfolioVault.sol`)

### Summary
`PortfolioVault`'s async ERC-7540 deposit/redeem flows rely on `VaultShareToken.SHAREHOLDER_ROLE` as the sole "block" mechanism for a controller believed to be compromised or malicious. The spec explicitly documents this as the intended security response: "If an address is compromised between these steps, the investor manager can revoke its `SHAREHOLDER_ROLE` to block the compromised address from claiming any assets or shares" [1](#0-0) . Because the revocation is a single, immediately-visible on-chain transaction with no timelock or pre-commit, and the claim/cancel functions (`deposit`, `mint`, `redeem`, `withdraw`, `cancelDepositRequest`, `cancelRedeemRequest`) execute atomically whenever the caller still holds `SHAREHOLDER_ROLE`, an attacker who observes the pending `revokeRole` transaction in the mempool can front-run it with a higher-gas call to claim their pending/claimable USDC or shares before the block takes effect — defeating the entire purpose of the "block a compromised controller" mechanism. This is the same root cause as the external report: a privileged block/seizure transaction can be front-run by the party being blocked because there is no pending-state lock or delay between the trigger and the block taking effect.

### Finding Description
The vault's investor-verification model uses `SHAREHOLDER_ROLE` (managed by `WHITELISTER_ROLE` on `VaultShareToken`) as the access-control gate for every claim and cancellation entry point on the vault: `_requireInvestor` is checked on `controller` (and `receiver` where applicable) in `deposit`, `mint`, `redeem`, `withdraw`, `cancelDepositRequest`, and `cancelRedeemRequest` [2](#0-1) . `SECURITY.md` item #31 documents that revoking `SHAREHOLDER_ROLE` while a controller has a pending or claimable request "freezes" it — the funds stay locked in the vault and are not lost, but only if the revocation transaction actually reaches the controller's account state before that controller acts [3](#0-2) .

However, none of the claim/cancel entry points are timelocked, rate-limited, or otherwise made atomic with the revocation. `requestDeposit`, `cancelDepositRequest`, `deposit`/`mint`, `requestRedeem`, `cancelRedeemRequest`, and `redeem`/`withdraw` are all plain externally-callable functions gated only by a live `hasRole(SHAREHOLDER_ROLE, account)` check at call time [4](#0-3) [5](#0-4) . Because the whitelister's `revokeRole(SHAREHOLDER_ROLE, controller)` transaction is visible in the public mempool before inclusion, a controller whose key is compromised (or who is otherwise the intended target of the block) can submit a competing transaction with higher gas to claim (`deposit`/`mint`/`redeem`/`withdraw`) or cancel (`cancelDepositRequest`/`cancelRedeemRequest`) their pending/claimable position and have it mined first, extracting the assets or shares before the role revocation lands.

This is structurally identical to the ToB finding: a manager/blocker-role transaction intended to freeze/seize a user's position can be front-run by the very depositor/controller being targeted, because the block and the withdrawal are two independent, unsynchronized transactions rather than a single atomic state transition or a delayed/committed freeze.

### Impact Explanation
When the investor manager revokes `SHAREHOLDER_ROLE` in response to a suspected key compromise, the intent is to prevent the holder of the compromised key from extracting the account's pending deposit (USDC) or claimable deposit (pre-minted shares) or pending/claimable redemption (locked shares / claimable USDC). An attacker who has compromised the controller's key can race the revocation and drain that value via `deposit`/`mint`/`redeem`/`withdraw` to any verified-shareholder receiver of their choosing, or unwind pending requests via `cancelDepositRequest`/`cancelRedeemRequest`, before the freeze takes effect. This is a theft/diversion of USDC or vault shares away from the legitimate account holder (the "honest user" whose key was compromised), which is within the allowed impact scope ("Theft, diversion, or unauthorized reassignment of USDC, vault assets, vault shares... from honest users"). It defeats the only on-chain incident-response control the protocol documents for this scenario, with no recovery once the funds have left the vault to an attacker-controlled but whitelisted address.

### Likelihood Explanation
Exploitation requires: (1) the investor manager to decide to revoke `SHAREHOLDER_ROLE` on a specific controller (a realistic, documented incident-response action), and (2) an attacker (holder of the compromised key, or a malicious controller anticipating being blocked) to be monitoring the mempool for the `VaultShareToken.revokeRole` transaction and react with a higher-gas/priority transaction. Given standard MEV/front-running tooling, this is straightforward to execute once the attacker suspects a block is imminent (e.g., after detecting suspicious on-chain manager activity or after the attacker's own compromise is likely to trigger a response). No special access is needed beyond retaining `SHAREHOLDER_ROLE` up to the moment of the race, which is exactly the precondition the revocation is trying to close.

### Recommendation
- **Short term:** Introduce a two-step freeze mechanism for suspected-compromise scenarios: an immediately-effective "pause claims for controller X" flag (settable by `INVESTOR_MANAGER`/`WHITELISTER_ROLE`) that is checked *before* any pending/claimable value is moved, separate from and faster-acting than `revokeRole`. Alternatively, route sensitive `revokeRole` calls for suspected compromise through a mechanism that first prevents `deposit`/`mint`/`redeem`/`withdraw`/`cancelDepositRequest`/`cancelRedeemRequest` (e.g., a per-controller freeze mapping checked at the very top of each function) so that the freeze and the block are atomic from the attacker's perspective, or submit the revocation via a private/protected relay to avoid mempool exposure.
- **Long term:** Analyze all privileged "block/seize/pause a specific actor" flows across the vault, loans, and exchange contracts for the same front-running exposure (e.g., `LoansAuth.unregisterAddress`, `revokeOriginator`/`revokeServicer` racing against `create`/`disburse`, and `TrustedSpender.removeDelegate` racing against `executeTransfer`), and standardize a freeze primitive that is atomic with respect to the actor being frozen.

### Proof of Concept
1. Attacker compromises the private key of `controller` (a whitelisted `SHAREHOLDER_ROLE` holder) who has a pending deposit: `PortfolioVault.requestDeposit(assets, controller, controller)` has already been called, so `pendingDepositAssets[controller] > 0` [4](#0-3) .
2. The `INVESTOR_MANAGER`/`WHITELISTER_ROLE` detects the compromise and submits `VaultShareToken.revokeRole(SHAREHOLDER_ROLE, controller)` to block the account, per the documented incident-response procedure [1](#0-0) .
3. The attacker observes this transaction in the mempool and immediately submits `PortfolioVault.cancelDepositRequest(controller, attackerControlledReceiver)` (or, if already approved, `deposit`/`mint`) with a higher gas price, targeting a receiver address that also holds `SHAREHOLDER_ROLE` [5](#0-4) .
4. The attacker's transaction is mined first, `_requireInvestor(controller)`/`_requireInvestor(receiver)` both still pass because the role has not yet been revoked, and the USDC (or shares) transfer completes.
5. The `revokeRole` transaction is mined afterward; the account is now blocked, but the funds have already left the vault to the attacker's chosen receiver — the protective freeze in `SECURITY.md` §31 and `vault.md`'s investor-verification rationale is bypassed.

### Citations

**File:** tare-io__tare-contracts/specs/vault.md (L101-103)
```markdown
Each address is verified at the appropriate step. Checks are either **explicit** (the vault calls `_requireInvestor()`) or **implicit** (the share token's `_update` hook reverts if the address lacks `SHAREHOLDER_ROLE`). The asset token (e.g. USDC) has no transfer restrictions, so asset transfers do not provide implicit checks.

**Why both controller and receiver are checked**: The deposit and redeem flows are asynchronous — time passes between request and claim. If an address is compromised between these steps, the investor manager can revoke its `SHAREHOLDER_ROLE` to block the compromised address from claiming any assets or shares. The controller check prevents the compromised address from initiating claims, and the receiver check prevents assets or shares from being directed to non-verified addresses.
```

**File:** tare-io__tare-contracts/specs/vault.md (L105-116)
```markdown
#### Verification Matrix

| Function               | `owner`                      | `controller` | `receiver`                   | Rationale                                                                                  |
| ---------------------- | ---------------------------- | ------------ | ---------------------------- | ------------------------------------------------------------------------------------------ |
| `requestDeposit`       | ✅ explicit                  | —            | —                            | Assets come from owner; must be verified. Controller deferred to claim time.               |
| `deposit` / `mint`     | —                            | ✅ explicit  | ✅ implicit (share transfer) | Controller directs the claim. Receiver enforced by share token `_update`.                  |
| `cancelDepositRequest` | —                            | ✅ explicit  | ✅ explicit                  | Controller authorizes cancellation. Receiver must be a verified investor (explicit check). |
| `requestRedeem`        | ✅ implicit (share transfer) | —            | —                            | Owner proven via `safeTransferFrom`. Controller deferred to claim time.                    |
| `redeem` / `withdraw`  | —                            | ✅ explicit  | ✅ explicit                  | No share transfer at claim — receiver gets assets. Both must be explicitly checked.        |
| `cancelRedeemRequest`  | —                            | ✅ explicit  | ✅ implicit (share transfer) | Controller authorizes cancellation. Receiver gets shares back via share token transfer.    |

**Vault self-exclusion**: The vault address itself holds `SHAREHOLDER_ROLE` (required for share custody during async flows), so `_requireInvestor` alone would accept it. All async functions therefore explicitly reject the vault as `controller` (`InvalidController`) on `requestDeposit`/`requestRedeem`, and as `receiver` (`InvalidReceiver`) on `deposit`, `mint`, `redeem`, `withdraw`, `cancelDepositRequest`, and `cancelRedeemRequest`. A request with the vault as controller would be unclaimable (no one can pass `onlyAccountOrOperator(vault)`), and a payout to the vault as receiver would be a self-donation.
```

**File:** tare-io__tare-contracts/SECURITY.md (L223-240)
```markdown
### 31 — Revoking `SHAREHOLDER_ROLE` freezes a controller's pending and claimable ERC-7540 requests

**Where:** [`PortfolioVault.cancelDepositRequest`](contracts/PortfolioVault.sol), [`PortfolioVault.deposit` / `mint`](contracts/PortfolioVault.sol), [`PortfolioVault.cancelRedeemRequest`](contracts/PortfolioVault.sol), [`PortfolioVault.redeem` / `withdraw`](contracts/PortfolioVault.sol)

Every claim and cancellation entry point on the vault calls `_requireInvestor(...)`, which checks `shareToken.hasRole(SHAREHOLDER_ROLE, account)`. If the controller's `SHAREHOLDER_ROLE` is revoked while they have:

- a **pending deposit** (`pendingDepositAssets[controller] > 0`) — `cancelDepositRequest` reverts (`_requireInvestor(controller)` and `_requireInvestor(receiver)`); their USDC stays locked in the vault.
- an **approved/claimable deposit** (`claimableDepositShares[controller] > 0`) — `deposit` / `mint` revert (`_requireInvestor(controller)`); their pre-minted shares stay held by the vault.
- a **pending redeem** (`pendingRedeemShares[controller] > 0`) — `cancelRedeemRequest` reverts; their share balance stays escrowed by the vault.
- an **approved/claimable redeem** (`claimableRedeemAssets[controller] > 0`) — `redeem` / `withdraw` revert (both `_requireInvestor(controller)` and `_requireInvestor(receiver)`); their USDC stays held by the vault.

The funds are not lost. Recovery paths, in order of preference:

1. Re-grant `SHAREHOLDER_ROLE` to the controller (admin/guardian on `VaultShareToken`) so they can claim or cancel themselves; revoke again afterwards.
2. Have the controller authorize an operator via `setOperator` before revocation, and route the cancel/claim through that operator (the operator still needs the controller and receiver to satisfy `_requireInvestor`, so this only helps if the role-grant strategy above is also used).
3. As a last resort, use `Rescuable` (admin/guardian) to sweep stuck assets or shares out of the vault to the recovery address — heavier than a per-request cancel, and it does not clear the controller's `pending*` / `claimable*` counters, so internal accounting will then disagree with on-chain balances (2 family of caveat).

The clean operational procedure is therefore: before revoking `SHAREHOLDER_ROLE`, ensure the controller has no pending or claimable requests (drain via the controller, or via their pre-authorized operator).
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L601-619)
```text
  /// @inheritdoc IERC7540Deposit
  function requestDeposit(
    uint256 assets,
    address controller,
    address owner
  ) external nonReentrant whenNotPaused onlyAccountOrOperator(owner) returns (uint256 requestId) {
    require(controller != address(this), InvalidController());
    _requireInvestor(owner);
    _requireInvestor(controller);
    require(assets > 0, ZeroAmount());

    assetToken.safeTransferFrom(owner, address(this), assets);

    pendingDepositAssets[controller] += assets;
    totalPendingDepositAssets += assets;

    emit DepositRequest(controller, owner, 0, msg.sender, assets);
    return 0;
  }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L666-682)
```text
  function cancelDepositRequest(
    address controller,
    address receiver
  ) external nonReentrant whenNotPaused onlyAccountOrOperator(controller) returns (uint256 assets) {
    _requireInvestor(controller);
    _requireInvestor(receiver);
    require(receiver != address(this), InvalidReceiver());
    assets = pendingDepositAssets[controller];
    require(assets > 0, NoPendingDeposit());

    pendingDepositAssets[controller] = 0;
    totalPendingDepositAssets -= assets;

    assetToken.safeTransfer(receiver, assets);

    emit DepositRequestCancelled(controller, receiver, assets);
  }
```
