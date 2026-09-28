### Title
Attacker can fill a victim's `depositTokensOfAccount` list with dust deposits via `DepositToken.deposit(amount_, onBehalfOf_)`, permanently blocking the victim from depositing new collateral types or borrowing new synthetics - (File: contracts/DepositToken.sol)

### Summary
`Pool` enforces `MAX_TOKENS_PER_USER = 30` combined deposit+debt tokens per account via `onlyIfAdditionWillNotReachMaxTokens`, reverting with `UserReachedMaxTokens` when full [1](#0-0) . `DepositToken.deposit` mints to an arbitrary `onBehalfOf_`, and `_mint` calls `pool.addToDepositTokensOfAccount(account_)` the first time an account's balance becomes nonzero [2](#0-1) . There is no minimum-deposit floor and no opt-in from the recipient, so any EOA can push 1-wei deposits of every listed deposit token into a victim's account list.

### Finding Description
Analogous to the CVE's "many small attacker-triggered allocations erode resources until the service fails," the attacker gradually fills a bounded per-account resource (`depositTokensOfAccount`, capped at 30) with untrusted dust entries:

1. Attacker buys/verifies dust amounts of each underlying listed in the pool and approves each `DepositToken`.
2. For each deposit token `D_i`: `D_i.deposit(1, victim)` — mints 1 wei of `msdTOKEN` to the victim, triggering `addToDepositTokensOfAccount(victim)` [3](#0-2) .
3. Since `addDepositToken` allows up to 30 deposit tokens per pool [4](#0-3) , the attacker can occupy all 30 slots.
4. After that, any action that would add a *new* token to the victim's list reverts inside `onlyIfAdditionWillNotReachMaxTokens`:
   - `deposit`/`transfer`/`transferFrom`/`seize` of a deposit token the victim doesn't already hold — `_mint`/`_transfer` revert through `addToDepositTokensOfAccount` [5](#0-4) .
   - `DebtToken.issue`/`flashIssue` for a synthetic the victim hasn't borrowed — `_mint` reverts through `addToDebtTokensOfAccount` [6](#0-5) .
   - `SmartFarmingManager.leverage` flows hitting new token types revert identically.

### Impact Explanation
The victim is denied the ability to open any position in a token type they do not already hold: no new collateral deposits, no new synthetic issuance, no leverage into new assets. If the victim's existing position becomes unhealthy and the needed action involves a new token, remediation is blocked. Recovery requires the victim to spend gas withdrawing/transferring the 30 dust balances one by one — a temporary freezing of funds/position functionality inflicted entirely by an unprivileged attacker at dust cost. Invariant broken: liveness of deposit/borrow entry points for the targeted account.

### Likelihood Explanation
Requires only an EOA holding dust of each listed underlying (1 wei each) and calling public `deposit`. No privileged role, no oracle manipulation, no timing constraint. Front-running is not needed; the attack persists until the victim manually clears each dust entry. It cannot steal existing funds directly, and victims can self-recover, so severity is moderate — matching the gradual-DoS nature of the source bug.

### Recommendation
- Enforce a minimum initial deposit/borrow amount (e.g. a `depositFloorInUsd` mirroring `debtFloorInUsd`) so list entries cannot be created with economically meaningless dust.
- Alternatively, charge entries only on self-initiated actions: track per-account lists only when `onBehalfOf_ == _msgSender()` or when the recipient has opted in; or make `addTo*TokensOfAccount` not count entries whose balance is below a dust threshold.
- Allow anyone to call a `removeDustEntries(account)`-style cleanup that removes tokens whose balance is below a threshold, so recovery is cheap.

### Proof of Concept
```solidity
// Foundry fork test sketch
function test_dustDepositFillsTokenList() public {
    // setup: pool with N deposit tokens D[0..N-1] already added by governor
    address victim = address(0xBEEF);

    for (uint i; i < N; ++i) {
        IERC20 underlying = depositTokens[i].underlying();
        deal(address(underlying), attacker, 1);
        vm.startPrank(attacker);
        underlying.approve(address(depositTokens[i]), 1);
        depositTokens[i].deposit(1, victim); // adds entry to victim's list
        vm.stopPrank();
    }
    assertEq(pool.getDepositTokensOfAccount(victim).length, N);

    // fill remaining slots with more tokens until combined count == 30
    // ...

    // victim can no longer deposit a collateral type they don't hold
    vm.expectRevert(IPool.UserReachedMaxTokens.selector);
    vm.prank(victim);
    newDepositToken.deposit(1e18, victim);

    // nor issue a synthetic they haven't borrowed
    vm.expectRevert(IPool.UserReachedMaxTokens.selector);
    vm.prank(victim);
    newDebtToken.issue(1e18, victim);
}
```
Note: if the pool lists fewer than 30 deposit tokens, the attacker can combine dust deposits with `transfer` dust of deposit tokens they hold to the same effect — every distinct first-time balance adds one entry.

### Citations

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/Pool.sol (L698-705)
```text
    function addDepositToken(address depositToken_) external onlyGovernor {
        if (depositToken_ == address(0)) revert AddressIsNull();
        IERC20 _underlying = IDepositToken(depositToken_).underlying();
        if (address(depositTokenOf[_underlying]) != address(0)) revert UnderlyingAssetInUse();
        // Note: Fee collector collects deposit tokens as fee
        if (depositTokens.length() >= MAX_TOKENS_PER_USER) revert ReachedMaxDepositTokens();

        if (!depositTokens.add(depositToken_)) revert DepositTokenAlreadyExists();
```

**File:** contracts/DepositToken.sol (L234-237)
```text
        _mint(onBehalfOf_, _deposited);

        emit CollateralDeposited(_msgSender, onBehalfOf_, amount_, _deposited, _fee);
    }
```

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```

**File:** contracts/DebtToken.sol (L597-600)
```text
        //  Add this token to the debt tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDebtTokensOfAccount(account_);
        }
```
