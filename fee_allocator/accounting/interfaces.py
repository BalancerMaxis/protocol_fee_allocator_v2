from abc import ABC

"""
`CorePoolChain` and `PoolFee` reference each other so they need to be abstract to prevent circular imports.
"""


class AbstractPoolFee(ABC):
    pass


class AbstractCorePoolChain(ABC):
    pass
