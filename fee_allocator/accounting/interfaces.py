from abc import ABC

"""
`Chain` and `CorePool` reference each other so they need to be abstract to prevent circular imports.
"""


class AbstractCorePool(ABC):
    pass


class AbstractChain(ABC):
    pass
