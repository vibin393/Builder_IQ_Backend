"""Survey business rules: the response window and the UK star rating.

The star rating is the figure a UK housebuilder is judged on publicly: the HBF
customer-satisfaction star scheme derives a 1-to-5 rating from the proportion of
homeowners who would recommend the builder. Keeping the thresholds here, in one place,
means the headline on the dashboard and any future export can never drift apart.
"""

from __future__ import annotations

from datetime import date, timedelta

# How long a homeowner has to respond before the survey is treated as expired. Also the
# point at which a reminder is worth sending.
RESPONSE_WINDOW_DAYS = 21

# HBF-style star bands on "% who would recommend". 5 stars is the builder everyone quotes;
# below 60% is the one-star result that makes the trade press.
STAR_THRESHOLDS = [(90, 5), (80, 4), (70, 3), (60, 2)]


def due_date_for(sent: date) -> date:
    return sent + timedelta(days=RESPONSE_WINDOW_DAYS)


def star_rating(pct_recommend: float | None) -> int | None:
    """1-5 stars from the percentage of respondents who would recommend (0-100)."""
    if pct_recommend is None:
        return None
    for threshold, stars in STAR_THRESHOLDS:
        if pct_recommend >= threshold:
            return stars
    return 1
