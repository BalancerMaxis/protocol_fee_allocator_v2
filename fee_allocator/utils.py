from datetime import datetime, timedelta
import pytz


def get_last_thursday_odd_week():
    # Use the current UTC date and time
    current_datetime = datetime.utcnow().replace(tzinfo=pytz.utc)

    # Calculate the difference between the current weekday and Thursday (where Monday is 0 and Sunday is 6)
    days_since_thursday = (current_datetime.weekday() - 3) % 7

    # Calculate the date of the most recent Thursday
    most_recent_thursday = current_datetime - timedelta(days=days_since_thursday)

    # Check if the week of the most recent Thursday is odd
    is_odd_week = most_recent_thursday.isocalendar()[1] % 2 == 1

    # If it's not an odd week or we are exactly on Thursday but need to check if the week before was odd
    if not is_odd_week or (
        days_since_thursday == 0
        and (most_recent_thursday - timedelta(weeks=1)).isocalendar()[1] % 2 == 1
    ):
        # Go back one more week if it's not an odd week
        most_recent_thursday -= timedelta(weeks=1)

    # Ensure the Thursday chosen is in an odd week
    if most_recent_thursday.isocalendar()[1] % 2 == 0:
        most_recent_thursday -= timedelta(weeks=1)

    # Calculate the timestamp of the last Thursday at 00:00 UTC
    last_thursday_odd_utc = most_recent_thursday.replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    return last_thursday_odd_utc
