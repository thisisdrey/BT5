### Title
Unblockable dust transfers fill a victim's token list (`UserReachedMaxTokens`), DoS-ing deposits, issuance, and liquidations - (File: contracts/Pool.sol)

### Summary
`DepositToken._transfer` and `DepositToken.deposit` unconditionally call `Pool.addToDepositTokensOfAccount(recipient_)`, which reverts with `UserReachedMaxTokens()` once `debtTokensOfAccount + depositTokensOfAccount` reaches `MAX_TOKENS_PER_USER = 30`. Because msdTOKEN transfers are permissionless and push-based (no recipient consent or opt-in), an unprivileged attacker can force dust balances of every registered deposit token onto any address — victims, known liquidator bots, or the `feeCollector` — permanently reverts any subsequent operation that would add a new token entry for that address. This is the Metronome analog of CVE-2020-0198: arithmetic/state-driven abort reachable by an unprivileged remote party causing denial of service.

### Finding Description [1](#0-0)  enforces a hard cap of 30 combined debt + deposit token entries per account. The add path is invoked automatically:

- `DepositToken._transfer` adds the token to the recipient's list whenever their balance moves from 0 to non-zero, with no approval from the recipient [2](#0-1) .
- `DepositToken.deposit(amount_, onBehalfOf_)` lets anyone mint deposit tokens to an arbitrary `onBehalfOf_` address [3](#0-2) .

Attack steps:

1. Attacker deposits (or obtains via `transfer`) dust amounts of each of the pool's registered `DepositToken`s.
2. Attacker calls `transfer(victim, 1 wei)` for each token until the victim's combined list length hits 30.
3. Afterwards, any victim action that adds a *new* token reverts: `deposit()` of a collateral type they don't already hold, and `DebtToken.issue()`/`_mint` for a synthetic they don't already owe (`addToDebtTokensOfAccount` shares the same 30-slot budget, checked in `onlyIfAdditionWillNotReachMaxTokens`) [4](#0-3) .
4. The attacker can likewise dust `pool.feeCollector()` and known liquidator addresses. `Pool.liquidate` calls `depositToken_.seize(account_, feeCollector, _fee)`; if the fee collector's list is full and it doesn't already hold that deposit token, `seize → _transfer → addToDepositTokensOfAccount` reverts and the *entire liquidation transaction* fails whenever `_fee > 0` [5](#0-4) . Liquidators are also blocked if their own list is full for a collateral they don't hold.

Removal of a dusted entry requires the victim to transfer the full dust balance back out, but `transfer`/`transferFrom` are gated by `_revertIfLocked` / `unlockedBalanceOf` [6](#0-5)  — a leveraged victim may be unable to free the slot until they deleverage, extending the DoS.

### Impact Explanation
- **Per-victim liveness**: a dusted victim cannot onboard new collateral types or open debt in a new synthetic; combined with locked collateral this can trap them in an unhealthy trajectory while blocking the standard remediation (depositing the new collateral).
- **Protocol-wide liquidation DoS → insolvency**: dusting `feeCollector` to a full list makes every `liquidate` with non-zero protocol fee revert, halting liquidations across all accounts collateraled by tokens the collector doesn't already hold. Bad debt can accumulate while liquidations are bricked. This matches the CVE class: a remotely triggered abort producing denial of service of a critical path.

### Likelihood Explanation
The attack needs only an EOA, dust amounts of each listed deposit token, and public `transfer`/`deposit` calls — no privileged role, oracle manipulation, or governance. Cost is bounded by ~30 dust transfers plus any `deposit` gas. Success depends on deployed config: it requires the pool to have enough registered `DepositToken`s to fill the cap (`addDepositToken` itself caps at `MAX_TOKENS_PER_USER`, so up to 30 exist [7](#0-6) ), and the fee-collector DoS requires `protocolFee > 0` in `feeProvider.liquidationFees()`. Neither requires attacker influence over governance.

### Recommendation
- Make list insertion non-reverting for unsolicited pushes, or only apply the `MAX_TOKENS_PER_USER` check on user-initiated actions (e.g., `deposit`/`issue` where the caller is the account) rather than inside `_transfer`/`seize` credit paths.
- Alternatively, exempt `feeCollector` and liquidation `seize` credits from the cap, or auto-rollover dust additions instead of reverting.
- A cheaper mitigation: let `removeFromDepositTokensOfAccount` be triggerable by the account via a `sweepDust`/force-transfer that ignores `_revertIfLocked` for balances below a dust threshold.

### Proof of Concept
Foundry fork test sketch (Hardhat equivalents in `test/Pool.test.ts` use the same helpers):

```solidity
// contracts: Pool, DepositToken (msdTOKEN per collateral), DebtToken
function test_DustGriefingBlocksVictimAndFeeCollectorLiquidations() public {
    address victim = address(0xV);
    address attacker = address(0xA);

    // --- step 1: attacker fills victim's list ---
    address[] memory dts = pool.getDepositTokens();
    for (uint i; i < dts.length && pool.debtTokensOfAccount.length(victim)
         + pool.depositTokensOfAccount.length(victim) < 30; ++i) {
        IDepositToken dt = IDepositToken(dts[i]);
        // attacker deposits dust and pushes it to victim (push, no consent)
        deal(address(dt.underlying()), attacker, 1);
        vm.startPrank(attacker);
        dt.underlying().approve(address(dt), 1);
        dt.deposit(1, attacker);
        dt.transfer(victim, dt.balanceOf(attacker)); // adds entry to victim's list
        vm.stopPrank();
    }
    assertEq(
        pool.debtTokensOfAccount.length(victim) + pool.depositTokensOfAccount.length(victim),
        pool.MAX_TOKENS_PER_USER()
    );

    // --- step 2: victim cannot deposit a new collateral type ---
    IDepositToken newDt = /* a deposit token victim doesn't hold */;
    vm.prank(victim);
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    newDt.deposit(1e18, victim);

    // --- step 3: victim cannot issue a new debt-token type ---
    vm.prank(victim);
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    newDebtToken.issue(1, victim);

    // --- step 4: dust feeCollector -> all fee-bearing liquidations revert ---
    address fc = pool.poolRegistry().feeCollector();
    for (uint i; i < dts.length; ++i) { /* same push loop targeting fc */ }

    // victim account under water:
    vm.prank(liquidator);
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    pool.liquidate(msEth, underwaterAccount, repayAmt, depositToken); // reverts at seize(feeCollector) when _fee > 0
}
```

The key assertion is step 4: with `feeProvider.liquidationFees().protocolFee > 0`, `liquidate` always calls `seize(..., feeCollector, _fee)`; once the collector's list is full for a token it doesn't hold, every liquidation of that collateral reverts, demonstrating the remote DoS on the liquidation path.

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

**File:** contracts/DepositToken.sol (L211-234)
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

**File:** contracts/DepositToken.sol (L348-354)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```

**File:** contracts/DebtToken.sol (L598-600)
```text
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDebtTokensOfAccount(account_);
        }
```
