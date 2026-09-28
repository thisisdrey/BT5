### Title
Attacker can fill a victim's per-account token list to `MAX_TOKENS_PER_USER` via permissionless `deposit(onBehalfOf)`, blocking new collateral deposits and inbound transfers - ([File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
The bug class is a cap that is enforced on the "creation" side but creates an inconsistent/unhandled condition on the "consumption" side: `Pool` enforces `MAX_TOKENS_PER_USER` (30) inside `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount`, but the *caller-side* paths that trigger the insertion — `DepositToken._mint` and `DepositToken._transfer` — can be invoked by anyone on behalf of an arbitrary victim via `deposit(amount_, onBehalfOf_)` or `transfer`. An unprivileged attacker can therefore push the victim's combined `debtTokensOfAccount + depositTokensOfAccount` list to the cap with dust, after which every subsequent first-time deposit, inbound transfer, liquidation seize target, or SmartFarmingManager leverage deposit that would add a new token to the victim's list reverts with `UserReachedMaxTokens`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

### Finding Description
- `Pool.addToDepositTokensOfAccount` is guarded by `onlyIfAdditionWillNotReachMaxTokens`, which reverts once `debtTokensOfAccount.length(account) + depositTokensOfAccount.length(account) >= 30`.
- `DepositToken._mint` inserts the token into `account_`'s list whenever the recipient's prior balance was 0. `deposit(amount_, onBehalfOf_)` is public, permissionless, and mints to an arbitrary `onBehalfOf_` — the attacker pays the underlying, the victim's list grows.
- `DepositToken._transfer` does the same for the recipient of any `transfer`/`transferFrom`, and `seize` (called by `Pool.liquidate`) routes through the same path.
- There is no opt-out, no minimum-amount check, and no way for the victim to prevent entries being added. Removal only happens when the victim's balance for that token returns to exactly 0 (`_burn`/`_transfer` → `removeFromDepositTokensOfAccount`), which requires the balance to be *unlocked* — i.e., `unlockedBalanceOf(account_)` must be ≥ the dust amount.
- If the victim has outstanding debt such that `unlockedBalanceOf` is 0 (or less than the dust), the victim cannot withdraw or transfer the dust out and cannot free the slots while the debt persists — the grief becomes sticky.

### Impact Explanation
Once the victim's list is at the cap:
- `depositToken.deposit(..., victim)` for any collateral the victim does not already hold reverts — the victim cannot add new collateral types to improve a risky position or open a new position type.
- Any `transfer`/`transferFrom` of a deposit token the victim does not already hold reverts.
- `SmartFarmingManager` leverage paths that mint deposit tokens to the user revert for new token types.
- Liquidators are unaffected (seized tokens go to the liquidator), so this does not break the liquidation invariant; the impact is temporary freezing of the victim's ability to receive/deposit collateral. If the victim has debt and zero unlocked balance in the dust tokens, the freeze persists until the victim repays debt — during which a worsening market cannot be countered by adding a *new* collateral type, raising insolvency risk for the protocol if the position goes underwater.

### Likelihood Explanation
- Attacker is an unprivileged EOA; `deposit(amount_, onBehalfOf_)` and `transfer` are public and `whenNotPaused`-only gated.
- Cost is dust amounts of the underlying for each deposit token registered in the pool; feasibility scales with the number of `depositTokens` registered (each slot needs a distinct token the victim doesn't already hold). On deployments with many deposit tokens, filling 30 slots minus the victim's existing tokens is cheap.
- No privileged role, oracle manipulation, or trusted-party assumption is needed. The `nonReentrant`, `onlyIfDepositTokenExists`, and SynthContext checks do not interfere.

### Recommendation
Do not count attacker-forced additions the same way as user-initiated ones. Options:
- In `Pool.addToDepositTokensOfAccount`, skip (rather than revert) when the list is full and the balance delta is below a dust threshold; or
- Make `_mint`/`_transfer` not revert on a full list — e.g., have `addToDepositTokensOfAccount` return a bool and only track tokens for positions the user opted into; or
- Require a minimum deposit amount per token so dust-filling is economically infeasible, and/or allow `removeFromDepositTokensOfAccount` to be triggered by the account itself (a `sweep`/`forgetToken` escape hatch) so a victim can always clear forced entries.

### Proof of Concept
Foundry/Hardhat fork against a deployed `Pool` with `N` registered `DepositToken`s:

```solidity
// victim holds, say, msdA + msxDebt (2 slots used). MAX_TOKENS_PER_USER = 30.
// attacker approves and deposits dust of each other deposit token on behalf of victim:
for (uint256 i; i < pool.getDepositTokens().length; ++i) {
    DepositToken dt = DepositToken(pool.getDepositTokens()[i]);
    if (dt.balanceOf(victim) == 0) {
        underlying(dt).approve(address(dt), DUST);
        dt.deposit(DUST, victim); // mints dust msdTOKEN to victim, adds to victim's list
    }
}
// assert: pool.getDepositTokensOfAccount(victim).length + debtTokens == 30

// victim now tries to deposit a collateral type they don't yet hold:
vm.prank(victim);
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
newDepositToken.deposit(1e18, victim);

// inbound transfer of a new msdTOKEN also reverts:
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
otherDepositToken.transferFrom(holder, victim, 1);

// if victim has debt and unlockedBalanceOf(dustToken) == 0, victim cannot
// withdraw the dust -> slots stay occupied until debt is repaid.
```

Key assertions: `addToDepositTokensOfAccount` reverts via `onlyIfAdditionWillNotReachMaxTokens` (Pool.sol:143-148), while the insertion is reachable from permissionless `deposit`/`transfer` (DepositToken.sol:214, 348, 486-488, 517-520), and removal requires a zero balance plus unlocked funds (DepositToken.sol:460-462, `unlockedBalanceOf`).

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

**File:** contracts/Pool.sol (L216-220)
```text
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```

**File:** contracts/DepositToken.sol (L211-237)
```text
    function deposit(
        uint256 amount_,
        address onBehalfOf_
    ) external override whenNotPaused nonReentrant onlyIfDepositTokenExists returns (uint256 _deposited, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();
        if (onBehalfOf_ == address(0)) revert BeneficiaryIsNull();

        IPool _pool = pool;
        IERC20 _underlying = underlying;
        address _msgSender = _msgSender();
        address _treasury = address(_pool.treasury());

        if (_msgSender == _treasury) revert TreasuryCanNotDeposit();

        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;

        (_deposited, _fee) = quoteDepositOut(amount_);
        if (_fee > 0) {
            _mint(_pool.feeCollector(), _fee);
        }

        _mint(onBehalfOf_, _deposited);

        emit CollateralDeposited(_msgSender, onBehalfOf_, amount_, _deposited, _fee);
    }
```

**File:** contracts/DepositToken.sol (L486-488)
```text
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
