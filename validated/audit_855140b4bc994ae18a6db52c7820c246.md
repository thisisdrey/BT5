### Title
Griefing via dust `DepositToken` transfers fills a victim's `depositTokensOfAccount` list and blocks deposits/receipts of new collateral types (temporary freezing of user funds) - (contracts/DepositToken.sol)

### Summary
BIT-java-2021-35561 is an unauthenticated, low-cost partial denial-of-service class. The strongest Metronome analog is a per-account liveness/availability break reachable by any EOA: `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance moves from 0, and `Pool.addToDepositTokensOfAccount` enforces `MAX_TOKENS_PER_USER = 30` via `onlyIfAdditionWillNotReachMaxTokens`, reverting once the combined `debtTokensOfAccount + depositTokensOfAccount` count reaches 30. An attacker can deposit dust in every supported collateral and `transfer` 1 wei of each `msdTOKEN` to a victim, irreversibly (for the duration) filling the victim's account set. After that, every code path that would add a new entry to the victim's list reverts with `UserReachedMaxTokens`: `deposit(amount, onBehalfOf = victim)` from any depositor, `transfer`/`transferFrom` to the victim for any `msdTOKEN` the victim does not already hold, `seize(account, victim, ...)` during liquidations, and `_mint(victim)` for a new collateral — i.e., the victim cannot onboard any additional collateral type and cannot receive any new deposit token, which is a partial DoS of the position-management surface.

### Finding Description
- `DepositToken._transfer` adds the token to the recipient's per-account set on a 0→positive balance transition, with no opt-in and no minimum amount: [1](#0-0) 
- `Pool.addToDepositTokensOfAccount` is gated by `onlyIfAdditionWillNotReachMaxTokens`, which reverts at `MAX_TOKENS_PER_USER = 30` entries (debt + deposit tokens combined): [2](#0-1) [3](#0-2) 
- `DepositToken.deposit` allows `onBehalfOf_` to be any non-zero address, so minting to a victim is possible; conversely `_mint` also calls `addToDepositTokensOfAccount`, so once the set is full, *any* deposit that would register a new token for the victim reverts: [4](#0-3) 
- Entry point is permissionless: `transfer(to_, amount_)` only checks `_revertIfLocked` on the *sender* (attacker), never on the recipient: [5](#0-4) 

Attack sequence (all public entry points, unprivileged EOA):
1. For each registered `DepositToken` in the pool, attacker deposits a minimal amount (or acquires dust) to their own address.
2. Attacker calls `msdTOKEN_i.transfer(victim, 1)` for up to `30 - len(victim's list)` distinct deposit tokens. Each transfer registers token `i` in `depositTokensOfAccount[victim]`.
3. Once the count hits 30, every subsequent path that would append a new token for the victim reverts with `UserReachedMaxTokens`:
   - `deposit(..., onBehalfOf_=victim)` for any collateral not already in the victim's set (`_mint` → `addToDepositTokensOfAccount` → revert);
   - `transfer`/`transferFrom` of any new `msdTOKEN` to the victim (whole tx reverts, so even legitimate senders cannot pay the victim);
   - `seize(..., to_=victim, ...)` in `Pool.liquidate` if the liquidator route credits a new token type to the victim account.

### Impact Explanation
Availability/liveness break against a specific user, matching the CVE's "partial DoS" class: the victim is denied onboarding of new collateral types and receipt of deposit tokens (including liquidation proceeds routed to them) until they unwind the dust. Recovery requires the victim to fully withdraw or transfer out each dust `msdTOKEN` (`_burn`/`_transfer` removes the entry when the balance hits zero), which is possible but costs gas per token and — critically — the attacker can front-run the victim's cleanup transaction to refill a slot, extending the freeze. During the freeze, a victim whose position approaches liquidation cannot top up with a *different* collateral to restore health, turning a liveness bug into potential forced liquidation/bad-debt exposure. This is temporary freezing of funds plus denial of deposit/transfer functionality, not merely a gas/unbounded-loop issue — the revert is caused by a hard cap, not OOG.

### Likelihood Explanation
- Fully unprivileged: any EOA; no governor/guardian/keeper/oracle role needed. `transfer` is public and only checks the sender's unlocked balance.
- Cheap: 1 wei of each underlying (post-fee) per token is enough; `amount_ > 0` is the only trigger.
- Feasible whenever the pool has ≥2 deposit tokens and the victim has <30 entries; more deposit tokens registered = more slots fillable. The shared 30-slot budget with `debtTokensOfAccount` means an attacker can also combine with dust debt issuance vectors if reachable.
- Not stopped by existing guards: `nonReentrant` (only on `transferFrom`), `whenNotPaused`, `SynthContext`, and supply caps do not constrain recipient-side list growth. The only self-limit is that the attacker must acquire each token first.
- Limitation: impact is per-victim (partial), recoverable by the victim at dust cost (subject to refill front-running), and does not directly steal funds — consistent with Medium severity.

### Recommendation
- Do not add tokens to `depositTokensOfAccount` on plain `transfer`/`transferFrom`/`seize` receipts; only register on `_mint` (deposit), or make registration opt-in (e.g., `claim`/`enableToken` by the account itself).
- Alternatively, enforce a meaningful minimum dust threshold for adding to the set, or store per-account sets uncapped (the `MAX_TOKENS_PER_USER` bound exists to keep `debtPositionOf`/`unlockedBalanceOf` loops bounded — if retained, restrict the cap to tokens the account actively deposited).
- At minimum, allow `seize` to bypass the cap so liquidations cannot be bricked, and consider letting `addToDepositTokensOfAccount` silently skip instead of reverting for non-deposit paths.

### Proof of Concept
Foundry fork-style sketch (against deployed `Pool` + any two+ `DepositToken`s):

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";

contract DustGriefingTest is Test {
    address victim = makeAddr("victim");
    address attacker = makeAddr("attacker");

    function test_dustFillBlocksNewDeposits() public {
        // depositTokens: array of registered IDepositToken for the pool (pool.getDepositTokens())
        // underlying_i: IDepositToken(depositTokens[i]).underlying()

        // Setup: victim holds some collateral normally
        // ...

        vm.startPrank(attacker);
        // 1) Attacker mints dust of each deposit token to self
        for (uint i; i < depositTokens.length; ++i) {
            IERC20 underlying = depositTokens[i].underlying();
            deal(address(underlying), attacker, 1 ether);
            underlying.approve(address(depositTokens[i]), type(uint256).max);
            depositTokens[i].deposit(1, attacker); // 1 wei minted to attacker
        }
        // 2) Attacker pushes 1 wei of each msdTOKEN to victim until set is full (30)
        for (uint i; i < depositTokens.length; ++i) {
            depositTokens[i].transfer(victim, 1);
        }
        vm.stopPrank();

        assertEq(pool.getDepositTokensOfAccount(victim).length, pool.MAX_TOKENS_PER_USER());

        // 3) Any deposit crediting a NEW token to victim reverts
        vm.startPrank(attacker);
        IERC20 underlying0 = depositTokens[0].underlying();
        underlying0.approve(address(depositTokens[0]), 1 ether);
        vm.expectRevert(Pool.UserReachedMaxTokens.selector);
        depositTokens[0].deposit(1 ether, victim);

        // 4) Transfers of a not-yet-held token to victim revert
        vm.expectRevert(Pool.UserReachedMaxTokens.selector);
        depositTokens[0].transferFrom(attacker2, victim, 1); // where attacker2 holds token0 and victim's balance of a *new* token is 0
        vm.stopPrank();
    }
}
```

Notes/caveats: the PoC assumes the pool exposes enough distinct deposit tokens to saturate the victim's remaining free slots (up to 30); with fewer registered collaterals the attacker fills as many as exist, and debt-token dust (if a small `issue` is reachable for the victim via `onBehalfOf`-style paths — `DebtToken.issue` mints to the caller, so the attacker would instead need collateral-per-debt-token transfers if debt tokens also add recipients on transfer, which was not fully verified) can compound the fill. The victim can recover by zeroing each dust balance, but the attacker can re-fill in the same block via front-running, so the freeze is effectively repeatable at dust cost.

### Citations

**File:** contracts/DepositToken.sol (L348-354)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }
```

**File:** contracts/DepositToken.sol (L483-488)
```text
        emit Transfer(address(0), account_, amount_);

        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L517-525)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/Pool.sol (L216-220)
```text
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```
