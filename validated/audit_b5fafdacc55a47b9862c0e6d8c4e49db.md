### Title
Dust-transfer griefing of `depositTokensOfAccount`/`debtTokensOfAccount` causes permanent `UserReachedMaxTokens` reverts, blocking deposits, issuance and leverage for the victim — (File: contracts/Pool.sol)

### Summary
`Pool` tracks every `DepositToken`/`DebtToken` an account has ever held in two bounded per-account sets capped at `MAX_TOKENS_PER_USER = 30` [1](#0-0) . Any addition reverts once the combined length reaches 30 [2](#0-1) . Because `DepositToken` is a freely transferable ERC-20 and `_transfer` adds the token to the *recipient's* set whenever their prior balance is zero [3](#0-2) , an unprivileged attacker can permanently fill a victim's slot budget with dust and cause all future first-time mints/transfers/issues to revert — the same availability-loss bug class as CVE-2016-8327 (hang/crash DoS).

### Finding Description
- `DepositToken.transfer`/`transferFrom` only check the *sender's* unlocked balance via `_revertIfLocked` [4](#0-3) . The recipient needs no consent.
- On receiving a first-time balance, `_transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` [3](#0-2) , which enforces `debtTokensOfAccount.length + depositTokensOfAccount.length < 30` [5](#0-4) .
- The attacker deposits 1 wei of underlying into each listed `DepositToken` (the pool allows up to 30 deposit tokens via `addDepositToken` [6](#0-5) ) and transfers the dust `msdTOKEN`s to the victim. The victim's set is now full.
- Consequences for the victim, all reverting with `UserReachedMaxTokens`:
  - `deposit()` into any deposit token they don't already hold — `_mint` → `addToDepositTokensOfAccount` [7](#0-6) .
  - `DebtToken.issue` of any synthetic they don't already owe (same modifier via `addToDebtTokensOfAccount` [8](#0-7) ).
  - Any `transfer`/`seize` of a new token type *to* them.
- Persistence amplifier: a slot is only freed when the balance goes fully to zero [9](#0-8) . If the victim has open debt, `unlockedBalanceOf` may return less than the dust amount [10](#0-9) , so the victim cannot even transfer the dust away — the DoS persists until they repay debt, and `transferFrom` of the dust by an approved spender can't be assumed.
- The same attack applies to SmartFarmingManager position accounts and `feeCollector`: dusting them blocks first-time fee-mints and leverage deposits of new collateral types, degrading those features pool-wide until the dust is cleared.

### Impact Explanation
Invariant broken: liveness/availability — the victim is denied deposits, new debt issuance, leverage, and receipt of new deposit-token types. For victims with locked collateral the state cannot be self-healed (dust is locked), so the feature freeze persists. If `feeCollector` or shared SmartFarmingManager accounts are targeted, liquidations carrying a protocol fee (`depositToken_.seize(account_, feeCollector, _fee)` [11](#0-10) ) for *newly added* deposit-token types revert, temporarily stalling liquidations — matching the CVE's "hang or repeatable crash" availability impact. This is a temporary-to-persistent freezing of protocol functionality, not gas-loop DoS.

### Likelihood Explanation
Fully unprivileged: any EOA can call `deposit` and `transfer` [12](#0-11) . Cost is ~30 dust deposits + 30 transfers. No privileged role, oracle manipulation, or governance action required. The only mitigation is that already-held token types are unaffected, so impact is bounded to *new* token types per victim — the reason severity stays Medium rather than High.

### Recommendation
- Make the per-account bound non-griefable: skip the `MAX_TOKENS_PER_USER` check on receipt of externally pushed dust (e.g., only enforce on `deposit`/`issue` initiated by the account itself or via its position contract), or allow a permissionless "remove zero/dust token" cleanup.
- Alternatively, exclude `feeCollector`/`Treasury`/SmartFarmingManager position contracts from the cap, and let recipients purge unwanted tokens regardless of lock status (e.g., a `sweepDust` that ignores `_revertIfLocked` for transfers to `address(0)` burn or treasury).

### Proof of Concept
Foundry fork test outline:

```solidity
// contracts: Pool, DepositToken(s), DebtToken as deployed (see deployments/mainnet/Pool.json)
function test_DustGrief_FillsTokenList() public {
    address victim = makeAddr("victim");
    address[] memory dts = pool.getDepositTokens(); // up to 30 listed collaterals

    // Attacker: deposit 1 wei underlying into each DepositToken, then dust victim
    for (uint i; i < dts.length; ++i) {
        IDepositToken dt = IDepositToken(dts[i]);
        deal(address(dt.underlying()), attacker, 1);
        IERC20(dt.underlying()).approve(address(dt), 1);
        dt.deposit(1, attacker);
        dt.transfer(victim, dt.balanceOf(attacker)); // addToDepositTokensOfAccount(victim)
    }

    // victim's set is now at MAX_TOKENS_PER_USER
    assertEq(pool.getDepositTokensOfAccount(victim).length, 30);

    // 1) Victim cannot deposit into any collateral type not already held
    vm.prank(victim);
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    IDepositToken(newDepositToken).deposit(100e18, victim);

    // 2) Victim cannot issue a synthetic they don't already owe
    vm.prank(victim);
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    IDebtToken(newDebtToken).issue(1e18, victim); // mint -> addToDebtTokensOfAccount reverts

    // 3) If victim's collateral is locked by debt, unlockedBalanceOf(victim) < dust,
    //    so victim cannot even transfer the dust out -> persistent DoS until repay.
}
```

Note: the strongest variant (griefing `feeCollector` to block fee-bearing liquidations of *newly listed* deposit tokens) depends on governance adding new collateral after the dusting; the per-victim variant is unconditional but only blocks token types the victim doesn't already hold.

### Citations

**File:** contracts/Pool.sol (L79-79)
```text
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

**File:** contracts/Pool.sol (L204-208)
```text
    function addToDebtTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _debtToken = _msgSender();
        _revertIfSenderIsNotDebtToken(_debtToken);
        if (!debtTokensOfAccount.add(account_, _debtToken)) revert DebtTokenAlreadyExists();
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

**File:** contracts/Pool.sol (L591-593)
```text
        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
        }
```

**File:** contracts/Pool.sol (L703-705)
```text
        if (depositTokens.length() >= MAX_TOKENS_PER_USER) revert ReachedMaxDepositTokens();

        if (!depositTokens.add(depositToken_)) revert DepositTokenAlreadyExists();
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

**File:** contracts/DepositToken.sol (L348-353)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
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

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```

**File:** contracts/DepositToken.sol (L522-525)
```text
        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```
