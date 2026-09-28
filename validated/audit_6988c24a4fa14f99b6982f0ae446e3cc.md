### Title
Unprivileged dust transfers can fill an account's token list and permanently block new deposits or debt - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool` enforces a shared `MAX_TOKENS_PER_USER` limit for each account's deposit-token and debt-token lists. Because `DepositToken` is transferable and automatically registers each previously unseen token on the recipient, an attacker can force another account to consume those slots by sending it dust balances.

Once the victim's combined `depositTokensOfAccount` and `debtTokensOfAccount` length reaches `30`, every subsequent balance introduction reverts with `UserReachedMaxTokens`. This blocks receiving additional collateral receipt tokens, opening a first debt position in an additional synthetic asset, and protocol paths that must transfer a new `DepositToken` to that account.

### Finding Description
`MAX_TOKENS_PER_USER` is fixed at `30` in `Pool` [1](#0-0) . The modifier used by both account-list insertion functions rejects when the sum of the victim's deposit-token and debt-token entries is already at least `30` [2](#0-1) . Deposit tokens are inserted without recipient consent through `addToDepositTokensOfAccount` [3](#0-2) .

The externally callable `DepositToken.transfer` only checks that the sender has enough unlocked balance; it does not require recipient approval [4](#0-3) . The internal `_transfer` records a new recipient entry whenever the recipient's prior balance of that specific deposit token was zero [5](#0-4) .

Likewise, depositing to an arbitrary `onBehalfOf_` recipient invokes `_mint`, which adds the deposit token to that recipient's account list when its prior balance is zero [6](#0-5) [7](#0-6) . Therefore, dust transfers or dust deposits can permanently occupy all available slots until the victim removes existing positions.

### Impact Explanation
An attacker can grief a targeted account by filling every available account-list slot with registered deposit-token dust. After that:

- Direct deposits minting a new `DepositToken` to the victim revert.
- Incoming `DepositToken.transfer`, `transferFrom`, liquidation `seize`, or withdrawal-fee transfers of a token not already in the victim list revert.
- Debt issuance for a first debt-token type can revert if the victim is already at the shared cap, because `DebtToken._mint` calls `pool.addToDebtTokensOfAccount` for new debt balances [8](#0-7) .
- If the fixed `feeCollector` account is filled this way, every withdrawal that charges a nonzero withdraw fee and every liquidation that charges a nonzero protocol liquidation fee can revert when `_fee > 0` transfers an unregistered deposit token to it [9](#0-8) [10](#0-9) .

The invariant broken is liveness/accounting extensibility: a balance-bearing account should be able to receive valid protocol balances without its finite account-index capacity being consumed by unsolicited dust.

A limitation is that the attacker can only introduce already-registered `DepositToken` types; fake tokens cannot be added directly to the pool's global list. The severity therefore depends on the deployed pool having enough collateral types, or on the target already consuming part of the shared 30-slot list through existing deposit and debt positions.

### Likelihood Explanation
The attack path is unprivileged: deposit tokens are transferable, and neither `transfer`, `deposit`, `seize`, nor the pool insertion path obtains recipient consent. The cost is a dust amount of each available collateral receipt plus transaction gas.

Feasibility is configuration-dependent. `Pool.addDepositToken` itself caps the global deposit-token set at `MAX_TOKENS_PER_USER` [11](#0-10) , so an account cannot be forced above 30 deposit-token entries using fake pool tokens. On deployments with fewer than 30 registered collaterals, the attacker cannot fill the whole list solely with dust unless the victim already has enough debt-token entries to make up the remainder. Debt tokens are non-transferable and cannot simply be assigned to the victim [12](#0-11) .

### Recommendation
Do not impose a global per-account token-list limit on involuntary receipts, or separate unsolicited receipt accounting from position accounting. Practical fixes include:

- Remove the hard `30` recipient limit from `addToDepositTokensOfAccount`, since the set is enumerable and bounded by the pool's own registered deposit-token count.
- Exclude the protocol `feeCollector` from `MAX_TOKENS_PER_USER`, because fee transfers must remain infallible.
- Split deposit-token and debt-token limits so attacker-controlled deposit receipts cannot consume slots needed for debt accounting.
- Alternatively, make new recipient registration opt-in, although that would require more extensive accounting changes because liquidation and fee transfers currently depend on implicit registration.

### Proof of Concept
A Foundry test can reproduce the issue against a fork with a pool containing enough registered deposit tokens:

```solidity
// test/foundry/TokenListGriefing.t.sol
function testDustFillBlocksNewDepositTokenReceipt() external {
    address victim = makeAddr("victim");
    address feeCollector = pool.poolRegistry().feeCollector();

    address[] memory tokens = pool.getDepositTokens();

    // If victim already has debt-token entries, only the remaining
    // deposit-token slots need to be occupied.
    uint256 needed =
        pool.MAX_TOKENS_PER_USER() -
        pool.getDebtTokensOfAccount(victim).length;

    for (uint256 i; i < needed && i < tokens.length; ++i) {
        IDepositToken dt = IDepositToken(tokens[i]);

        // Obtain an unlocked dust balance by depositing a small amount
        // of each underlying collateral, then send it to the victim.
        IERC20 underlying = dt.underlying();
        deal(address(underlying), attacker, DUST);
        underlying.approve(address(dt), DUST);
        dt.deposit(DUST, attacker);

        dt.transfer(victim, 1);
    }

    assertGe(
        pool.getDepositTokensOfAccount(victim).length +
            pool.getDebtTokensOfAccount(victim).length,
        pool.MAX_TOKENS_PER_USER()
    );

    // Any previously unseen DepositToken now causes UserReachedMaxTokens.
    IDepositToken nextToken = IDepositToken(tokens[needed]);
    IERC20 nextUnderlying = nextToken.underlying();
    deal(address(nextUnderlying), attacker, DUST);
    nextUnderlying.approve(address(nextToken), DUST);
    nextToken.deposit(DUST, attacker);

    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    nextToken.transfer(victim, 1);
}
```

For the protocol-wide fee-collector variant, repeat the dust deposits/transfers with `victim = feeCollector`; subsequently, a withdrawal or liquidation whose computed `_fee > 0` and whose deposit token is absent from `feeCollector`'s list reaches `_transfer(..., feeCollector, _fee)` and reverts in `addToDepositTokensOfAccount`.

### Citations

**File:** contracts/Pool.sol (L76-80)
```text
    /**
     * @notice Maximum tokens per pool a user may have
     */
    uint256 public constant MAX_TOKENS_PER_USER = 30;

```

**File:** contracts/Pool.sol (L140-148)
```text
    /**
     * @dev Throws if token addition will reach the `account_`'s max
     */
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/Pool.sol (L211-220)
```text
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

**File:** contracts/Pool.sol (L587-593)
```text
        syntheticToken_.burn(_msgSender, amountToRepay_);
        _debtToken.burn(account_, amountToRepay_);
        depositToken_.seize(account_, _msgSender, _toLiquidator);

        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
        }
```

**File:** contracts/Pool.sol (L698-709)
```text
    function addDepositToken(address depositToken_) external onlyGovernor {
        if (depositToken_ == address(0)) revert AddressIsNull();
        IERC20 _underlying = IDepositToken(depositToken_).underlying();
        if (address(depositTokenOf[_underlying]) != address(0)) revert UnderlyingAssetInUse();
        // Note: Fee collector collects deposit tokens as fee
        if (depositTokens.length() >= MAX_TOKENS_PER_USER) revert ReachedMaxDepositTokens();

        if (!depositTokens.add(depositToken_)) revert DepositTokenAlreadyExists();

        depositTokenOf[_underlying] = IDepositToken(depositToken_);

        emit DepositTokenAdded(depositToken_);
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

**File:** contracts/DepositToken.sol (L347-354)
```text
    /// @inheritdoc IERC20
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }
```

**File:** contracts/DepositToken.sol (L469-488)
```text
    function _mint(
        address account_,
        uint256 amount_
    ) private onlyIfDepositTokenIsActive updateRewardsBeforeMintOrBurn(account_) {
        if (account_ == address(0)) revert MintToTheZeroAddress();

        totalSupply += amount_;
        if (totalSupply > maxTotalSupply) revert SurpassMaxDepositSupply();

        uint256 _balanceBefore = balanceOf[account_];
        unchecked {
            balanceOf[account_] = _balanceBefore + amount_;
        }

        emit Transfer(address(0), account_, amount_);

        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L506-520)
```text
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
```

**File:** contracts/DepositToken.sol (L545-551)
```text
        (_withdrawn, _fee) = quoteWithdrawOut(amount_);
        if (_fee > 0) {
            _transfer(account_, _pool.feeCollector(), _fee);
        }

        _burn(account_, _withdrawn);
        _pool.treasury().pull(to_, _withdrawn);
```

**File:** contracts/DebtToken.sol (L505-519)
```text
    /// @inheritdoc IERC20
    // solhint-disable-next-line
    function transfer(address /*recipient_*/, uint256 /*amount_*/) external override returns (bool) {
        revert TransferNotSupported();
    }

    /// @inheritdoc IERC20
    // solhint-disable-next-line
    function transferFrom(
        address /*sender_*/,
        address /*recipient_*/,
        uint256 /*amount_*/
    ) external override returns (bool) {
        revert TransferNotSupported();
    }
```

**File:** contracts/DebtToken.sol (L590-600)
```text
        totalSupply_ += amount_;
        if (totalSupply_ > maxTotalSupply) revert SurpassMaxDebtSupply();

        principalOf[account_] = _balanceBefore + amount_;
        debtIndexOf[account_] = debtIndex;
        emit Transfer(address(0), account_, amount_);

        //  Add this token to the debt tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDebtTokensOfAccount(account_);
        }
```
