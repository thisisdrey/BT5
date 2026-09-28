### Title
Griefing via capped per-account token list: `deposit(amount, onBehalfOf)` lets anyone fill a victim's `depositTokensOfAccount` to `MAX_TOKENS_PER_USER`, blocking them from adding new collateral types or issuing debt - (contracts/Pool.sol)

### Summary
Metronome keeps a per-account enumerable list of deposit tokens and debt tokens (`depositTokensOfAccount`, `debtTokensOfAccount`) and caps their combined size at `MAX_TOKENS_PER_USER`. Every insertion goes through `Pool.onlyIfAdditionWillNotReachMaxTokens`, which reverts with `UserReachedMaxTokens` once the cap is hit. Because `DepositToken.deposit(uint256 amount_, address onBehalfOf_)` lets any caller mint deposit tokens to an arbitrary `onBehalfOf_` address, an unprivileged attacker can push dust deposits of every listed collateral to a victim, filling the victim's list and DoS-ing any subsequent action that requires adding a new token to their list.

### Finding Description
- `DepositToken.deposit` accepts an arbitrary beneficiary and pulls the underlying from the caller, then `_mint(onBehalfOf_, _deposited)` (contracts/DepositToken.sol:211-237).
- `_mint` calls `pool.addToDepositTokensOfAccount(account_)` whenever the recipient's balance goes from 0 to >0 (contracts/DepositToken.sol:486-488). The same happens in `_transfer` for the recipient (contracts/DepositToken.sol:518-520), so plain `transfer`/`transferFrom` of deposit tokens can also be used to fill the victim's list at the cost of giving the tokens away.
- `Pool.addToDepositTokensOfAccount` and `Pool.addToDebtTokensOfAccount` are both gated by `onlyIfAdditionWillNotReachMaxTokens`, which reverts if `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER` (contracts/Pool.sol:143-148, 204-220).
- Once the cap is reached, the victim cannot:
  - `deposit` into any collateral they don't already hold (`_mint` → `addToDepositTokensOfAccount` reverts),
  - receive any new deposit-token type via `transfer`, `transferFrom`, or `seize` (reverts in the recipient-add step — this also makes the victim unable to be the beneficiary/liquidator of a `seize` for a new collateral),
  - `issue` a new synthetic (debt token `_mint` calls `addToDebtTokensOfAccount` on first mint, contracts/DebtToken.sol:598-600), so if all slots are filled with deposit tokens the victim cannot open any new debt position at all.
- No modifier stops this: `deposit` is `whenNotPaused nonReentrant` and permissionless, `onBehalfOf_` is unrestricted, `addToDepositTokensOfAccount` only authenticates that the caller is a registered deposit token (which is satisfied — the call originates from the real `DepositToken`), and there is no minimum deposit amount beyond `amount_ > 0`.

The invariant broken is liveness/availability of the per-account position-management list — the same class as the Salty whitelisting-queue DoS: a bounded list that permissionless, attacker-initiated insertions can fill, blocking legitimate use.

### Impact Explanation
The victim is prevented from adding any new collateral type or opening a new synthetic-debt position until they free a slot themselves (fully `withdraw`/`transfer` one dust deposit token, which triggers `removeFromDepositTokensOfAccount`, contracts/DepositToken.sol:460-462, 523-525). It is a temporary, self-healable DoS of core user functionality rather than permanent freezing — mirroring the Salty finding that was judged Medium ("function of the protocol or its availability could be impacted"). A corollary: an account stuffed to the cap also cannot receive seized collateral of a new type during liquidation (`seize` → `_transfer` → add reverts), which can make `Pool.liquidate` revert for that beneficiary path.

### Likelihood Explanation
The attack is fully unprivileged and cheap: depositing 1 unit (or any dust amount) of each whitelisted collateral to the victim is enough, since no minimum deposit is enforced. The number of distinct deposit tokens per pool is governor-controlled, and whether the cap can be fully reached depends on the deployed count vs `MAX_TOKENS_PER_USER`; pools with at least `MAX_TOKENS_PER_USER` listed collateral tokens are fully exploitable. Note the attacker effectively gifts the dust to the victim, which is a mild disincentive but negligible at dust scale.

### Recommendation
- Restrict who can trigger a first-time list insertion for a beneficiary — e.g., require `onBehalfOf_ == _msgSender()` in `deposit` (or make the add opt-in for the recipient), so a third party cannot grow a victim's list.
- Alternatively, drop entries by value or sweep dust balances when deciding list membership, or let `seize`/liquidation paths bypass the cap so a capped victim list can never break liquidations.
- Consider separating the cap accounting for debt vs deposit tokens so deposit-token spam cannot block debt issuance.

### Proof of Concept
Hardhat-style sketch (adapt to the repo's test fixtures in `test/Pool.test.ts`):

```ts
it('griefs victim via dust deposits until MAX_TOKENS_PER_USER', async () => {
  const depositTokens = await pool.getDepositTokens() // array of DepositToken addrs
  const attacker = alice
  const victim = bob

  for (const dt of depositTokens) {
    const depositToken = await ethers.getContractAt('DepositToken', dt)
    const underlying = await ethers.getContractAt('ERC20', await depositToken.underlying())
    // attacker acquires 1 wei of each underlying and approves the deposit token
    await underlying.connect(attacker).approve(dt, ethers.constants.MaxUint256)
    await depositToken.connect(attacker).deposit(1, victim.address)
    if ((await pool.depositTokensOfAccountLength(victim.address)) >= MAX) break
  }

  // victim's own deposit of a collateral type they don't hold now reverts
  await expect(
    newDepositToken.connect(victim).deposit(amount, victim.address)
  ).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

  // victim cannot issue a new synthetic either (first debt-token insert reverts)
  await expect(
    debtToken.connect(victim).issue(amount, victim.address)
  ).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

  // an AMM/liquidation seize to victim of a new collateral type also reverts
})
```

Caveat: I could not confirm the deployed value of `MAX_TOKENS_PER_USER` or the exact number of registered deposit tokens per pool from the indexed code; feasibility requires the pool to have enough distinct deposit tokens to reach the cap.