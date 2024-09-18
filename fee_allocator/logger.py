import logging

logger = logging.getLogger()
log_handler = logging.StreamHandler()
logger.addHandler(log_handler)
logger.setLevel(logging.INFO)

# the gql logger likes to spam stdout
gql_logger = logging.getLogger("gql.transport.requests")
gql_logger.setLevel(logging.CRITICAL)
