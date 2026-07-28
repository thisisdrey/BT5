# Q3855: Smart account deployment and initialization: init arrays / foreign context configure / initializer fidelity

## Question
Can an unprivileged deployer or external caller with no guardian or admin powers enter through `SmartAccountFactory.deploySmartAccount(...) and predictSmartAccountAddress(...)` with delegates, currencies, NFT collections, trusted recipients, owners, and threshold in the deployment initializer while the factory is deploying a brand-new Safe through the standard proxy path and make an attacker use the factory's configuration logic to alter a Safe or Safe-like context it should not own, breaking the rule that duplicate or crafted array inputs should not silently widen module delegates or spending surface beyond the explicit initializer semantics and leading to Unauthorized delegate or spending surface on a deployed smart account?

## Target
- File/function: contracts/SmartAccountFactory.sol / deploySmartAccount, configureSmartAccount, predictSmartAccountAddress
- Entrypoint: SmartAccountFactory.deploySmartAccount(...) and predictSmartAccountAddress(...)
- Attacker controls: delegates, currencies, NFT collections, trusted recipients, owners, and threshold in the deployment initializer
- Exploit idea: make an attacker use the factory's configuration logic to alter a Safe or Safe-like context it should not own
- Invariant to test: duplicate or crafted array inputs should not silently widen module delegates or spending surface beyond the explicit initializer semantics
- Expected Immunefi impact: Unauthorized delegate or spending surface on a deployed smart account
- Fast validation: Check that no foreign Safe or Safe-like contract can abuse the factory's delegatecall-based initializer to install new module state.
