### Title
Attacker can permanently block a victim's account from receiving new deposit/debt token types by stuffing `depositTokensOfAccount` to `MAX_TOKENS_PER_USER` via dust transfers - (File: contracts/Pool.sol)

### Summary
The reference CVE is a resource-exhaustion DoS: each malformed response leaks a small amount of a finite resource until the process dies. The direct analog in Metronome is the per-account token registry. Every time a `DepositToken` balance goes from 0 to positive, `Pool.addToDepositTokensOfAccount` pushes the token into a bounded per-account list capped at `MAX_TOKENS_PER_USER = 30` (shared between deposit and debt tokens) [1](#0-0) . An unprivileged attacker can add entries to *any* victim's list at near-zero cost via `DepositToken.transfer`, because `_transfer` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance was 0 [2](#0-1) . Once the combined list length hits 30, every subsequent first-time mint/transfer/issue to the victim reverts with `UserReachedMaxTokens` [3](#0-2) .

### Finding Description
- `Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` are guarded by `onlyIfAdditionWillNotReachMaxTokens`, which reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` [4](#0-3) .
- `DepositToken._transfer` adds the token to the *recipient's* list with no consent mechanism and no minimum amount [5](#0-4) .
- `DepositToken._mint` does the same for recipients of a `deposit` (`onBehalfOf_`) [6](#0-5) .
- `DebtToken` mint/issue paths call `addToDebtTokensOfAccount`, so the same cap also blocks the victim from issuing a *new* synthetic asset.

Attack path (all public, no privileges):
1. Attacker deposits a dust amount into each of the pool's deposit tokens (`deposit(1 wei, attacker)`).
2. Attacker calls `depositToken.transfer(victim, 1)` for each distinct deposit token. Each call pushes one entry into `depositTokensOfAccount[victim]`.
3. If the pool lists enough deposit tokens (or the victim already holds some positions so that `deposit + debt` entries reach 30), the victim's list hits the cap.
4. Thereafter: `deposit(amount, victim)` for any deposit token the victim doesn't already hold reverts; `issue`/`mint` of any new synthetic reverts; transfers of not-yet-held deposit tokens to the victim revert; `seize` to a liquidator whose own list is stuffed also reverts (self-inflicted, but shows the invariant is global).

### Impact Explanation
Liveness / temporary-to-partial freezing of funds. A stuffed victim cannot add new collateral types to their position. If the victim's existing collateral factor drops (oracle move) and they need a *different* collateral type to restore health, every such deposit reverts, leaving the position to be liquidated when it could have been rescued — indirect loss of funds. Additionally, an attacker can keep re-stuffing the list (each eviction the victim performs can be immediately refilled with a 1-wei transfer of another token), making the DoS persistent for as long as the attacker is willing to pay gas. The victim can remove entries only by zeroing the dust balances, which may be impossible for the portion covered by `lockedBalanceOf` when the position has debt [7](#0-6) . Analog to the CVE: a small, repeatable, attacker-controlled allocation exhausts a fixed resource until functionality fails.

### Likelihood Explanation
Requires the pool to list enough deposit tokens so that the victim's combined list can reach 30 — the attacker can only add deposit-token entries (debt tokens cannot be force-added to a victim). On deployments with few collateral types the cap is unreachable and the attack fails; on pools with many listed deposit tokens it is cheap (gas + dust collateral, no capital at risk, dust is recoverable). Attack cost is linear and small; no privileged role, oracle manipulation, or timing is needed. Mitigating factor: the victim can clear entries by transferring/withdrawing the dust, so the attack is a contest of gas and only "freezes" the victim's ability to open *new* token positions rather than locking existing balances outright — hence Medium rather than High.

### Recommendation
- Only add a token to `depositTokensOfAccount`/`debtTokensOfAccount` through trusted, balance-meaningful paths — e.g., require a minimum amount, or track list membership lazily instead of on every 0→+ balance change.
- Alternatively, let an account opt out of receiving new token types, or allow the account itself (not only the token contract) to call a `removeFromDepositTokensOfAccount`-style cleanup so eviction is not gated behind token transfers of locked dust.
- Raise the cap or split the shared cap between deposit and debt lists so stuffing requires more attacker effort.

### Proof of Concept
Foundry test sketch (run against the real `Pool`/`DepositToken` wiring):

```solidity
function testDustListStuffing() public {
    // pool has depositTokens[] listed by governor; attacker needs no roles
    address victim = makeAddr("victim");

    for (uint i; i < depositTokens.length; ++i) {
        DepositToken dt = depositTokens[i];
        // attacker deposits dust and transfers 1 wei to victim
        deal(address(dt.underlying()), attacker, 1);
        vm.startPrank(attacker);
        dt.underlying().approve(address(dt), 1);
        dt.deposit(1, attacker);
        dt.transfer(victim, 1);
        vm.stopPrank();
    }

    assertEq(pool.getDepositTokensOfAccount(victim).length, depositTokens.length);

    // repeat with remaining capacity via other listed tokens until 30 entries
    // then: victim cannot deposit a collateral type they don't already hold
    vm.startPrank(victim);
    newUnderlying.approve(address(newDepositToken), 100e18);
    vm.expectRevert(IPool.UserReachedMaxTokens.selector);
    newDepositToken.deposit(100e18, victim);

    // and cannot issue a new synthetic (debt list shares the same cap)
    vm.expectRevert(IPool.UserReachedMaxTokens.selector);
    newDebtToken.issue(1e18, victim); // or Pool.mint path
    vm.stopPrank();
}
```

The PoC shows: unprivileged entry point (`transfer`), no health/pause/guard prevents the insertion, and the `UserReachedMaxTokens` revert blocks new-position operations for the victim until the dust entries are evicted — which the attacker can continually re-fill.

### Citations

**File:** contracts/Pool.sol (L79-79)
```text
    uint256 public constant MAX_TOKENS_PER_USER = 30;
```

**File:** contracts/Pool.sol (L143-147)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
```

**File:** contracts/Pool.sol (L204-220)
```text
    function addToDebtTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _debtToken = _msgSender();
        _revertIfSenderIsNotDebtToken(_debtToken);
        if (!debtTokensOfAccount.add(account_, _debtToken)) revert DebtTokenAlreadyExists();
    }

    /**
     * @notice Add a deposit token to the per-account list
     * @dev This function is called from `DepositToken` when user's balance changes from `0`
     * @dev The caller should ensure to not pass `address(0)` as `_account`
     * @param account_ The account address
     */
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```

**File:** contracts/DepositToken.sol (L383-398)
```text
    function unlockedBalanceOf(address account_) public view override returns (uint256 _unlockedBalance) {
        IPool _pool = pool;

        (, , uint256 _debtInUsd, , uint256 _issuableInUsd) = _pool.debtPositionOf(account_);

        if (_debtInUsd == 0) {
            return balanceOf[account_];
        }

        if (_issuableInUsd > 0) {
            _unlockedBalance = Math.min(
                balanceOf[account_],
                _pool.masterOracle().quoteUsdToToken(address(underlying), _issuableInUsd.wadDiv(collateralFactor))
            );
        }
    }
```

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L498-525)
```text
    function _transfer(
        address sender_,
        address recipient_,
        uint256 amount_
    ) private updateRewardsBeforeTransfer(sender_, recipient_) {
        if (sender_ == address(0)) revert TransferFromTheZeroAddress();
        if (recipient_ == address(0)) revert TransferToTheZeroAddress();

        uint256 _senderBalanceBefore = balanceOf[sender_];
        if (_senderBalanceBefore < amount_) revert TransferAmountExceedsBalance();
        uint256 _recipientBalanceBefore = balanceOf[recipient_];

        unchecked {
            balanceOf[sender_] = _senderBalanceBefore - amount_;
            balanceOf[recipient_] += amount_;
        }

        emit Transfer(sender_, recipient_, amount_);

        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```
