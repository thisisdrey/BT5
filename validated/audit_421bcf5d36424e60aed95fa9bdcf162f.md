### Title
Attacker can fill a victim's per-account token list with dust deposits, blocking collateral top-ups and new debt positions and enabling forced liquidation - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
The Discourse bug class is "cheap attacker requests consume a bounded shared resource (disk) until the service fails." The on-chain analog in Metronome is `Pool.depositTokensOfAccount` / `debtTokensOfAccount`, bounded by `MAX_TOKENS_PER_USER = 30`. Because `DepositToken.deposit(amount_, onBehalfOf_)` mints to an arbitrary recipient and `_mint` unconditionally calls `pool.addToDepositTokensOfAccount(account_)` on a first-time balance, an unprivileged attacker can dust-fill any victim's token list. Once the combined list hits 30 entries, `onlyIfAdditionWillNotReachMaxTokens` reverts, so the victim cannot deposit any *new* collateral type or issue debt in a new synthetic — including the top-up needed to avoid liquidation.

### Finding Description
- `Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` are guarded by `onlyIfAdditionWillNotReachMaxTokens`, which reverts when `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= 30` [1](#0-0) .
- `DepositToken.deposit` accepts an arbitrary `onBehalfOf_` and only requires the caller to supply the underlying; `_mint` adds the token to the recipient's list whenever their prior balance is zero [2](#0-1) [3](#0-2) .
- The same applies to `DepositToken.transfer`/`transferFrom` via `_transfer`, which also calls `addToDepositTokensOfAccount` for a first-time recipient — so the attacker doesn't even need to buy every underlying; holding dust of each msdTOKEN is enough.

Attack path (no privileged role needed):
1. Attacker deposits dust (e.g., 1 wei above the deposit fee) of every pool collateral into their own account, or acquires dust msdTOKEN balances.
2. For each deposit token `d_i` in `pool.getDepositTokens()`, attacker calls `d_i.transfer(victim, dust)` or `d_i.deposit(dust, victim)`. Each call appends `d_i` to `depositTokensOfAccount[victim]`; the "AlreadyExists" revert protects re-adds, and `UserReachedMaxTokens` only caps the *victim's* list — nothing authenticates that the victim consented.
3. Victim's combined list reaches 30. Any subsequent `deposit`/`transfer`/`issue`/`leverage` path that would add a *new* token reverts with `UserReachedMaxTokens`.

Broken invariant: liveness — the victim loses the ability to add new collateral types or open new debt positions, exactly like a full log disk stops a service. If the victim's position approaches the collateral factor threshold, they cannot deposit a different collateral to restore health (depositing an existing listed collateral still works, but if they don't hold it they are stuck), and a liquidator can seize their position at a bonus.

### Impact Explanation
Temporary freezing of user functionality and a path to forced liquidation (direct loss via liquidator incentive). The victim retains the dust tokens (they can burn/withdraw them to free slots, paying gas each time), so funds are not permanently locked; however, the attacker can re-grief cheaply and repeatedly, and during a price crash a blocked collateral top-up converts directly into liquidation loss. The account list also feeds `debtPositionOf`/`depositOf` loops, though the 30-cap keeps those bounded, so this is a slot-exhaustion DoS, not a gas DoS.

### Likelihood Explanation
Fully unprivileged: `deposit` is `whenNotPaused nonReentrant onlyIfDepositTokenExists` with no victim authorization, and `transfer` is permissionless. Cost is roughly N small transfers (bounded by the number of registered deposit/debt tokens, typically far below 30 combined, so the attacker needs the victim to already hold a few entries or register across multiple pools' tokens — note the cap counts both lists in *this* pool only). The victim's self-service recovery (burning dust to zero out balances) costs gas and must race re-griefing, which keeps the griefing window meaningful.

### Recommendation
Track membership per (account, token) such that unsolicited dust cannot consume slots without consent — e.g., only add to `depositTokensOfAccount` when the recipient is `_msgSender()` or `onBehalfOf_ == _msgSender()`, or maintain an explicit opt-in set for third-party additions. Alternatively, allow list eviction of zero-value dust entries, or raise/remove reliance on the fixed cap by bounding the health loops differently.

### Proof of Concept
```solidity
// Hardhat/Foundry fork sketch
// assume pool has deposit tokens d1..dk and victim has 0..m existing entries
for (uint i; i < k; ++i) {
    IDepositToken d = depositTokens[i];
    if (pool.getDepositTokensOfAccount(victim).contains(d)) continue;
    // cheapest: attacker already holds dust of d, or deposits dust on behalf of victim
    d.transfer(victim, 1); // or d.deposit(dustUnderlying, victim)
}
// victim list now at MAX_TOKENS_PER_USER
// any new-token operation reverts:
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
newDepositToken.deposit(1 ether, victim);
// if victim's HF < 1 and they only hold a new collateral type to top up,
// liquidator executes pool.liquidate(synth, victim, repay, existingDepositToken)
```
Existing test coverage confirming the revert behavior: `test/Pool.test.ts` "should revert when reach max tokens" [4](#0-3) .

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

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** test/Pool.test.ts (L1386-1416)
```typescript
      it('should revert when reach max tokens', async function () {
        // given
        const max = (await pool.MAX_TOKENS_PER_USER()).toNumber()
        const accountAddress = ethers.utils.hexlify(ethers.utils.randomBytes(20))

        for (let i = 0; i < max / 2; ++i) {
          const deposit = await smock.fake('DepositToken')
          deposit.underlying.returns(ethers.utils.hexlify(ethers.utils.randomBytes(20)))
          await setCode(deposit.address, '0x01')
          await setBalance(deposit.address, parseEther('1'))

          await pool.addDepositToken(deposit.address)
          await pool.connect(deposit.wallet).addToDepositTokensOfAccount(accountAddress)
        }

        for (let i = 0; i < max / 2; ++i) {
          const debt = await smock.fake('DebtToken')
          debt.syntheticToken.returns(ethers.utils.hexlify(ethers.utils.randomBytes(20)))
          await setCode(debt.address, '0x01')
          await setBalance(debt.address, parseEther('1'))

          await pool.addDebtToken(debt.address)
          await pool.connect(debt.wallet).addToDebtTokensOfAccount(accountAddress)
        }

        // then
        const tx = pool.connect(msdTOKEN.wallet).addToDepositTokensOfAccount(accountAddress)

        // when
        await expect(tx).revertedWithCustomError(pool, 'UserReachedMaxTokens')
      })
```
