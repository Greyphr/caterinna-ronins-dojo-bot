# Friendly labels mapped to IANA timezone database names.
# Using IANA names (not raw abbreviations like "EST") avoids ambiguity and
# automatically handles daylight saving time shifts correctly.

TIMEZONE_CHOICES = {
    "West Africa Time (WAT - Lagos/Abuja)": "Africa/Lagos",
    "UK (London)": "Europe/London",
    "Central Europe (Berlin/Paris)": "Europe/Berlin",
    "US Eastern (New York)": "America/New_York",
    "US Central (Chicago)": "America/Chicago",
    "US Mountain (Denver)": "America/Denver",
    "US Pacific (Los Angeles)": "America/Los_Angeles",
    "India (IST)": "Asia/Kolkata",
    "Japan (JST)": "Asia/Tokyo",
    "Australia Eastern (Sydney)": "Australia/Sydney",
}
