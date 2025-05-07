from functools import wraps
from decimal import Decimal
from fee_allocator.logger import logger


def return_zero_if_dust(threshold=Decimal("1E-20"), any_or_all="any"):
    """
    short circuit and return zero if `any_or_all` values used in the decorated method are less than `threshold`
    or if the final result is less than `threshold`

    `any_or_all` can be either 'any' or 'all'
    any: if any value used in the decorated method is less than `threshold`, return zero
    all: if all values used in the decorated method are less than `threshold`, return zero
    """

    def decorator(func):
        @wraps(func)
        def wrapper(self):
            attributes = func.__code__.co_names
            values = [getattr(self, attr, None) for attr in attributes]
            decimal_values = [value for value in values if isinstance(value, Decimal)]

            if any_or_all == "any":
                if any(value < threshold for value in decimal_values):
                    return Decimal(0)
            elif any_or_all == "all":
                if all(value < threshold for value in decimal_values):
                    return Decimal(0)
            else:
                raise ValueError("any_or_all must be 'any' or 'all'")

            result = func(self)
            if not isinstance(result, Decimal):
                raise TypeError(
                    f"`return_zero_if_dust` can only be used on methods that return Decimals, not {type(result)}"
                )
            return result if result > threshold else Decimal(0)

        return wrapper

    return decorator


def round(decimals):
    def decorator(func):
        @wraps(func)
        def wrapper(self):
            result = func(self)
            if not isinstance(result, Decimal):
                raise TypeError(
                    f"`round` can only be used on methods that return `Decimal`, not {type(result)}"
                )
            return result.quantize(Decimal(10) ** -decimals)

        return wrapper

    return decorator


def require_pool_fee_data(func):
    """
    Ensures that pool_fee_data is set before accessing properties that depend on it.
    If pool_fee_data is None, calls set_pool_fee_data() to initialize it.
    """
    @wraps(func)
    def wrapper(self):
        if self.pool_fee_data is None:
            logger.info(f"Initializing pool_fee_data for {self.name} before accessing {func.__name__}")
            self.set_pool_fee_data()
            logger.info(f"Successfully initialized pool_fee_data for {self.name}")
        return func(self)
    return wrapper
