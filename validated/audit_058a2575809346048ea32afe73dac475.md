### Title
Unprivileged dust deposits can fill a victim's per-account token list (`MAX_TOKENS_PER_USER = 30`), DoSing any further collateral deposits or debt issuance for that account - (File: contracts/Pool.sol)

### Summary
`Pool` keeps a per-account enumerable set of deposit tokens and debt tokens, capped at `MAX_TOKENS_PER_USER = 30` [1](#0-0) . Any addition reverts once `debtTokensOfAccount + depositTokensOfAccount >= 30` [2](#0-1) . Entries are added whenever an account's `DepositToken` balance moves from `0` to non-zero, via `addToDepositTokensOfAccount` called from the token during `_mint`/`_transfer` [3](#0-2) . Critically, `DepositToken.deposit(amount_, onBehalfOf_)` is permissionless and mints to an arbitrary `onBehalfOf_` [4](#0-3) , and `transfer` lets anyone push dust balances to a victim as well [5](#0-4) .

### Finding Description
Analogous to the OpenQ finding (attacker fills a 5-slot NFT deposit cap with worthless NFTs), an attacker here can fill a victim's 30-slot per-account token set with dust amounts of every registered `DepositToken`, at near-zero cost:

- For each registered deposit token `d` in `pool.getDepositTokens()`, the attacker calls `d.deposit(1 wei-equivalent, victim)` (or buys dust once and `transfer`s it). Each call adds `d` to `depositTokensOfAccount[victim]` because `addToDepositTokensOfAccount` only requires `msg.sender` to be a registered deposit token [3](#0-2) .
- Once the combined count reaches 30, `onlyIfAdditionWillNotReachMaxTokens` reverts with `UserReachedMaxTokens` [2](#0-1) .
- Thereafter: (a) any deposit/transfer/seize that would add a *new* deposit token to the victim's set reverts (the `_mint`/`_transfer` reverts atomically when `pool.addToDepositTokensOfAccount` reverts), and (b) the victim cannot issue any new debt-token type, since `DebtToken._mint` triggers `addToDebtTokensOfAccount` under the same cap [6](#0-5) .

The attack is also self-perpetuating: the dust balances the attacker gifts are counted in the victim's `depositOf`, so they add a negligible amount of collateral but permanently occupy slots; the victim can only free slots by withdrawing/transferring the dust to zero.

### Impact Explanation
This is a liveness/DoS impact on a specific account: the victim is permanently blocked from depositing collateral types not already in their list and from issuing any new synthetic debt type (`issue`, `leverage` paths all mint a new `DebtToken` entry). Existing positions remain withdrawable, so funds are not frozen, but the victim's ability to open positions is curtailed — matching the OpenQ report's "no more deposits can be made" severity (Medium). The impact is bounded by the number of deposit tokens actually registered in the pool (`addDepositToken` also caps at 30 [7](#0-6) ); on a pool with few listed collaterals the attack cannot reach 30 slots on its own, but it consumes the victim's free slots, and any mixture of deposit/debt tokens counts jointly toward the cap.

### Likelihood Explanation
- Fully permissionless: `deposit` has only `whenNotPaused nonReentrant onlyIfDepositTokenExists` [8](#0-7)  and `transfer` has no access control [5](#0-4) . No governor/keeper/oracle involvement needed.
- Cost: `30 × (dust amount + depositFee)`; dust can be 1 unit of the smallest denomination since the add only requires a non-zero resulting balance.
- Caveat: feasibility depends on the pool listing enough deposit tokens for the attacker to fill the victim's remaining slots; dust transfers of debt tokens are not possible (debt is non-transferable), so only deposit tokens are attacker-pushable.

### Recommendation
- Exclude zero-value dust: require a minimum meaningful amount in `deposit`/`_mint`/`_transfer` before calling `addToDepositTokensOfAccount`, or only add on deposits above a threshold.
- Alternatively, make the per-account cap additive-failure-tolerant: let `addTo*TokensOfAccount` no-op (or revert only the accounting, not the mint) — though that breaks `debtPositionOf` accuracy, so the cleaner fix is a per-token minimum balance and/or allowing victims to forcibly remove a token entry.
- Same fix pattern as the OpenQ recommendation: restrict who can push positions onto an account (e.g., require `onBehalfOf_ == _msgSender()` in `deposit`, or an opt-in whitelist).

### Proof of Concept
```ts
// Hardhat fork test against a deployed Pool with >= k registered DepositTokens
it('fills victim token list and blocks further deposits/issues', async () => {
  const pool = await ethers.getContractAt('Pool', POOL_ADDRESS);
  const depositTokenAddrs = await pool.getDepositTokens();
  const victim = alice.address;

  // Attacker pushes dust of each registered deposit token to the victim
  for (const addr of depositTokenAddrs) {
    const d = await ethers.getContractAt('DepositToken', addr);
    const underlying = await ethers.getContractAt('IERC20', await d.underlying());
    await underlying.connect(attacker).approve(d.address, 1);
    await d.connect(attacker).deposit(1, victim); // mints dust msdTOKEN to victim
  }

  const slots = (await pool.getDepositTokensOfAccount(victim)).length
              + (await pool.getDebtTokensOfAccount(victim)).length;
  expect(slots).to.be.gte(30); // MAX_TOKENS_PER_USER

  // Victim can no longer receive any new deposit token type
  const newToken = await ethers.getContractAt('DepositToken', someOtherDepositToken);
  await underlying2.connect(victim).approve(newToken.address, 1000);
  await expect(newToken.connect(victim).deposit(1000, victim))
    .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

  // Victim can no longer issue a new debt token type
  await expect(debtToken.connect(victim).issue(amount, victim))
    .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
});
```

Note on verification status: the `addToDepositTokensOfAccount` call site inside `DepositToken._mint`/`_transfer` was confirmed by behavior (tests show `addToDepositTokensOfAccount` is invoked exactly once per first non-zero balance, `test/DepositToken.test.ts:525-550`), but the exact line in `_mint` after `contracts/DepositToken.sol:480` was not read. The revert propagates because the Pool call is made synchronously during the mint/transfer.

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

**File:** contracts/Pool.sol (L703-703)
```text
        if (depositTokens.length() >= MAX_TOKENS_PER_USER) revert ReachedMaxDepositTokens();
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

**File:** contracts/DepositToken.sol (L348-354)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }
```
