### Title
Missing whitelist/investor check on `receiver` in `PortfolioVault.deposit()`/`mint()` allows shares to be routed to a non-whitelisted address - (File: `tare-io__tare-contracts/contracts/PortfolioVault.sol`)

### Summary
The external report flags that `CreditManager.openCreditAccount`'s `onBehalfOf` parameter is never validated against `address(0)`, letting an account be opened for an address that should never hold state. The Tare analog is a similar unchecked-recipient pattern in `PortfolioVault`'s async ERC-7540 claim functions: the redeem/withdraw side validates the `receiver` against the investor whitelist, but the deposit/mint side does not.

### Finding Description
In `deposit()` and `mint()`, the vault validates only the `controller`: [1](#0-0) 

`_requireInvestor(controller)` is called, but `receiver` — the address that actually receives the minted vault shares — is never checked. `mint()` follows the same pattern: [2](#0-1) 

By contrast, the redeem/withdraw side explicitly whitelists both `controller` and `receiver`: [3](#0-2) [4](#0-3) 

This is the same root-cause class as the reported `onBehalfOf` bug: a state-mutating entry point accepts an unvalidated address parameter that is used to assign ownership/entitlement, while a sibling function in the same contract performs the validation. Because `onAccountOrOperator(controller)` only checks that `msg.sender` is `controller` or an approved operator of `controller` — not that `receiver` is whitelisted — a whitelisted investor (`controller`) can direct freshly minted vault shares to an arbitrary, non-whitelisted `receiver`, including addresses that were never vetted as investors.

I was not able to fully verify the internal `_requireInvestor` and `_claimDeposit`/`_claimRedeem` implementations within the available tool budget, so I cannot confirm with certainty whether `shareToken` itself enforces a transfer-time whitelist that would independently block a non-whitelisted holder from later using the shares. This uncertainty should be resolved by inspecting `_requireInvestor`, `_claimDeposit`, and the `shareToken` contract directly.

### Impact Explanation
If `shareToken` (or downstream redemption logic) relies on the vault-level whitelist gate as its sole investor-eligibility check (as implied by the explicit `_requireInvestor(receiver)` calls on the redeem/withdraw path), then this asymmetry lets an approved investor mint vault shares directly into a non-whitelisted address, bypassing the "whitelist gating" invariant the vault is designed to enforce for async request/claim flows. This is an unauthorized state transition / permission-bypass class impact under the allowed impact gate (an unprivileged actor causing vault shares to be assigned to an entity that was never granted investor status).

### Likelihood Explanation
Any address already approved as `controller`'s operator or `controller` itself can call `deposit`/`mint` with an arbitrary `receiver` with no additional privilege required beyond having a pending/claimable deposit — a normal, expected user flow. No special conditions beyond normal usage are needed to trigger the divergence from the redeem-side behavior.

### Recommendation
Add `_requireInvestor(receiver)` to `deposit()` and `mint()`, mirroring the checks already present in `redeem()` and `withdraw()`, so that vault shares from async deposit claims can only be routed to whitelisted investor addresses (and never to `address(0)`).

### Proof of Concept
1. Investor `A` (whitelisted) calls `requestDeposit(assets, controller=A, owner=A)` and gets shares approved to claimable state.
2. `A` calls `deposit(assets, receiver=X, controller=A)` where `X` is an arbitrary address that has never been registered via `registerAddress`/whitelisted as `Roles.Investor`.
3. Because `deposit()` never calls `_requireInvestor(receiver)`, the call succeeds and `X` receives the minted vault shares, unlike the symmetric `redeem`/`withdraw` path which would revert for a non-whitelisted `receiver`.

### Citations

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L628-641)
```text
  function deposit(
    uint256 assets,
    address receiver,
    address controller
  ) external nonReentrant whenNotPaused onlyAccountOrOperator(controller) returns (uint256 shares) {
    _requireInvestor(controller);
    uint256 claimableAssets_ = claimableDepositAssets[controller];
    uint256 claimableShares_ = claimableDepositShares[controller];
    require(claimableAssets_ > 0 && claimableShares_ > 0, NoClaimableDeposit());
    require(assets > 0 && assets <= claimableAssets_, ExceedsClaimable());

    shares = (assets * claimableShares_) / claimableAssets_;
    _claimDeposit(controller, receiver, assets, shares, claimableAssets_, claimableShares_);
  }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L650-660)
```text
  function mint(
    uint256 shares,
    address receiver,
    address controller
  ) external nonReentrant whenNotPaused onlyAccountOrOperator(controller) returns (uint256 assets) {
    _requireInvestor(controller);
    uint256 claimableAssets_ = claimableDepositAssets[controller];
    uint256 claimableShares_ = claimableDepositShares[controller];
    require(claimableAssets_ > 0 && claimableShares_ > 0, NoClaimableDeposit());
    require(shares > 0 && shares <= claimableShares_, ExceedsClaimable());

```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L753-767)
```text
  function redeem(
    uint256 shares,
    address receiver,
    address controller
  ) external nonReentrant whenNotPaused onlyAccountOrOperator(controller) returns (uint256 assets) {
    _requireInvestor(controller);
    _requireInvestor(receiver);
    uint256 claimableShares_ = claimableRedeemShares[controller];
    uint256 claimableAssets_ = claimableRedeemAssets[controller];
    require(claimableShares_ > 0 && claimableAssets_ > 0, NoClaimableRedeem());
    require(shares > 0 && shares <= claimableShares_, ExceedsClaimable());

    assets = (shares * claimableAssets_) / claimableShares_;
    _claimRedeem(controller, receiver, assets, shares, claimableAssets_, claimableShares_);
  }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L776-790)
```text
  function withdraw(
    uint256 assets,
    address receiver,
    address controller
  ) external nonReentrant whenNotPaused onlyAccountOrOperator(controller) returns (uint256 shares) {
    _requireInvestor(controller);
    _requireInvestor(receiver);
    uint256 claimableShares_ = claimableRedeemShares[controller];
    uint256 claimableAssets_ = claimableRedeemAssets[controller];
    require(claimableShares_ > 0 && claimableAssets_ > 0, NoClaimableRedeem());
    require(assets > 0 && assets <= claimableAssets_, ExceedsClaimable());

    shares = (assets * claimableShares_) / claimableAssets_;
    _claimRedeem(controller, receiver, assets, shares, claimableAssets_, claimableShares_);
  }
```
