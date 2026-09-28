### Title
Unprivileged attacker can fill a victim's deposit-token account list to `MAX_TOKENS_PER_USER` with dust deposits, DoS-ing all future deposits/receipts of new collateral types - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`DepositToken.deposit(uint256 amount_, address onBehalfOf_)` lets any caller mint deposit tokens to an arbitrary `onBehalfOf_` account. On the first receipt of a deposit token, `Pool.addToDepositTokensOfAccount` is invoked, which reverts with `UserReachedMaxTokens` once the account already tracks `MAX_TOKENS_PER_USER` (30) tokens. An unprivileged attacker can deposit dust amounts of every registered deposit token on behalf of a victim, permanently filling the victim's per-account list. After that, any action that would add a *new* deposit token to the victim's set reverts — including the victim's own `deposit()` of a collateral type they don't yet hold, incoming `transfer`/`transferFrom` of deposit tokens, and `SmartFarmingManager` flows that mint new deposit positions.

### Finding Description
- `DepositToken.deposit` accepts an attacker-chosen beneficiary `onBehalfOf_` and calls `_mint(onBehalfOf_, _deposited)` [1](#0-0) .
- `_mint` calls `pool.addToDepositTokensOfAccount(account_)` whenever the recipient's balance was zero [2](#0-1) .
- The same add path is hit on `_transfer`, so liquidations/seizes and plain transfers into the victim also count toward the cap [3](#0-2) .
- `Pool` enforces `MAX_TOKENS_PER_USER = 30` and reverts with `UserReachedMaxTokens` once the victim's `depositTokensOfAccount` set is full [4](#0-3) .
- The attacker only needs to deposit 1-wei dust of each registered deposit token; `deposit` has no minimum-amount check beyond `amount_ > 0` and no permission check on `onBehalfOf_`.

### Impact Explanation
Once the victim's set is saturated (30 distinct deposit tokens held with dust), every code path that tries to add a new deposit token for that account reverts. Concretely:
- `victim.deposit()` for any collateral they don't already hold → reverts in `addToDepositTokensOfAccount` → funds cannot be deposited.
- Any `transfer`/`transferFrom` of a new deposit token to the victim → reverts.
- Any flow that mints a new deposit token to the victim (e.g., SmartFarmingManager leverage paths that mint collateral receipt tokens to the beneficiary) → reverts mid-transaction, freezing the feature.

This is a reachable denial of service on protocol operations against a targeted account, matching the bug class of the referenced advisory (unprivileged → availability impact). The victim can recover by transferring out the dust tokens to free slots, so impact is temporary freezing of functionality/funds rather than permanent loss — consistent with a Medium severity.

### Likelihood Explanation
- Requires only an EOA; no governor/guardian/keeper/oracle manipulation needed.
- Cost is bounded by acquiring dust amounts of each pool's registered deposit token; on pools with few collateral types the attack fills the cap quickly.
- Constraint: the pool must have enough distinct deposit tokens registered to reach 30, or the attacker combines the deposit-griefing with forced dust `transfer`s of any deposit token (also unrestricted). If the pool has fewer than 30 deposit tokens, the attacker fills all slots up to the registered count and the victim is blocked only for tokens they don't already hold — the severity scales with how many distinct collaterals exist. This caveat limits likelihood on small pools.
- Modifiers do not stop it: `whenNotPaused`/`whenNotShutdown` are normal-operation flags, `nonReentrant` is irrelevant, and `SynthContext._msgSender` only affects who `msgSender` is, not `onBehalfOf_`.

### Recommendation
- Enforce a minimum deposit amount (in USD terms via `masterOracle`, e.g. refuse deposits below a dust threshold) in `DepositToken.deposit`, or
- Only add tokens to `depositTokensOfAccount` when the resulting balance exceeds a minimum meaningful balance, or
- Remove `MAX_TOKENS_PER_USER` enforcement on the add path and instead bound iteration cost in `debtPositionOf`, so griefed slots cannot brick deposits.

### Proof of Concept
Hardhat sketch (uses existing test fixture style):

```ts
// Attacker fills victim's depositTokensOfAccount to MAX_TOKENS_PER_USER
const victim = alice.address
const attacker = user2

// assume pool has N registered deposit tokens; attacker holds dust of each underlying
for (const dToken of registeredDepositTokens) {
  const underlying = await ethers.getContractAt('IERC20', await dToken.underlying())
  await underlying.connect(attacker).approve(dToken.address, 1)
  await dToken.connect(attacker).deposit(1, victim) // mints dust on behalf of victim
}

// victim's set is now at the cap (or at N distinct tokens)
expect(await pool.getDepositTokensOfAccount(victim)).to.have.lengthOf(30)

// victim tries to deposit a collateral type they don't hold -> reverts
await underlyingNew.connect(victim).approve(msdNew.address, depositAmount)
await expect(
  msdNew.connect(victim).deposit(depositAmount, victim)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// any incoming transfer of a new deposit token also reverts
await expect(
  msdNew.connect(attacker).transfer(victim, 1)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
```

Note: I could not verify within the iteration limit whether `debtPositionOf` itself reverts when the list is at capacity (i.e., whether the cap blocks reads or only adds), and whether a minimum-deposit guard exists elsewhere in `Pool`/`SynthContext`. If `debtPositionOf` iterates the victim's whole set, the same griefing could additionally make `unlockedBalanceOf`/`withdraw` more expensive or OOG on large sets — worth confirming in a fork test before severity assignment.

### Citations

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

**File:** contracts/DepositToken.sol (L486-489)
```text
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
    }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```

**File:** contracts/Pool.sol (L76-79)
```text
    /**
     * @notice Maximum tokens per pool a user may have
     */
    uint256 public constant MAX_TOKENS_PER_USER = 30;
```
